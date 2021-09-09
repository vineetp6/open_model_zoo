#!/usr/bin/env python3

"""
 Copyright (c) 2021 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import logging as log
import re
import sys
from argparse import ArgumentParser, SUPPRESS
from pathlib import Path
from time import perf_counter

from openvino.inference_engine import IECore, get_version

sys.path.append(str(Path(__file__).resolve().parents[2] / 'common/python'))

from models.tokens_bert import text_to_tokens, load_vocab_file
from html_reader import get_paragraphs
from models import BertNamedEntityRecognition
from pipelines import get_user_config, parse_devices, AsyncPipeline
from helpers import log_runtime_settings

log.basicConfig(format='[ %(levelname)s ] %(message)s', level=log.DEBUG, stream=sys.stdout)

sentence_splitter = r'(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?)\s'
label_to_tag = ['O', 'B-MIS', 'I-MIS', 'B-PER', 'I-PER', 'B-ORG', 'I-ORG', 'B-LOC', 'I-LOC']


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help', default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument("-v", "--vocab", help="Required. Path to the vocabulary file with tokens",
                      required=True, type=str)
    args.add_argument("-m", "--model", help="Required. Path to an .xml file with a trained model",
                      required=True, type=Path)
    args.add_argument("-i", "--input", help="Required. URL to a page with context",
                      action='append',
                      required=True, type=str)
    args.add_argument("--input_names",
                      help="Optional. Inputs names for the network. "
                           "Default values are \"input_ids,attention_mask,token_type_ids\" ",
                      required=False, type=str, default="input_ids,attention_mask,token_type_ids")
    args.add_argument("-d", "--device",
                      help="Optional. Target device to perform inference on."
                           "Default value is CPU", default="CPU", type=str)
    args.add_argument('-nireq', '--num_infer_requests', help='Optional. Number of infer requests.',
                      default=0, type=int)
    args.add_argument('-nstreams', '--num_streams',
                      help='Optional. Number of streams to use for inference on the CPU or/and GPU in throughput '
                           'mode (for HETERO and MULTI device cases use format '
                           '<device1>:<nstreams1>,<device2>:<nstreams2> or just <nstreams>).',
                      default='', type=str)
    args.add_argument('-nthreads', '--num_threads', default=None, type=int,
                      help='Optional. Number of threads to use for inference on CPU (including HETERO cases).')
    return parser


def print_raw_results(score, filtered_labels_id, meta):
    if not filtered_labels_id:
        return
    sentence, c_token_s_e = meta['sentence'], meta['c_token_s_e']
    log.info('\t\tSentence: \n\t{}'.format(sentence))
    visualized = set()
    for id, label_id in filtered_labels_id:
        word_s, word_e = c_token_s_e[id - 1]
        if (word_s, word_e) in visualized:
            continue
        visualized.add((word_s, word_e))
        word = sentence[word_s:word_e]
        confidence = score[id][label_id]
        tag = label_to_tag[label_id]
        log.info('\n\tWord: {}\n\tConfidence: {}\n\tTag: {}'.format(word, confidence, tag))


def main():
    args = build_argparser().parse_args()

    paragraphs = get_paragraphs(args.input)

    preprocessing_start_time = perf_counter()
    vocab = load_vocab_file(args.vocab)
    log.debug("Loaded vocab file from {}, get {} tokens".format(args.vocab, len(vocab)))

    # get context as a string (as we might need it's length for the sequence reshape)
    context = '\n'.join(paragraphs)
    sentences = re.split(sentence_splitter, context)
    preprocessed_sentences = [text_to_tokens(sentence, vocab) for sentence in sentences]
    max_sentence_length = max([len(tokens) + 2 for tokens, _ in preprocessed_sentences])
    preprocessing_total_time = (perf_counter() - preprocessing_start_time) * 1e3
    source = tuple(zip(sentences, preprocessed_sentences))

    log.info('OpenVINO Inference Engine')
    log.info('\tbuild: {}'.format(get_version()))
    ie = IECore()

    plugin_config = get_user_config(args.device, args.num_streams, args.num_threads)

    log.info('Reading model {}'.format(args.model))
    model = BertNamedEntityRecognition(ie, args.model, vocab, args.input_names)
    if max_sentence_length > model.max_length:
        model.reshape(max_sentence_length)

    pipeline = AsyncPipeline(ie, model, plugin_config,
                             device=args.device, max_num_requests=args.num_infer_requests)
    log.info('The model {} is loaded to {}'.format(args.model, args.device))
    log_runtime_settings(pipeline.exec_net, set(parse_devices(args.device)))

    next_sentence_id = 0
    next_sentence_id_to_show = 0
    start_time = perf_counter()

    while True:
        if pipeline.callback_exceptions:
            raise pipeline.callback_exceptions[0]
        results = pipeline.get_result(next_sentence_id_to_show)
        if results:
            (score, filtered_labels_id), meta = results
            next_sentence_id_to_show += 1
            print_raw_results(score, filtered_labels_id, meta)
            continue

        if pipeline.is_ready():
            if next_sentence_id == len(source):
                break
            sentence, (c_tokens_id, c_token_s_e) = source[next_sentence_id]
            pipeline.submit_data(c_tokens_id, next_sentence_id, {'sentence': sentence, 'c_token_s_e': c_token_s_e})
            next_sentence_id += 1
        else:
            pipeline.await_any()

    pipeline.await_all()
    for sentence_id in range(next_sentence_id_to_show, next_sentence_id):
        results = pipeline.get_result(sentence_id)
        while results is None:
            results = pipeline.get_result(sentence_id)
        (score, filtered_labels_id), meta = results
        print_raw_results(score, filtered_labels_id, meta)

    total_latency = (perf_counter() - start_time) * 1e3 + preprocessing_total_time
    log.info("Metrics report:")
    log.info("\tLatency: {:.1f} ms".format(total_latency))


if __name__ == '__main__':
    sys.exit(main() or 0)
