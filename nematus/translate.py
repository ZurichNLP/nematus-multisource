#!/usr/bin/env python
'''
Translates a source file using a translation model.
'''
import sys
import numpy
import json
import os
import logging

from multiprocessing import Process, Queue
from collections import defaultdict
from Queue import Empty

from util import load_dict, load_config, seqs2words
from compat import fill_options, dummy_options
from hypgraph import HypGraphRenderer
from console import ConsoleInterfaceDefault

class Translation(object):
    #TODO move to separate file?
    """
    Models a translated segment.
    """
    def __init__(self, source_words, target_words, sentence_id=None, score=0, alignment=None,
                 target_probs=None, hyp_graph=None, hypothesis_id=None, aux_source_words=None, aux_alignment=None):
        self.source_words = source_words
        self.target_words = target_words
        self.sentence_id = sentence_id
        self.score = score
        self.alignment = alignment #TODO: assertion of length?
        self.target_probs = target_probs #TODO: assertion of length?
        self.hyp_graph = hyp_graph
        self.hypothesis_id = hypothesis_id

        # multi-source params
        self.aux_source_words = aux_source_words
        self.aux_alignment = aux_alignment
        self.multisource = True

    def get_alignment(self, aux_id=None):
        if aux_id is not None:
            return self.aux_alignment[aux_id]
        else:
            return self.alignment

    def get_alignment_text(self, aux_id=None):
        """
        Returns this translation's alignment (or the auxiliary alignment for multi-source
        @param aux True) rendered as a string.
        Columns in header: sentence id ||| target words ||| score |||
                           source words ||| number of source words |||
                           number of target words
        """
        if aux_id is None:
            src_words = self.source_words
            alignment = self.alignment
        else:
            src_words = self.aux_source_words[aux_id]
            alignment = self.aux_alignment[aux_id]

        columns = [
            self.sentence_id,
            " ".join(self.target_words),
            self.score,
            " ".join(src_words),
            len(src_words) + 1,
            len(self.target_words) + 1
        ]
        header = "{0} ||| {1} ||| {2} ||| {3} ||| {4} {5}\n".format(*columns)

        matrix = []
        for target_word_alignment in alignment:
            current_weights = []
            for weight in target_word_alignment:
                current_weights.append(str(weight))
            matrix.append(" ".join(current_weights))

        return header + "\n".join(matrix)

    def get_alignment_json(self, as_string=True, aux=None):
        """
        Returns this translation's alignment (or the auxiliary alignment for multi-source
        @param aux True) as a JSON serializable object
        (@param as_string False) or a JSON formatted string (@param as_string
        True).
        """
        if aux is None:
            source_tokens = self.source_words + ["</s>"]
            alignment = self.alignment
        else:
            source_tokens = self.aux_source_words[aux] + ["</s>"]
            alignment = self.aux_alignment[aux]

        target_tokens = self.target_words + ["</s>"]

        if self.hypothesis_id is not None:
            tid = self.sentence_id + self.hypothesis_id
        else:
            tid = self.sentence_id
        links = []

        for target_index, target_word_alignment in enumerate(alignment):
            for source_index, weight in enumerate(target_word_alignment):

                links.append(
                             (target_tokens[target_index],
                              source_tokens[source_index],
                              str(weight),
                              self.sentence_id,
                              tid)
                             )
        return json.dumps(links, ensure_ascii=False, indent=2) if as_string else links

    def get_target_probs(self):
        """
        Returns this translation's word probabilities as a string.
        """
        return " ".join("{0}".format(prob) for prob in self.target_probs)

    def save_hyp_graph(self, filename, word_idict_trg, detailed=True, highlight_best=True):
        """
        Writes this translation's search graph to disk.
        """
        if self.hyp_graph:
            renderer = HypGraphRenderer(self.hyp_graph)
            renderer.wordify(word_idict_trg)
            renderer.save_png(filename, detailed, highlight_best)
        else:
            pass #TODO: Warning if no search graph has been constructed during decoding?


class QueueItem(object):
    """
    Models items in a queue.
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Translator(object):

    def __init__(self, decoder_settings):
        """
        Loads translation models.
        """
        self._models = decoder_settings.models
        self._num_processes = decoder_settings.num_processes
        self._device_list = decoder_settings.device_list
        self._verbose = decoder_settings.verbose
        self._retrieved_translations = defaultdict(dict)

        # load model options
        self._load_model_options()

        # multi-source
        self.multisource = decoder_settings.multisource
        self.num_attentions = self._options[-1]['num_attentions']
        self.num_encoders = self._options[-1]['num_encoders']

        # load and invert dictionaries
        self._build_dictionaries()
        # set up queues
        self._init_queues()
        # init worker processes
        self._init_processes()

    def _load_model_options(self):
        """
        Loads config options for each model.
        """
        options = []
        for model in self._models:
            options.append(load_config(model))
            # backward compatibility
            fill_options(options[-1])
            # dummy features for single source using multi-source code
            dummy_options(options[-1])

        self._options = options

    def _build_dictionaries(self):
        """
        Builds and inverts source and target dictionaries, taken
        from the first model since all of them must have the same
        vocabulary.
        """
        # is at least one model multi-source?
        #if len([True for options in self._options if options['multisource_type'] is not None ]) > 0:
        #    multisource = True
        #else:
        #    multisource = False

        dictionaries = self._options[0]['dictionaries']
        dictionaries_source = dictionaries[:-1]
        dictionary_target = dictionaries[-1]
        aux_dictionaries_source = [] # to be left empty if no multisource

        # get all auxiliary dictionaries
        if self.multisource:

            totalnum = 0
            for i in range(sum(self._options[0]['extra_source_dicts_nums'])):
                end_idx = totalnum + self._options[0]['extra_source_dicts_nums'][i]
                aux_dictionaries_source.append(self._options[0]['extra_source_dicts'][totalnum:end_idx])
                totalnum = end_idx

            # if still empty, then none were specified so copy source dictionaries
            if len(aux_dictionaries_source) == 0:
                aux_dictionaries_source = [dictionaries_source * self.num_encoders]

            # assign the same dictionaries as for the main input if none are specified
            #if aux_dictionaries_source is None or len(aux_dictionaries_source)<1:
            #    logging.info("No auxiliary input source dicts provided so reusing the main source dicts.")
            #    aux_dictionaries_source = [dictionaries_source * self.num_encoders]

            # otherwise use those that are specified
            #elif len(aux_dictionaries_source) != sum(self._options[0]['extra_source_dicts_nums']):
            #    exit('The number of extra dictionaries provided does not match the number specified.\n')

            #else:
            #    totalnum = 0
            #    for num_dicts in self._options[0]['extra_source_dicts_nums']:
            #        aux_dictionaries_source.append(aux_dictionaries_source_tmp[totalnum:num_dicts])
            #    totalnum += num_dicts

        # load and invert source dictionaries
        all_n_words_src = self._options[0]['n_words_src'] #+ self._options[0]['extra_n_words_src']
        # go through the set of dictionaries for each input

        self._word_dicts = []
        self._word_idicts = []

        for input_dictionary in [dictionaries_source] + aux_dictionaries_source:
            word_dicts = []
            word_idicts = []
            for dictionary in input_dictionary:
                word_dict = load_dict(dictionary)
                if all_n_words_src:
                    for key, idx in word_dict.items():
                        if idx >= all_n_words_src:
                            del word_dict[key]
                word_idict = dict()
                for kk, vv in word_dict.iteritems():
                    word_idict[vv] = kk
                word_idict[0] = '<eos>'
                word_idict[1] = 'UNK'
                word_dicts.append(word_dict)
                word_idicts.append(word_idict)

            self._word_dicts.append(word_dicts)
            self._word_idicts.append(word_idicts)


        # load and invert target dictionary
        word_dict_trg = load_dict(dictionary_target)
        word_idict_trg = dict()
        for kk, vv in word_dict_trg.iteritems():
            word_idict_trg[vv] = kk
        word_idict_trg[0] = '<eos>'
        word_idict_trg[1] = 'UNK'

        self._word_idict_trg = word_idict_trg


    def _init_queues(self):
        """
        Sets up shared queues for inter-process communication.
        """
        self._input_queue = Queue()
        self._output_queue = Queue()

    def shutdown(self):
        """
        Executed from parent process to terminate workers,
        method: "poison pill".
        """
        for process in self._processes:
            self._input_queue.put(None)

    def _init_processes(self):
        """
        Starts child (worker) processes.
        """
        processes = [None] * self._num_processes
        for process_id in xrange(self._num_processes):
            deviceid = ''
            if self._device_list is not None and len(self._device_list) != 0:
                deviceid = self._device_list[process_id % len(self._device_list)].strip()
            processes[process_id] = Process(target=self._start_worker, args=(process_id, deviceid))
            processes[process_id].start()

        self._processes = processes

    # MODEL LOADING AND TRANSLATION IN CHILD PROCESS ###
    def _load_theano(self):
        """
        Loads models, sets theano shared variables and builds samplers.
        This entails irrevocable binding to a specific GPU.
        """
        from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
        from theano import shared

        from nmt import (build_sampler, build_multi_sampler, gen_sample)
        from theano_util import (numpy_floatX, load_params, init_theano_params)

        trng = RandomStreams(1234)
        use_noise = shared(numpy_floatX(0.))

        fs_init = []
        fs_next = []

        for model, option in zip(self._models, self._options):

            # check compatibility with multisource
            if option["multisource_type"] is not None and len(option['extra_sources']) == 0:
                logging.error("This model is multi-source but no auxiliary source file was provided.")
                sys.exit(1)
            elif option["multisource_type"] is None and len(option['extra_sources']) != 0:
                logging.warn("You provided an auxiliary input but this model is not multi-source. Ignoring extra input.")

            param_list = numpy.load(model).files
            param_list = dict.fromkeys([key for key in param_list if not key.startswith('adam_')], 0)
            params = load_params(model, param_list)
            tparams = init_theano_params(params)

            # always return alignment at this point
            if option['multisource_type'] is not None:
                f_init, f_next = build_multi_sampler(tparams, option, use_noise, trng, return_alignment=True)
            else:
                f_init, f_next = build_sampler(tparams, option, use_noise, trng, return_alignment=True)

            fs_init.append(f_init)
            fs_next.append(f_next)

        return trng, fs_init, fs_next, gen_sample

    def _set_device(self, device_id):
        """
        Modifies environment variable to change the THEANO device.
        """
        if device_id != '':
            try:
                theano_flags = os.environ['THEANO_FLAGS'].split(',')
                exist = False
                for i in xrange(len(theano_flags)):
                    if theano_flags[i].strip().startswith('device'):
                        exist = True
                        theano_flags[i] = '%s=%s' % ('device', device_id)
                        break
                if exist is False:
                    theano_flags.append('%s=%s' % ('device', device_id))
                os.environ['THEANO_FLAGS'] = ','.join(theano_flags)
            except KeyError:
                # environment variable does not exist at all
                os.environ['THEANO_FLAGS'] = 'device=%s' % device_id

    def _load_models(self, process_id, device_id):
        """
        Modifies environment variable to change the THEANO device, then loads
        models and returns them.
        """
        logging.debug("Process '%s' - Loading models on device %s\n" % (process_id, device_id))

        # modify environment flag 'device'
        self._set_device(device_id)

        # build and return models
        return self._load_theano()

    def _start_worker(self, process_id, device_id):
        """
        Function executed by each worker once started. Do not execute in
        the parent process.
        """
        # load theano functionality
        trng, fs_init, fs_next, gen_sample = self._load_models(process_id, device_id)

        # listen to queue in while loop, translate items
        while True:
            input_item = self._input_queue.get()
            if input_item is None:
                break
            idx = input_item.idx
            request_id = input_item.request_id
            output_item = self._translate(process_id, input_item, trng, fs_init, fs_next, gen_sample)
            self._output_queue.put((request_id, idx, output_item))
        return

    def _translate(self, process_id, input_item, trng, fs_init, fs_next, gen_sample):
        """
        Actual translation (model sampling).
        """
        # unpack input item attributes
        normalization_alpha = input_item.normalization_alpha
        nbest = input_item.nbest
        idx = input_item.idx

        # logging
        logging.debug('{0} - {1}\n'.format(process_id, idx))

        # sample given an input sequence and obtain scores
        if len(input_item.aux_seq) > 0:
            sample, score, word_probs, alignments, hyp_graph = self._multi_sample(input_item, trng, fs_init, fs_next, gen_sample)
        else:
            sample, score, word_probs, alignments, hyp_graph = self._sample(input_item, trng, fs_init, fs_next, gen_sample)


        # normalize scores according to sequence lengths
        if normalization_alpha:
            adjusted_lengths = numpy.array([len(s) ** normalization_alpha for s in sample])
            score = score / adjusted_lengths
        if nbest is True:
            output_item = sample, score, word_probs, alignments, hyp_graph
        else:
            # return translation with lowest score only
            sidx = numpy.argmin(score)

            # modified for multi-source
            output_item = sample[sidx], score[sidx], word_probs[sidx], [align[sidx] for align in alignments], hyp_graph

        return output_item

    def _multi_sample(self, input_item, trng, fs_init, fs_next, gen_sample):
        """
        Sample from model.
        """
        # unpack input item attributes
        return_hyp_graph = input_item.return_hyp_graph
        return_alignment = input_item.return_alignment

        suppress_unk = input_item.suppress_unk
        k = input_item.k
        seq = input_item.seq
        aux_seqs = input_item.aux_seq

        if self._options[0]['multisource_type'] == 'init-decoder':
            init_decoder = True
        else:
            init_decoder = False

        extra_xs=[numpy.array(aux).T.reshape([len(aux[0]), len(aux), 1]) for aux in aux_seqs]

        return gen_sample(fs_init, fs_next,
                          numpy.array(seq).T.reshape([len(seq[0]), len(seq), 1]),
                          trng=trng, k=k, maxlen=200,
                          stochastic=False, argmax=False,
                          return_alignment=return_alignment,
                          suppress_unk=suppress_unk,
                          return_hyp_graph=return_hyp_graph,
                          extra_xs=extra_xs, #[numpy.array(aux_seq).T.reshape([len(aux_seq[0]), len(aux_seq), 1])],
                          init_decoder=init_decoder)

    def _sample(self, input_item, trng, fs_init, fs_next, gen_sample):
        """
        Sample from model.
        """
        # unpack input item attributes
        return_hyp_graph = input_item.return_hyp_graph
        return_alignment = input_item.return_alignment
        suppress_unk = input_item.suppress_unk
        k = input_item.k
        seq = input_item.seq

        return gen_sample(fs_init, fs_next,
                          numpy.array(seq).T.reshape(
                              [len(seq[0]), len(seq), 1]),
                          trng=trng, k=k, maxlen=200,
                          stochastic=False, argmax=False,
                          return_alignment=return_alignment,
                          suppress_unk=suppress_unk,
                          return_hyp_graph=return_hyp_graph)


    ### WRITING TO AND READING FROM QUEUES ###

    def _send_jobs(self, input_, translation_settings):
        """
        """
        source_sentences = []
        for idx, line in enumerate(input_):
            if translation_settings.char_level:
                words = list(line.decode('utf-8').strip())
            else:
                words = line.strip().split()

            x = []
            for w in words:
                w = [self._word_dicts[i][f] if f in self._word_dicts[i] else 1 for (i,f) in enumerate(w.split('|'))]
                if len(w) != self._options[0]['factors']:
                    logging.warning('Expected {0} factors, but input word has {1}\n'.format(self._options[0]['factors'], len(w)))
                    for midx in xrange(self._num_processes):
                        self._processes[midx].terminate()
                    sys.exit(1)
                x.append(w)

            x += [[0]*self._options[0]['factors']]

            input_item = QueueItem(verbose=self._verbose,
                                   return_hyp_graph=translation_settings.get_search_graph,
                                   return_alignment=translation_settings.get_alignment,
                                   k=translation_settings.beam_width,
                                   suppress_unk=translation_settings.suppress_unk,
                                   normalization_alpha=translation_settings.normalization_alpha,
                                   nbest=translation_settings.n_best,
                                   seq=x,
                                   aux_seq=[],
                                   idx=idx,
                                   request_id=translation_settings.request_id)

            self._input_queue.put(input_item)
            source_sentences.append(words)
        return idx + 1, source_sentences

    # Multi-source version (with one auxiliary input)
    def _send_jobs_multisource(self, input_, aux_input_, translation_settings):
        """
        """
        # prepare to store in lists of inputs
        source_sentences = [[] for _ in range(len(aux_input_)+1)]

        # go through sentences (returns tuples w/ sentence for each input)
        for sidx, line in enumerate(zip(input_, *aux_input_)):

            # stock the x forms of the words (convert from dictionaries)
            xs = [[] for _ in range(len(aux_input_) + 1)]
            # stock the words of the input (for each of the inputs)
            words_s = [[] for _ in range(len(aux_input_) + 1)]

            # stock words for each of the lines
            for iidx, input in enumerate(line):
                if translation_settings.char_level:
                    words = list(input.decode('utf-8').strip())
                else:
                    words = input.strip().split()
                words_s[iidx] = words

                # stock factors
                for w in words_s[iidx]:
                    w = [self._word_dicts[iidx][j][f] if f in self._word_dicts[iidx][j] else 1 for (j, f) in enumerate(w.split('|'))]
                    if len(w) != self._options[0]['factors']:
                        logging.warning(
                            'Expected {0} factors, but input word has {1}\n'.format(self._options[0]['factors'], len(w)))
                        for midx in xrange(self._num_processes):
                            self._processes[midx].terminate()
                        sys.exit(1)
                    xs[iidx].append(w)

                xs[iidx] += [[0] * self._options[0]['factors']]
                source_sentences[iidx].append(words_s[iidx])

            input_item = QueueItem(verbose=self._verbose,
                                   return_hyp_graph=translation_settings.get_search_graph,
                                   return_alignment=translation_settings.get_alignment,
                                   k=translation_settings.beam_width,
                                   suppress_unk=translation_settings.suppress_unk,
                                   normalization_alpha=translation_settings.normalization_alpha,
                                   nbest=translation_settings.n_best,
                                   seq=xs[0],
                                   aux_seq=xs[1:],
                                   idx=sidx,
                                   request_id=translation_settings.request_id)
            self._input_queue.put(input_item)

        return sidx+1, tuple(source_sentences) #(source_sentences, source_sentences2)

    def _retrieve_jobs(self, num_samples, request_id, timeout=5):
        """
        """
        while len(self._retrieved_translations[request_id]) < num_samples:
            resp = None
            #print len(self._retrieved_translations[request_id]), num_samples
            while resp is None:
                try:
                    resp = self._output_queue.get(True, timeout)
                    #print resp
                # if queue is empty after 5s, check if processes are still alive
                except Empty:
                    for midx in xrange(self._num_processes):
                        #print 'exitcode =', self._processes[midx].exitcode
                        #print 'alive =', self._processes[midx].is_alive()
                        if not self._processes[midx].is_alive() and self._processes[midx].exitcode != 0:
                            #print "not alive and not 0"
                            # kill all other processes and raise exception if one dies
                            self._input_queue.cancel_join_thread()
                            self._output_queue.cancel_join_thread()
                            for idx in xrange(self._num_processes):
                                self._processes[idx].terminate()
                            logging.error("Translate worker process {0} crashed with exitcode {1}".format(self._processes[midx].pid, self._processes[midx].exitcode))
                            sys.exit(1)
            request_id, idx, output_item = resp
            self._retrieved_translations[request_id][idx] = output_item

        for idx in xrange(num_samples):
            yield self._retrieved_translations[request_id][idx]

        # then remove all entries with this request ID from the dictionary
        del self._retrieved_translations[request_id]


    def translate_no_queue(self, input_, aux_input_, translation_settings):

        1


    ### EXPOSED TRANSLATION FUNCTIONS ###
    # modified to use predicted translations when using previous target sentence as additional input

    def translate(self, source_segments, translation_settings, aux_source_segments=[]):
        """
        Returns the translation of @param source_segments (and @param aux_source_segments if multi-source)
        """
        logging.info('Translating {0} segments...\n'.format(len(source_segments)))
        if len(aux_source_segments) > 0:
            n_samples, multiple_source_sentences = self._send_jobs_multisource(source_segments,
                                                                               aux_source_segments,
                                                                               translation_settings)
        else:
            # TODO: make this one the generic send jobs
            n_samples, multiple_source_sentences = self._send_jobs_multisource(source_segments, [], translation_settings)

        #os.sys.stderr.write(str(translation_settings.predicted_trg)+"\n")

        translations = []

        for i, trans in enumerate(self._retrieve_jobs(n_samples, translation_settings.request_id)):

            # previous target sentence (take predicted previous sentence)
            if translation_settings.predicted_trg and aux_source_segments:
                if i==0:
                    current_aux = "<START>"
                else:
                    os.sys.stderr.write("Using previous translation...")
                    current_aux = translations[i-1]

            # just use the auxiliary input provided
            else:
                # handle potential multi-source input
                current_aux = [ss[i] for ss in multiple_source_sentences[1:]]

            samples, scores, word_probs, alignments, hyp_graph = trans

            # n-best list
            if translation_settings.n_best is True:
                order = numpy.argsort(scores)
                n_best_list = []
                for j in order:
                    current_alignment = None if not translation_settings.get_alignment else alignments[0][j]

                    aux_current_alignments = [] # list for multi-source
                    for e in range(len(self.num_encoders - 1)):
                        aux_current_alignments.append(None if not translation_settings.get_alignment else alignments[e + 1][j])

                    translation = Translation(sentence_id=i,
                                              source_words=multiple_source_sentences[0][i],
                                              target_words=seqs2words(samples[j], self._word_idict_trg, join=False),
                                              score=scores[j],
                                              alignment=current_alignment,
                                              target_probs=word_probs[j],
                                              hyp_graph=hyp_graph,
                                              hypothesis_id=j,
                                              aux_source_words=current_aux, # list of extra inputs
                                              aux_alignment=aux_current_alignments)
                    n_best_list.append(translation)
                translations.append(n_best_list)
            # single-best translation
            else:
                current_alignment = None if not translation_settings.get_alignment else alignments[0]

                aux_current_alignments = []  # list for multi-source
                for e in range(self.num_encoders - 1):
                    aux_current_alignments.append(None if not translation_settings.get_alignment else alignments[e + 1])

                translation = Translation(sentence_id=i,
                                          source_words=multiple_source_sentences[0][i],
                                          target_words=seqs2words(samples, self._word_idict_trg, join=False),
                                          score=scores,
                                          alignment=current_alignment,
                                          target_probs=word_probs,
                                          hyp_graph=hyp_graph,
                                          aux_source_words=current_aux, # list of extra inputs
                                          aux_alignment=aux_current_alignments)
                translations.append(translation)
        return translations

    def translate_file(self, input_object, translation_settings, aux_input_objects=[]):
        """
        """
        source_segments = input_object.readlines()
        # multi-source
        aux_source_segments = []

        for aux in aux_input_objects:
            aux_source_segments.append(aux.readlines())

        return self.translate(source_segments, translation_settings, aux_source_segments=aux_source_segments)


    def translate_string(self, segment, translation_settings):
        """
        Translates a single segment
        """
        if not segment.endswith('\n'):
            segment += '\n'
        source_segments = [segment]
        return self.translate(source_segments, translation_settings)

    def translate_list(self, segments, translation_settings, aux_segments=[]):
        """
        Translates a list of segments
        """
        source_segments = [s + '\n' if not s.endswith('\n') else s for s in segments]
        return self.translate(source_segments, translation_settings, aux_source_segments=aux_segments)

    ### FUNCTIONS FOR WRITING THE RESULTS ###

    def write_alignment(self, translation, translation_settings, aux_id=None):
        """
        Writes alignments to a file.
        """
        if aux_id is None:
            output_file = translation_settings.alignment_filename
        else:
            output_file = translation_settings.aux_alignment_filenames[aux_id]

        # TODO: 1 = TEXT, 2 = JSON
        if translation_settings.alignment_type == 1:
            output_file.write(translation.get_alignment_text(aux_id=aux_id) + "\n\n")
        else:
            output_file.write(translation.get_alignment_json(aux=aux_id) + "\n")

    def write_translation(self, output_file, translation, translation_settings):
        """
        Writes a single translation to a file or STDOUT.
        """
        output_items = []
        # sentence ID only for nbest
        if translation_settings.n_best is True:
            output_items.append(str(translation.sentence_id))

        # translations themselves
        output_items.append(" ".join(translation.target_words))

        # write scores for nbest?
        if translation_settings.n_best is True:
            output_items.append(str(translation.score))

        # write probabilities?
        if translation_settings.get_word_probs:
            output_items.append(translation.get_target_probs())

        if translation_settings.n_best is True:
            output_file.write(" ||| ".join(output_items) + "\n")
        else:
            output_file.write("\n".join(output_items) + "\n")

        # write alignments to file?
        if translation_settings.get_alignment:
            self.write_alignment(translation, translation_settings)

            # extra alignments for multi-source
            for i in range(self.num_encoders - 1):
                self.write_alignment(translation, translation_settings, aux_id=i)

        # construct hypgraph?
        if translation_settings.get_search_graph:
            translation.save_hyp_graph(
                                       translation_settings.search_graph_filename,
                                       self._word_idict_trg,
                                       detailed=True,
                                       highlight_best=True
            )

    def write_translations(self, output_file, translations, translation_settings):
        """
        Writes translations to a file or STDOUT.
        """
        if translation_settings.n_best is True:
            for nbest_list in translations:
                for translation in nbest_list:
                    self.write_translation(output_file, translation, translation_settings)
        else:
            for translation in translations:
                self.write_translation(output_file, translation, translation_settings)


def main(input_file, output_file, decoder_settings, translation_settings, aux_input_files=[]):
    """
    Translates a source language file (or STDIN) into a target language file
    (or STDOUT).
    """
    translator = Translator(decoder_settings)

    # set encoder number and multi-source bool
    if len(aux_input_files) > 0:
        translator.num_encoders = len(aux_input_files) + 1
        if translator._options[0]['multisource_type'] != 'init-decoder':
            translator.num_attentions = len(aux_input_files) + 1
        else:
            translator.num_attentions = 1
        translator.multisource = True
    else:
        translator.multisource = False
        translator.num_encoders = 1

    translations = translator.translate_file(input_file, translation_settings, aux_input_objects=aux_input_files)

    translator.write_translations(output_file, translations, translation_settings)

    logging.info('Done')
    translator.shutdown()


if __name__ == "__main__":
    # parse console arguments
    parser = ConsoleInterfaceDefault()
    args = parser.parse_args()
    input_file = args.input

    # multi-source
    if args.aux_input:
        aux_input_file = args.aux_input
    else:
        aux_input_file = []

    output_file = args.output
    decoder_settings = parser.get_decoder_settings()
    translation_settings = parser.get_translation_settings()
    # start logging
    level = logging.DEBUG if decoder_settings.verbose else logging.WARNING
    logging.basicConfig(level=level, format='%(levelname)s: %(message)s')
    main(input_file, output_file, decoder_settings, translation_settings, aux_input_files=aux_input_file)
