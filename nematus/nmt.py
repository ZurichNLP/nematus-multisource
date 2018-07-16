#!/usr/bin/env python
# -*- coding: utf-8 -*-
'''
Build a neural machine translation model with soft attention
'''
import theano
import theano.tensor as tensor
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams

import json
import numpy
import copy
import argparse

import os
import sys
import time
import logging

import itertools

from subprocess import Popen
from collections import OrderedDict, namedtuple

import theano.d3viz as d3v

profile = False

from data_iterator import TextIterator
from training_progress import TrainingProgress
from util import *
from theano_util import *
from alignment_util import *

from layers import *
from initializers import *
from optimizers import *
from metrics.scorer_provider import ScorerProvider

from domain_interpolation_data_iterator import DomainInterpolatorTextIterator

logger = logging.getLogger(__name__)

# batch preparation
def prepare_data(seqs_x, seqs_y, maxlen=None, n_words_src=30000,
                 n_words=30000, n_factors=1):
    # x: a list of sentences
    lengths_x = [len(s) for s in seqs_x]
    lengths_y = [len(s) for s in seqs_y]

    if maxlen is not None:
        new_seqs_x = []
        new_seqs_y = []
        new_lengths_x = []
        new_lengths_y = []
        for l_x, s_x, l_y, s_y in zip(lengths_x, seqs_x, lengths_y, seqs_y):
            if l_x < maxlen and l_y < maxlen:
                new_seqs_x.append(s_x)
                new_lengths_x.append(l_x)
                new_seqs_y.append(s_y)
                new_lengths_y.append(l_y)
        lengths_x = new_lengths_x
        seqs_x = new_seqs_x
        lengths_y = new_lengths_y
        seqs_y = new_seqs_y

        if len(lengths_x) < 1 or len(lengths_y) < 1:
            return None, None, None, None

    n_samples = len(seqs_x)
    maxlen_x = numpy.max(lengths_x) + 1
    maxlen_y = numpy.max(lengths_y) + 1

    x = numpy.zeros((n_factors, maxlen_x, n_samples)).astype('int64')
    y = numpy.zeros((maxlen_y, n_samples)).astype('int64')
    x_mask = numpy.zeros((maxlen_x, n_samples)).astype(floatX)
    y_mask = numpy.zeros((maxlen_y, n_samples)).astype(floatX)
    for idx, [s_x, s_y] in enumerate(zip(seqs_x, seqs_y)):
        x[:, :lengths_x[idx], idx] = zip(*s_x)
        x_mask[:lengths_x[idx] + 1, idx] = 1.
        y[:lengths_y[idx], idx] = s_y
        y_mask[:lengths_y[idx] + 1, idx] = 1.

    return x, x_mask, y, y_mask

# batch preparation for multi-source
# inputs are now lists of inputs
def prepare_multi_data(seqs_xs, seqs_y, maxlen=None, n_words_src=[30000], n_words=30000, n_factors=1):
    # ensure the same length for all inputs and target

    assert len(set(len(seq_x) for seq_x in list(seqs_xs) + [seqs_y])) == 1

    # get lengths (as many for each example as the number of inputs)
    lengths_xs = []
    new_seqs_xs = []
    for sx in seqs_xs:
        lengths_xs.append([len(seq) for seq in sx])
    # one output length per example
    lengths_y = [len(s) for s in seqs_y]

    if maxlen is not None:
        new_seqs_xs = [[] for _ in seqs_xs]
        new_seqs_y = []
        new_lengths_xs = [[] for _ in seqs_xs]
        new_lengths_y = []

        for i, (l_y, s_y) in enumerate(zip(lengths_y, seqs_y)):

            if l_y < maxlen and all(lx[i] < maxlen for lx in lengths_xs):
                for s in range(len(seqs_xs)):
                    new_seqs_xs[s].append(seqs_xs[s][i])
                    new_lengths_xs[s].append(lengths_xs[s][i])
                new_seqs_y.append(s_y)
                new_lengths_y.append(l_y)

        lengths_xs = new_lengths_xs
        seqs_xs = new_seqs_xs
        lengths_y = new_lengths_y
        seqs_y = new_seqs_y

        if len(lengths_y) < 1 or any(len(lengths_x) < 1 for lengths_x in lengths_xs):
            return None, None, None, None

    n_samples = len(seqs_xs[0])

    maxlen_xs = [numpy.max(lengths_x) + 1 for lengths_x in lengths_xs]
    maxlen_y = numpy.max(lengths_y) + 1

    # prepare numpy objects and masks
    xs = [[] for _ in seqs_xs]
    x_masks = [[] for _ in seqs_xs]

    for i in range(len(seqs_xs)):
        xs[i] = numpy.zeros((n_factors, maxlen_xs[i], n_samples)).astype('int64')
        x_masks[i] = numpy.zeros((maxlen_xs[i], n_samples)).astype(floatX)
    y = numpy.zeros((maxlen_y, n_samples)).astype('int64')
    y_mask = numpy.zeros((maxlen_y, n_samples)).astype(floatX)

    for idx, s_y in enumerate(seqs_y):
        for i in range(len(seqs_xs)):
            xs[i][:, :lengths_xs[i][idx], idx] = zip(*seqs_xs[i][idx])
            x_masks[i][:lengths_xs[i][idx] + 1, idx] = 1.
        y[:lengths_y[idx], idx] = s_y
        y_mask[:lengths_y[idx] + 1, idx] = 1.

    return xs, x_masks, y, y_mask


# initialize all parameters
def init_params(options):
    params = OrderedDict()

    # --------------- ENCODER(S) ---------------
    # allow for multiple encoders
    num_encoders = len(options['extra_sources']) + 1

    if not options['tie_encoder_decoder_embeddings']:
        params = get_layer_param('embedding')(options, params, options['n_words'],
                                              options['dim_word'], suffix='_dec')

    # initialise encoder for every possible encoder (for now they have have the same parameter values)
    for i in range(num_encoders):
        suff = str(i)

        # embedding
        if i > 0 and options['tie_encoder_embeddings']:
            # then reuse existing parameters
            logger.info("Skipping initialization of embedding layer for encoder %d" % i)
        else:
            # else every encoder has their own embedding parameters
            params = get_layer_param('embedding')(options, params, options['n_words_src'][i],
                                              options['dim_per_factor'], options['factors'], suffix=suff)
            logger.info("Embedding layer with n_words=%d, dim=%s, suffix=%s" % (options['n_words_src'][i], str(options['dim_per_factor']), i))

        # encoder: bidirectional RNN: same for single and multi-source
        params = get_layer_param(options['encoder'])(options, params,
                                                     prefix='encoder' + suff,
                                                     nin=options['dim_word'],
                                                     dim=options['dim'],
                                                     recurrence_transition_depth=options['enc_recurrence_transition_depth'])
        params = get_layer_param(options['encoder'])(options, params,
                                                     prefix='encoder_r' + suff,
                                                     nin=options['dim_word'],
                                                     dim=options['dim'],
                                                     recurrence_transition_depth=options['enc_recurrence_transition_depth'])

        if options['enc_depth'] > 1:
            for level in range(2, options['enc_depth'] + 1):
                prefix_f = pp('encoder', level) + suff
                prefix_r = pp('encoder_r', level) + suff

                if level <= options['enc_depth_bidirectional']:
                    params = get_layer_param(options['encoder'])(options, params,
                                                                 prefix=prefix_f,
                                                                 nin=options['dim'],
                                                                 dim=options['dim'],
                                                                 recurrence_transition_depth=options['enc_recurrence_transition_depth'])
                    params = get_layer_param(options['encoder'])(options, params,
                                                                 prefix=prefix_r,
                                                                 nin=options['dim'],
                                                                 dim=options['dim'],
                                                                 recurrence_transition_depth=options['enc_recurrence_transition_depth'])
                else:
                    params = get_layer_param(options['encoder'])(options, params,
                                                                 prefix=prefix_f,
                                                                 nin=options['dim'] * 2,
                                                                 dim=options['dim'] * 2,
                                                                 recurrence_transition_depth=options['enc_recurrence_transition_depth'])

    # Context dimension(s) - as many as there are input sources
    ctxdims = [2 * options['dim']] * (1 + len(options['extra_sources']))

    # init_state, init_cell
    params = get_layer_param('ff')(options, params, prefix='ff_state',
                                   nin=ctxdims[0], nout=options['dim'])

    # --------------- DECODER ---------------
    # use a multi-cGRU if multi-source is used
    if options['multisource_type'] is not None and len(options['extra_sources']) == 1:
        params = get_layer_param('bi_gru_cond')(options,
                                                params,
                                                prefix='decoder',
                                                nin=options['dim_word'],
                                                dim=options['dim'],
                                                dimctx=ctxdims,
                                                recurrence_transition_depth=options['dec_base_recurrence_transition_depth'])
    elif len(options['extra_sources']) == 2:
        assert options['multisource_type'] == 'att-hier'

        logger.info("Building a model with 3 inputs")
        params = get_layer_param('tri_gru_cond')(options,
                                                params,
                                                prefix='decoder',
                                                nin=options['dim_word'],
                                                dim=options['dim'],
                                                dimctx=ctxdims,
                                                recurrence_transition_depth=options['dec_base_recurrence_transition_depth'])

    else:
        params = get_layer_param(options['decoder'])(options, params,
                                                     prefix='decoder',
                                                     nin=options['dim_word'],
                                                     dim=options['dim'],
                                                     dimctx=[ctxdims[0]],
                                                     recurrence_transition_depth=options['dec_base_recurrence_transition_depth'])

    # deeper layers of the decoder
    if options['dec_depth'] > 1:
        if options['dec_deep_context']:
            input_dim = options['dim'] + ctxdims[0]
        else:
            input_dim = options['dim']

        for level in range(2, options['dec_depth'] + 1):
            params = get_layer_param(options['decoder_deep'])(options, params,
                                                              prefix=pp('decoder', level),
                                                              nin=input_dim,
                                                              dim=options['dim'],
                                                              dimctx=ctxdims[0],
                                                              recurrence_transition_depth=options[
                                                                  'dec_high_recurrence_transition_depth'])

    # --------------- READOUT ---------------
    params = get_layer_param('ff')(options, params, prefix='ff_logit_lstm',
                                   nin=options['dim'], nout=options['dim_word'],
                                   ortho=False)
    params = get_layer_param('ff')(options, params, prefix='ff_logit_prev',
                                   nin=options['dim_word'],
                                   nout=options['dim_word'], ortho=False)
    params = get_layer_param('ff')(options, params, prefix='ff_logit_ctx',
                                   nin=ctxdims[0], nout=options['dim_word'],
                                   ortho=False)

    params = get_layer_param('ff')(options, params, prefix='ff_logit',
                                   nin=options['dim_word'],
                                   nout=options['n_words'],
                                   weight_matrix=not options['tie_decoder_embeddings'],
                                   followed_by_softmax=True)

    sizes = {}

    count = 0
    for name, array in params.iteritems():
        size = array.size
        sizes[name] = size
        count += size

    logger.debug("Model has the following parameters and sizes: %s", str(sizes))
    logger.info("Total number of model parameters: %d" % count)

    return params


# bidirectional RNN encoder: take input x (optionally with mask), and produce sequence of context vectors (ctx)
def build_encoder(tparams, options, dropout, x_mask=None, sampling=False, suffix='', embedding_suffix=''):

    logger.info("Building encoder with suffix=%s, embedding_suffix=%s" % (suffix, embedding_suffix))

    x = tensor.tensor3('x' + suffix, dtype='int64')
    # source text; factors 1; length 5; batch size 10
    x.tag.test_value = (numpy.random.rand(1, 5, 10) * 100).astype('int64')

    # for the backward rnn, we just need to invert x
    xr = x[:, ::-1]
    if x_mask is None:
        xr_mask = None
    else:
        xr_mask = x_mask[::-1]

    n_timesteps = x.shape[1]
    n_samples = x.shape[2]

    # word embedding for forward rnn (source)
    emb = get_layer_constr('embedding')(tparams, x, suffix=embedding_suffix, factors=options['factors'])

    # word embedding for backward rnn (source)
    embr = get_layer_constr('embedding')(tparams, xr, suffix=embedding_suffix, factors=options['factors'])

    if options['use_dropout']:
        source_dropout = dropout((n_timesteps, n_samples, 1), options['dropout_source'])
        if not sampling:
            source_dropout = tensor.tile(source_dropout, (1, 1, options['dim_word']))
        emb *= source_dropout

        if sampling:
            embr *= source_dropout
        else:
            # we drop out the same words in both directions
            embr *= source_dropout[::-1]

    # level 1
    proj = get_layer_constr(options['encoder'])(tparams, emb, options, dropout,
                                                prefix='encoder' + suffix,
                                                mask=x_mask,
                                                dropout_probability_below=options['dropout_embedding'],
                                                dropout_probability_rec=options['dropout_hidden'],
                                                recurrence_transition_depth=options['enc_recurrence_transition_depth'],
                                                truncate_gradient=options['encoder_truncate_gradient'],
                                                profile=profile)
    projr = get_layer_constr(options['encoder'])(tparams, embr, options, dropout,
                                                 prefix='encoder_r' + suffix,
                                                 mask=xr_mask,
                                                 dropout_probability_below=options['dropout_embedding'],
                                                 dropout_probability_rec=options['dropout_hidden'],
                                                 recurrence_transition_depth=options['enc_recurrence_transition_depth'],
                                                 truncate_gradient=options['encoder_truncate_gradient'],
                                                 profile=profile)

    # bidirectional levels before merge
    for level in range(2, options['enc_depth_bidirectional'] + 1):
        prefix_f = pp('encoder', level) + suffix
        prefix_r = pp('encoder_r', level) + suffix

        # run forward on previous backward and backward on previous forward
        input_f = projr[0][::-1]
        input_r = proj[0][::-1]

        proj = get_layer_constr(options['encoder'])(tparams, input_f, options, dropout,
                                                    prefix=prefix_f,
                                                    mask=x_mask,
                                                    dropout_probability_below=options['dropout_hidden'],
                                                    dropout_probability_rec=options['dropout_hidden'],
                                                    recurrence_transition_depth=options[
                                                        'enc_recurrence_transition_depth'],
                                                    truncate_gradient=options['encoder_truncate_gradient'],
                                                    profile=profile)
        projr = get_layer_constr(options['encoder'])(tparams, input_r, options, dropout,
                                                     prefix=prefix_r,
                                                     mask=xr_mask,
                                                     dropout_probability_below=options['dropout_hidden'],
                                                     dropout_probability_rec=options['dropout_hidden'],
                                                     recurrence_transition_depth=options[
                                                         'enc_recurrence_transition_depth'],
                                                     truncate_gradient=options['encoder_truncate_gradient'],
                                                     profile=profile)

        # residual connections
        if level > 1:
            proj[0] += input_f
            projr[0] += input_r

    # context will be the concatenation of forward and backward rnns
    ctx = concatenate([proj[0], projr[0][::-1]], axis=proj[0].ndim - 1)
    ctx.tag.test_value = (numpy.random.rand(5, 10, 48) * 100).astype('int64')

    # forward encoder layers after bidirectional layers are concatenated
    for level in range(options['enc_depth_bidirectional'] + 1, options['enc_depth'] + 1):
        ctx += get_layer_constr(options['encoder'])(tparams, ctx, options, dropout,
                                                    prefix=pp('encoder', level) + suffix,
                                                    mask=x_mask,
                                                    dropout_probability_below=options['dropout_hidden'],
                                                    dropout_probability_rec=options['dropout_hidden'],
                                                    recurrence_transition_depth=options[
                                                        'enc_recurrence_transition_depth'],
                                                    truncate_gradient=options['encoder_truncate_gradient'],
                                                    profile=profile)[0]

    return x, ctx


# RNN decoder (including embedding and feedforward layer before output)
def build_decoder(tparams, options, y, ctx, init_state, dropout, x_mask=None, y_mask=None,
                  sampling=False, pctx_=None, shared_vars=None, extra_x_masks=[], extra_ctxs=[], extra_pctxs_=[]):
    opt_ret = dict()

    # multi-source: number of total encoders
    num_encoders = len(options['extra_sources']) + 1

    assert len(extra_ctxs) == (num_encoders - 1), 'Incompatible extra context provided'

    # fill with Nones
    for i in range(len(extra_ctxs) - len(extra_pctxs_)):
        extra_pctxs_.append(None)
    for i in range(len(extra_ctxs) - len(extra_x_masks)):
        extra_x_masks.append(None)

    # tell RNN whether to advance just one step at a time (for sampling) or loop through sequence (for training)
    if sampling:
        one_step = True
    else:
        one_step = False

    if options['use_dropout']:
        if sampling:
            target_dropout = dropout(dropout_probability=options['dropout_target'])
        else:
            n_timesteps_trg = y.shape[0]
            n_samples = y.shape[1]
            target_dropout = dropout((n_timesteps_trg, n_samples, 1), options['dropout_target'])
            target_dropout = tensor.tile(target_dropout, (1, 1, options['dim_word']))

    # word embedding (target), we will shift the target sequence one time step
    # to the right. This is done because of the bi-gram connections in the
    # readout and decoder rnn. The first target will be all zeros and we will
    # not condition on the last output.
    decoder_embedding_suffix = '' if options['tie_encoder_decoder_embeddings'] else '_dec'
    emb = get_layer_constr('embedding')(tparams, y, suffix=decoder_embedding_suffix)
    if options['use_dropout']:
        emb *= target_dropout

    if sampling:
        emb = tensor.switch(y[:, None] < 0,
                            tensor.zeros((1, options['dim_word'])), emb)
    else:
        emb_shifted = tensor.zeros_like(emb)
        emb_shifted = tensor.set_subtensor(emb_shifted[1:], emb[:-1])
        emb = emb_shifted

    # decoder - pass through the decoder conditional gru with attention
    if options['multisource_type'] not in (None, 'init-decoder') and num_encoders == 2:
        proj = get_layer_constr('bi_gru_cond')(tparams, emb, options, dropout,
                                                  prefix='decoder',
                                                  mask=y_mask, context=ctx,
                                                  context_mask=x_mask,
                                                  pctx_=pctx_,
                                                  one_step=one_step,
                                                  init_state=init_state[0],
                                                  recurrence_transition_depth=options['dec_base_recurrence_transition_depth'],
                                                  dropout_probability_below=options['dropout_embedding'],
                                                  dropout_probability_ctx=options['dropout_hidden'],
                                                  dropout_probability_rec=options['dropout_hidden'],
                                                  truncate_gradient=options['decoder_truncate_gradient'],
                                                  profile=profile,
                                                  extra_context=extra_ctxs[0],
                                                  extra_context_mask=extra_x_masks[0],
                                                  extra_pctx_=extra_pctxs_[0])

    elif num_encoders == 3:
        assert options['multisource_type'] == 'att-hier', 'The attention combination is not compatible with 3 inputs'
        proj = get_layer_constr('tri_gru_cond')(tparams, emb, options, dropout,
                                               prefix='decoder',
                                               mask=y_mask, context=ctx,
                                               context_mask=x_mask,
                                               pctx_=pctx_,
                                               one_step=one_step,
                                               init_state=init_state[0],
                                               recurrence_transition_depth=options['dec_base_recurrence_transition_depth'],
                                               dropout_probability_below=options['dropout_embedding'],
                                               dropout_probability_ctx=options['dropout_hidden'],
                                               dropout_probability_rec=options['dropout_hidden'],
                                               truncate_gradient=options['decoder_truncate_gradient'],
                                               profile=profile,
                                               extra_context1=extra_ctxs[0], extra_context2=extra_ctxs[1],
                                               extra_context_mask1=extra_x_masks[0], extra_context_mask2=extra_x_masks[1],
                                               extra_pctx1_=extra_pctxs_[0], extra_pctx2_=extra_pctxs_[1])

    else:

        #logger.info("Building a single-source model")

        proj = get_layer_constr(options['decoder'])(tparams, emb, options, dropout,
                                                    prefix='decoder',
                                                    mask=y_mask, context=ctx,
                                                    context_mask=x_mask,
                                                    pctx_=pctx_,
                                                    one_step=one_step,
                                                    init_state=init_state[0],
                                                    recurrence_transition_depth=options['dec_base_recurrence_transition_depth'],
                                                    dropout_probability_below=options['dropout_embedding'],
                                                    dropout_probability_ctx=options['dropout_hidden'],
                                                    dropout_probability_rec=options['dropout_hidden'],
                                                    truncate_gradient=options['decoder_truncate_gradient'],
                                                    profile=profile)
    # hidden states of the decoder gru
    next_state = proj[0]

    # weighted averages of context, generated by attention module
    ctxs = proj[1]

    # weights (alignment matrix)
    opt_ret['dec_alphas0'] = proj[2]
    if options['multisource_type'] not in (None, 'init-decoder'):
        for i in range(num_encoders - 1):
            opt_ret['dec_alphas'+str(i+1)] = proj[3+i]  # auxiliary

        # hierarchical attention weights? (might not be specified)
        if len(proj)> 2+num_encoders:
            opt_ret['hier_alpha'] = proj[3+num_encoders]

    # we return state of each layer
    if sampling:
        ret_state = [next_state.reshape((1, next_state.shape[0], next_state.shape[1]))]
    else:
        ret_state = None

    if options['dec_depth'] > 1:
        for level in range(2, options['dec_depth'] + 1):

            if options['dec_deep_context']:
                if sampling:
                    axis = 1
                else:
                    axis = 2
                input_ = tensor.concatenate([next_state, ctxs], axis=axis)
            else:
                input_ = next_state

            if options['multisource_type'] not in (None, 'init-decoder'):
                out_state = get_layer_constr('bi_gru_cond')(tparams, input_, options, dropout,
                                                               prefix=pp('decoder', level),
                                                               mask=y_mask,
                                                               context=ctx,
                                                               context_mask=x_mask,
                                                               pctx_=None,
                                                               # TODO: we can speed up sampler by precomputing this
                                                               one_step=one_step,
                                                               init_state=init_state[level - 1],
                                                               dropout_probability_below=options['dropout_hidden'],
                                                               dropout_probability_rec=options['dropout_hidden'],
                                                               recurrence_transition_depth=options[
                                                                   'dec_high_recurrence_transition_depth'],
                                                               truncate_gradient=options['decoder_truncate_gradient'],
                                                               profile=profile,
                                                               aux_context=extra_ctxs,
                                                               aux_context_mask=extra_x_masks,
                                                               extra_pctxs_=extra_pctxs_)[0]
            else:
                out_state = get_layer_constr(options['decoder_deep'])(tparams, input_, options, dropout,
                                                                      prefix=pp('decoder', level),
                                                                      mask=y_mask,
                                                                      context=ctx,
                                                                      context_mask=x_mask,
                                                                      pctx_=None,
                                                                      # TODO: we can speed up sampler by precomputing this
                                                                      one_step=one_step,
                                                                      init_state=init_state[level - 1],
                                                                      dropout_probability_below=options[
                                                                          'dropout_hidden'],
                                                                      dropout_probability_rec=options['dropout_hidden'],
                                                                      recurrence_transition_depth=options[
                                                                          'dec_high_recurrence_transition_depth'],
                                                                      truncate_gradient=options[
                                                                          'decoder_truncate_gradient'],
                                                                      profile=profile)[0]

            if sampling:
                ret_state.append(out_state.reshape((1, next_state.shape[0], next_state.shape[1])))

            # residual connection
            next_state += out_state

    if sampling:
        if options['dec_depth'] > 1:
            ret_state = tensor.concatenate(ret_state, axis=0)
        else:
            ret_state = ret_state[0]

    # hidden layer taking RNN state, previous word embedding and context vector as input
    # (this counts as the first layer in our deep output, which is always on)
    logit_lstm = get_layer_constr('ff')(tparams, next_state, options, dropout,
                                        dropout_probability=options['dropout_hidden'],
                                        prefix='ff_logit_lstm', activ='linear')
    logit_prev = get_layer_constr('ff')(tparams, emb, options, dropout,
                                        dropout_probability=options['dropout_embedding'],
                                        prefix='ff_logit_prev', activ='linear')
    logit_ctx = get_layer_constr('ff')(tparams, ctxs, options, dropout,
                                       dropout_probability=options['dropout_hidden'],
                                       prefix='ff_logit_ctx', activ='linear')
    logit = tensor.tanh(logit_lstm + logit_prev + logit_ctx)

    # last layer
    logit_W = tparams['Wemb' + decoder_embedding_suffix].T if options['tie_decoder_embeddings'] else None
    logit = get_layer_constr('ff')(tparams, logit, options, dropout,
                                   dropout_probability=options['dropout_hidden'],
                                   prefix='ff_logit', activ='linear', W=logit_W, followed_by_softmax=True)

    return logit, opt_ret, ret_state


def build_model(tparams, options):
    trng = RandomStreams(1234)
    use_noise = theano.shared(numpy_floatX(0.))
    dropout = dropout_constr(options, use_noise, trng, sampling=False)

    x_mask = tensor.matrix('x_mask', dtype=floatX)
    y = tensor.matrix('y', dtype='int64')
    y_mask = tensor.matrix('y_mask', dtype=floatX)
    # source text length 5; batch size 10
    x_mask.tag.test_value = numpy.ones(shape=(5, 10)).astype(floatX)
    # target text length 8; batch size 10
    y.tag.test_value = (numpy.random.rand(8, 10) * 100).astype('int64')
    y_mask.tag.test_value = numpy.ones(shape=(8, 10)).astype(floatX)

    x, ctx = build_encoder(tparams, options, dropout, x_mask, sampling=False)
    n_samples = x.shape[2]

    # mean of the context (across time) will be used to initialize decoder rnn
    ctx_mean = (ctx * x_mask[:, :, None]).sum(0) / x_mask.sum(0)[:, None]

    # or you can use the last state of forward + backward encoder rnns
    # ctx_mean = concatenate([proj[0][-1], projr[0][-1]], axis=proj[0].ndim-2)

    # initial decoder state
    init_state = get_layer_constr('ff')(tparams, ctx_mean, options, dropout,
                                        dropout_probability=options['dropout_hidden'],
                                        prefix='ff_state', activ='tanh')

    # every decoder RNN layer gets its own copy of the init state
    init_state = init_state.reshape([1, init_state.shape[0], init_state.shape[1]])
    if options['dec_depth'] > 1:
        init_state = tensor.tile(init_state, (options['dec_depth'], 1, 1))

    logit, opt_ret, _ = build_decoder(tparams, options, y, ctx, init_state, dropout, x_mask=x_mask, y_mask=y_mask,
                                      sampling=False)

    logit_shp = logit.shape
    probs = tensor.nnet.softmax(logit.reshape([logit_shp[0] * logit_shp[1],
                                               logit_shp[2]]))

    # cost
    y_flat = y.flatten()
    y_flat_idx = tensor.arange(y_flat.shape[0]) * options['n_words'] + y_flat
    cost = -tensor.log(probs.flatten()[y_flat_idx])

    cost = cost.reshape([y.shape[0], y.shape[1]])
    # per word cost
    opt_ret['cost_per_word'] = cost * y_mask
    cost = (cost * y_mask).sum(0)

    return trng, use_noise, x, x_mask, y, y_mask, opt_ret, cost


# build a training model
def build_multisource_model(tparams, options):
    logger.info("Building multi-source model")

    # get total number of encoders (multi-source)
    num_encoders = len(options['extra_sources']) + 1

    trng = RandomStreams(1234)
    use_noise = theano.shared(numpy_floatX(0.))
    dropout = dropout_constr(options, use_noise, trng, sampling=False)

    # deal with outputs first
    y = tensor.matrix('y', dtype='int64')
    y_mask = tensor.matrix('y_mask', dtype=floatX)

    # target text length 8; batch size 10
    y.tag.test_value = (numpy.random.rand(8, 10) * 100).astype('int64')
    y_mask.tag.test_value = numpy.ones(shape=(8, 10)).astype(floatX)

    # store all inputs as lists for multi-source compatibility
    xs = [[] for _ in range(num_encoders)]
    x_masks = [[] for _ in range(num_encoders)]
    ctxs = [[] for _ in range(num_encoders)]
    n_samples = [[] for _ in range(num_encoders)]
    ctx_means = [[] for _ in range(num_encoders)]

    # ------------ encoder(s) ------------
    for i in range(num_encoders):
        if i > 0 and options['tie_encoder_embeddings']:
            # then figure out type
            type = options['extra_sources_types'][i-1]
            if type == "s":
                # extra source-side encoder
                embedding_suffix = "0"
            else:
                # extra target-side encoder
                embedding_suffix = "_dec"
        else:
            embedding_suffix = str(i)

        suffix = str(i)

        x_masks[i] = tensor.matrix('x_mask' + suffix, dtype=floatX)
        # source text length 5; batch size 10
        x_masks[i].tag.test_value = numpy.ones(shape=(5, 10)).astype(floatX)

        xs[i], ctxs[i] = build_encoder(tparams, options, dropout, x_masks[i], sampling=False, suffix=suffix, embedding_suffix=embedding_suffix)

        n_samples[i] = xs[i].shape[2]
        # mean of the context (across time) will be used to initialize decoder rnn
        ctx_means[i] = (ctxs[i] * x_masks[i][:, :, None]).sum(0) / x_masks[i].sum(0)[:, None]
        # or you can use the last state of forward + backward encoder rnns
        # ctx_mean = concatenate([proj[0][-1], projr[0][-1]], axis=proj[0].ndim-2)

    # utility function to look up parameters and apply weight normalization if enabled
    def wn(param_name):
        param = tparams[param_name]
        if options['weight_normalisation']:
            return weight_norm(param, tparams[param_name + '_wns'])
        else:
            return param

    # ------------ DECODER ------------
    # initial decoder state
    # different ways of combining the two attention mechanisms
    if options['multisource_type'] in ('att-concat', 'att-gate', 'att-hier'):
        # mean of contexts
        ctx_mean_combo = numpy.sum(ctx_means)/len(ctx_means)

    # initialise decoder state with auxiliary context
    elif options['multisource_type'] == 'init-decoder':
        ctx_mean_combo = numpy.sum(ctx_means)/len(ctx_means)#ctx_means[1]

    else:
        assert len(ctx_means) == 1, 'you must specify a multi-source type compatible with build_multisource_model()'
        ctx_mean_combo = ctx_means[0]

    # initial decoder state
    init_state = get_layer_constr('ff')(tparams, ctx_mean_combo, options, dropout,
                                        dropout_probability=options['dropout_hidden'],
                                        prefix='ff_state', activ='tanh')

    # every decoder RNN layer gets its own copy of the init state
    init_state = init_state.reshape([1, init_state.shape[0], init_state.shape[1]])
    if options['dec_depth'] > 1:
        init_state = tensor.tile(init_state, (options['dec_depth'], 1, 1))

    # build decoder
    logit, opt_ret, _ = build_decoder(tparams, options, y, ctxs[0], init_state, dropout,
                                      x_mask=x_masks[0], y_mask=y_mask, sampling=False,
                                      extra_x_masks=x_masks[1:], extra_ctxs=ctxs[1:])

    # ------------ OUTPUT LAYERS ------------
    logit_shp = logit.shape
    probs = tensor.nnet.softmax(logit.reshape([logit_shp[0] * logit_shp[1],
                                               logit_shp[2]]))

    # ------------ COST ------------
    y_flat = y.flatten()
    y_flat_idx = tensor.arange(y_flat.shape[0]) * options['n_words'] + y_flat
    cost = -tensor.log(probs.flatten()[y_flat_idx])

    cost = cost.reshape([y.shape[0], y.shape[1]])
    # per word cost
    opt_ret['cost_per_word'] = cost * y_mask
    cost = (cost * y_mask).sum(0)

    return trng, use_noise, xs, x_masks, y, y_mask, opt_ret, cost


# build a multi-sampler
def build_multi_sampler(tparams, options, use_noise, trng, return_alignment=False):
    # potentially multiple inputs, stored in 'encoders'

    num_encoders = len(options['extra_sources']) + 1

    dropout = dropout_constr(options, use_noise, trng, sampling=True)

    xs = [[]] * num_encoders
    ctxs = [[]] * num_encoders
    ctx_means = [[]] * num_encoders

    # build each of the encoders (first is main one and following ones are auxiliary ones)
    for i in range(num_encoders):
        if i > 0 and options['tie_encoder_embeddings']:
            # then figure out type
            type = options['extra_sources_types'][i-1]
            if type == "s":
                # extra source-side encoder
                embedding_suffix = "0"
            else:
                # extra target-side encoder
                embedding_suffix = "_dec"
        else:
            embedding_suffix = str(i)

        suffix = str(i)

        xs[i], ctxs[i] = build_encoder(tparams, options, dropout, x_mask=None, sampling=True, suffix=suffix, embedding_suffix=embedding_suffix)
        n_samples = xs[i].shape[2]
        # get the input for decoder rnn initializer mlp
        ctx_means[i] = ctxs[i].mean(0)
        # ctx_mean = concatenate([proj[0][-1],projr[0][-1]], axis=proj[0].ndim-2)

    # combine the contexts for initialisation by mean of context
    if options['multisource_type'] == 'init-decoder':
        logger.info("using aux context to initialise decoder")
        ctx_mean = ctx_means[1]
    else:
        ctx_mean = sum(ctx_means)/len(ctx_means)

    init_state = get_layer_constr('ff')(tparams, ctx_mean, options, dropout,
                                        dropout_probability=options['dropout_hidden'],
                                        prefix='ff_state', activ='tanh')

    # every decoder RNN layer gets its own copy of the init state
    init_state = init_state.reshape([1, init_state.shape[0], init_state.shape[1]])
    if options['dec_depth'] > 1:
        init_state = tensor.tile(init_state, (options['dec_depth'], 1, 1))

    logger.info('Building f_init...')
    inps = xs
    outs = [init_state] + ctxs
    f_init = theano.function(inps, outs, name='f_init', profile=profile)
    logger.info('Done')

    # x: 1 x 1
    y = tensor.vector('y_sampler', dtype='int64')
    y.tag.test_value = -1 * numpy.ones((10,)).astype('int64')
    init_state_old = init_state
    init_state = tensor.tensor3('init_state', dtype=floatX)
    if theano.config.compute_test_value != 'off':
        init_state.tag.test_value = numpy.random.rand(*init_state_old.tag.test_value.shape).astype(floatX)

    logit, opt_ret, ret_state = build_decoder(tparams, options, y, ctxs[0], init_state, dropout,
                                              x_mask=None, y_mask=None, sampling=True,
                                              extra_x_masks=[], extra_ctxs=ctxs[1:])

    # compute the softmax probability
    next_probs = tensor.nnet.softmax(logit)

    # sample from softmax distribution to get the sample
    next_sample = trng.multinomial(pvals=next_probs).argmax(1)

    # compile a function to do the whole thing above, next word probability,
    # sampled word for the next target, next hidden state to be used

    logger.info('Building f_next...')

    if options['multisource_type'] == 'init-decoder':
        inps = [y, ctxs[0], init_state]
    else:
        inps = [y] + ctxs + [init_state]
    outs = [next_probs, next_sample, ret_state]

    if return_alignment:
        for i in range(num_encoders):
            if 'dec_alphas' + str(i) in opt_ret:
                outs.append(opt_ret['dec_alphas' + str(i)])

    f_next = theano.function(inps, outs, name='f_next', profile=profile, on_unused_input='warn')
    logger.info('Done')

    return f_init, f_next


# build a sampler
def build_sampler(tparams, options, use_noise, trng, return_alignment=False):
    dropout = dropout_constr(options, use_noise, trng, sampling=True)

    x, ctx = build_encoder(tparams, options, dropout, x_mask=None, sampling=True)
    n_samples = x.shape[2]

    # get the input for decoder rnn initializer mlp
    ctx_mean = ctx.mean(0)
    # ctx_mean = concatenate([proj[0][-1],projr[0][-1]], axis=proj[0].ndim-2)

    init_state = get_layer_constr('ff')(tparams, ctx_mean, options, dropout,
                                        dropout_probability=options['dropout_hidden'],
                                        prefix='ff_state', activ='tanh')

    # every decoder RNN layer gets its own copy of the init state
    init_state = init_state.reshape([1, init_state.shape[0], init_state.shape[1]])
    if options['dec_depth'] > 1:
        init_state = tensor.tile(init_state, (options['dec_depth'], 1, 1))

    logger.info('Building f_init...')
    outs = [init_state, ctx]
    f_init = theano.function([x], outs, name='f_init', profile=profile)
    logger.info('Done')

    # x: 1 x 1
    y = tensor.vector('y_sampler', dtype='int64')
    y.tag.test_value = -1 * numpy.ones((10,)).astype('int64')
    init_state_old = init_state
    init_state = tensor.tensor3('init_state', dtype=floatX)
    if theano.config.compute_test_value != 'off':
        init_state.tag.test_value = numpy.random.rand(*init_state_old.tag.test_value.shape).astype(floatX)

    logit, opt_ret, ret_state = build_decoder(tparams, options, y, ctx, init_state, dropout, x_mask=None, y_mask=None,
                                              sampling=True)

    # compute the softmax probability
    next_probs = tensor.nnet.softmax(logit)

    # sample from softmax distribution to get the sample
    next_sample = trng.multinomial(pvals=next_probs).argmax(1)

    # compile a function to do the whole thing above, next word probability,
    # sampled word for the next target, next hidden state to be used
    logger.info('Building f_next..')
    inps = [y, ctx, init_state]
    outs = [next_probs, next_sample, ret_state]

    if return_alignment:
        for i in range(len(options['extra_sources'])+1):
            outs.append(opt_ret['dec_alphas'+str(i)])

    f_next = theano.function(inps, outs, name='f_next', profile=profile)
    logger.info('Done')

    return f_init, f_next


# minimum risk cost
# assumes cost is the negative sentence-level log probability
# and each sentence in the minibatch is a sample of the same source sentence
def mrt_cost(cost, y_mask, options):
    loss = tensor.vector('loss', dtype=floatX)
    alpha = theano.shared(numpy_floatX(options['mrt_alpha']))

    if options['mrt_ml_mix'] > 0:
        ml_cost = cost[0]

        # remove reference for MRT objective unless enabled
        if not options['mrt_reference']:
            cost = cost[1:]

    cost *= alpha

    # get normalized probability
    cost = tensor.nnet.softmax(-cost)[0]

    # risk: expected loss
    if options['mrt_ml_mix'] > 0 and not options['mrt_reference']:
        cost *= loss[1:]
    else:
        cost *= loss

    cost = cost.sum()

    if options['mrt_ml_mix'] > 0:
        # normalize ML by length (because MRT is length-invariant)
        ml_cost /= y_mask[:, 0].sum(0)
        ml_cost *= options['mrt_ml_mix']
        cost += ml_cost

    return cost, loss


# build a sampler that produces samples in one theano function
def build_full_sampler(tparams, options, use_noise, trng, greedy=False):
    logger.info('Building full sampler')

    dropout = dropout_constr(options, use_noise, trng, sampling=True)

    if greedy:
        x_mask = tensor.matrix('x_mask', dtype=floatX)
        x_mask.tag.test_value = numpy.ones(shape=(5, 10)).astype(floatX)
    else:
        x_mask = None

    x, ctx = build_encoder(tparams, options, dropout, x_mask, sampling=True)
    n_samples = x.shape[2]

    if x_mask:
        ctx_mean = (ctx * x_mask[:, :, None]).sum(0) / x_mask.sum(0)[:, None]
    else:
        ctx_mean = ctx.mean(0)

    init_state = get_layer_constr('ff')(tparams, ctx_mean, options, dropout,
                                        dropout_probability=options['dropout_hidden'],
                                        prefix='ff_state', activ='tanh')

    # every decoder RNN layer gets its own copy of the init state
    init_state = init_state.reshape([1, init_state.shape[0], init_state.shape[1]])
    if options['dec_depth'] > 1:
        init_state = tensor.tile(init_state, (options['dec_depth'], 1, 1))

    if greedy:
        init_w = tensor.alloc(numpy.int64(-1), n_samples)
    else:
        k = tensor.iscalar("k")
        k.tag.test_value = 12
        init_w = tensor.alloc(numpy.int64(-1), k * n_samples)

        ctx = tensor.tile(ctx, [k, 1])

        init_state = tensor.tile(init_state, [1, k, 1])

    # projected context
    assert ctx.ndim == 3, 'Context must be 3-d: #annotation x #sample x dim'
    pctx_ = tensor.dot(ctx * dropout(dropout_probability=options['dropout_hidden']), tparams[pp('decoder', 'Wc_att')]) + \
            tparams[pp('decoder', 'b_att')]

    def decoder_step(y, init_state, ctx, pctx_, *shared_vars):

        logit, opt_ret, ret_state = build_decoder(tparams, options, y, ctx, init_state, dropout, x_mask=x_mask,
                                                  y_mask=None, sampling=True, pctx_=pctx_, shared_vars=shared_vars)

        # compute the softmax probability
        next_probs = tensor.nnet.softmax(logit)

        if greedy:
            next_sample = next_probs.argmax(1)
        else:
            # sample from softmax distribution to get the sample
            next_sample = trng.multinomial(pvals=next_probs).argmax(1)

        # do not produce words after EOS
        next_sample = tensor.switch(
            tensor.eq(y, 0),
            0,
            next_sample)

        return [next_sample, ret_state, next_probs[:, next_sample].diagonal()], \
               theano.scan_module.until(tensor.all(tensor.eq(next_sample, 0)))  # stop when all outputs are 0 (EOS)

    decoder_prefixes = ['decoder']
    if options['dec_depth'] > 1:
        for level in range(2, options['dec_depth'] + 1):
            decoder_prefixes.append(pp('decoder', level))

    shared_vars = []
    for prefix in decoder_prefixes:
        shared_vars.extend([tparams[pp(prefix, 'U')],
                            tparams[pp(prefix, 'Wc')],
                            tparams[pp(prefix, 'U_att')],
                            tparams[pp(prefix, 'c_tt')],
                            tparams[pp(prefix, 'Ux')],
                            tparams[pp(prefix, 'Wcx')],
                            tparams[pp(prefix, 'U_nl')],
                            tparams[pp(prefix, 'Ux_nl')],
                            tparams[pp(prefix, 'b_nl')],
                            tparams[pp(prefix, 'bx_nl')]])

        if options['multisource_type'] == 'att-gate' or options['multisource_type'] == 'att-gate2':
            shared_vars.append(tparams[pp(prefix, 'W_comb_att')])


    n_steps = tensor.iscalar("n_steps")
    n_steps.tag.test_value = 50

    (sample, state, probs), updates = theano.scan(decoder_step,
                                                  outputs_info=[init_w, init_state, None],
                                                  non_sequences=[ctx, pctx_] + shared_vars,
                                                  n_steps=n_steps,
                                                  truncate_gradient=options['decoder_truncate_gradient'])

    logger.info('Building f_sample...')
    if greedy:
        inps = [x, x_mask, n_steps]
    else:
        inps = [x, k, n_steps]
    outs = [sample, probs]
    f_sample = theano.function(inps, outs, name='f_sample', updates=updates, profile=profile)
    logger.info('Done')

    return f_sample


# TODO: generic multi-source
# TODO: just do two for now
# generate sample, either with stochastic sampling or beam search. Note that
# this function iteratively calls f_init and f_next functions.
def gen_sample(f_init, f_next, x, trng=None, k=1, maxlen=30,
               stochastic=True, argmax=False, return_alignment=False, suppress_unk=False,
               return_hyp_graph=False, extra_xs=[], init_decoder=False):
    # k is the beam size we have
    if k > 1 and argmax:
        assert not stochastic, \
            'Beam search does not support stochastic sampling with argmax'

    # collapse inputs to one list for ease of looping
    if len(extra_xs) == 0:
        xs = [x]
    else:
        xs = [x] + extra_xs

    assert len(xs) <= 3, 'Only accepting up to 2 extra sources for now'

    sample = []
    sample_score = []
    sample_word_probs = []
    alignments = [[] for _ in xs] # list for multi-source
    hyp_graph = None
    if stochastic:
        if argmax:
            sample_score = 0
        live_k = k
    else:
        live_k = 1

    if return_hyp_graph:
        from hypgraph import HypGraph
        hyp_graph = HypGraph()

    dead_k = 0

    hyp_samples = [[] for i in xrange(live_k)]
    word_probs = [[] for i in xrange(live_k)]
    hyp_scores = numpy.zeros(live_k).astype(floatX)
    hyp_states = []
    if return_alignment:
        hyp_alignment = [] # list for multi-source
        for _ in xs:
            hyp_alignment.append([[] for _ in xrange(live_k)])

    # for ensemble decoding, we keep track of states and probability distribution
    # for each model in the ensemble
    num_models = len(f_init)
    next_state = [None] * num_models
    next_p = [None] * num_models

    # multi-source (at least 2 attention mechanisms)
    ctx = [] # list for multi-source
    dec_alphas = [] # list for multi-source
    for _ in xs:
        ctx.append([None] * num_models)
        if not init_decoder:
            dec_alphas.append([None] * num_models)

    if init_decoder:
        dec_alphas = [[[None] * num_models]]

    # get initial state of decoder rnn and encoder context
    for i in xrange(num_models):
        inps = xs

        ret = f_init[i](*inps)

        # to more easily manipulate batch size, go from (layers, batch_size, dim) to (batch_size, layers, dim)
        ret[0] = numpy.transpose(ret[0], (1, 0, 2))

        next_state[i] = numpy.tile(ret[0], (live_k, 1, 1))

        # assign each output to ctx
        for inputnum in range(len(xs)):
            ctx[inputnum][i] = ret[inputnum+1]

    next_w = -1 * numpy.ones((live_k,)).astype('int64')  # bos indicator

    # x is a sequence of word ids followed by 0, eos id
    for ii in xrange(maxlen):
        for i in xrange(num_models):
            ctxs = []
            for inputnum in range(len(xs)):
                ctxs.append(numpy.tile(ctx[inputnum][i], [live_k, 1]))

            # for theano function, go from (batch_size, layers, dim) to (layers, batch_size, dim)
            next_state[i] = numpy.transpose(next_state[i], (1, 0, 2))

            if init_decoder:
                inps = [next_w, ctxs[0], next_state[i]]
            else:
                inps = [next_w] + ctxs + [next_state[i]]

            ret = f_next[i](*inps)

            # TODO: do multi-souce from here!
            # dimension of dec_alpha (k-beam-size, number-of-input-hidden-units)
            next_p[i], next_w_tmp, next_state[i] = ret[0], ret[1], ret[2]
            if return_alignment:
                for inputnum in range(len(xs)):
                    dec_alphas[inputnum][i] = ret[3 + inputnum]

            # to more easily manipulate batch size, go from (layers, batch_size, dim) to (batch_size, layers, dim)
            next_state[i] = numpy.transpose(next_state[i], (1, 0, 2))

            if suppress_unk:
                next_p[i][:, 1] = -numpy.inf
        if stochastic:
            # batches are not supported with argmax: output data structure is different
            if argmax:
                nw = sum(next_p)[0].argmax()
                sample.append(nw)
                sample_score += numpy.log(next_p[0][0, nw])
                if nw == 0:
                    break
            else:
                # FIXME: sampling is currently performed according to the last model only
                nws = next_w_tmp
                cand_scores = numpy.array(hyp_scores)[:, None] - numpy.log(next_p[-1])
                probs = next_p[-1]

                for idx, nw in enumerate(nws):
                    hyp_samples[idx].append(nw)

                hyp_states = []
                for ti in xrange(live_k):
                    hyp_states.append([copy.copy(next_state[i][ti]) for i in xrange(num_models)])
                    hyp_scores[ti] = cand_scores[ti][nws[ti]]
                    word_probs[ti].append(probs[ti][nws[ti]])

                new_hyp_states = []
                new_hyp_samples = []
                new_hyp_scores = []
                new_word_probs = []
                for hyp_sample, hyp_state, hyp_score, hyp_word_prob in zip(hyp_samples, hyp_states, hyp_scores,
                                                                           word_probs):
                    if hyp_sample[-1] > 0:
                        new_hyp_samples.append(copy.copy(hyp_sample))
                        new_hyp_states.append(copy.copy(hyp_state))
                        new_hyp_scores.append(hyp_score)
                        new_word_probs.append(hyp_word_prob)
                    else:
                        sample.append(copy.copy(hyp_sample))
                        sample_score.append(hyp_score)
                        sample_word_probs.append(hyp_word_prob)

                hyp_samples = new_hyp_samples
                hyp_states = new_hyp_states
                hyp_scores = new_hyp_scores
                word_probs = new_word_probs

                live_k = len(hyp_samples)
                if live_k < 1:
                    break

                next_w = numpy.array([w[-1] for w in hyp_samples])
                next_state = [numpy.array(state) for state in zip(*hyp_states)]
        else:
            cand_scores = hyp_scores[:, None] - sum(numpy.log(next_p))
            probs = sum(next_p) / num_models
            cand_flat = cand_scores.flatten()
            probs_flat = probs.flatten()
            ranks_flat = cand_flat.argpartition(k - dead_k - 1)[:(k - dead_k)]

            # averaging the attention weights accross models
            if return_alignment:
                mean_alignment = []
                for inputnum in range(len(xs)):
                    mean_alignment.append(sum(dec_alphas[inputnum]) / num_models)

            voc_size = next_p[0].shape[1]
            # index of each k-best hypothesis
            trans_indices = ranks_flat / voc_size
            word_indices = ranks_flat % voc_size
            costs = cand_flat[ranks_flat]

            new_hyp_samples = []
            new_hyp_scores = numpy.zeros(k - dead_k).astype(floatX)
            new_word_probs = []
            new_hyp_states = []

            # TODO: do this for multi-source too!
            if return_alignment:
                # holds the history of attention weights for each time step for each of the surviving hypothesis
                # dimensions (live_k * target_words * source_hidden_units]
                # at each time step we append the attention weights corresponding to the current target word
                new_hyp_alignment = []
                for _ in xs:
                    new_hyp_alignment.append([[] for _ in xrange(k - dead_k)])

            # ti -> index of k-best hypothesis
            for idx, [ti, wi] in enumerate(zip(trans_indices, word_indices)):
                new_hyp_samples.append(hyp_samples[ti] + [wi])
                new_word_probs.append(word_probs[ti] + [probs_flat[ranks_flat[idx]].tolist()])
                new_hyp_scores[idx] = copy.copy(costs[idx])
                new_hyp_states.append([copy.copy(next_state[i][ti]) for i in xrange(num_models)])
                if return_alignment:
                    # get history of attention weights for the current hypothesis

                    for inputnum in range(len(xs)):
                        new_hyp_alignment[inputnum][idx] = copy.copy(hyp_alignment[inputnum][ti])
                        # extend the history with current attention weights
                        new_hyp_alignment[inputnum][idx].append(mean_alignment[inputnum][ti])

            # check the finished samples
            new_live_k = 0
            hyp_samples = []
            hyp_scores = []
            hyp_states = []
            word_probs = []
            if return_alignment:
                hyp_alignment = [[] for _ in xs]

            # sample and sample_score hold the k-best translations and their scores
            for idx in xrange(len(new_hyp_samples)):
                if return_hyp_graph:
                    word, history = new_hyp_samples[idx][-1], new_hyp_samples[idx][:-1]
                    score = new_hyp_scores[idx]
                    word_prob = new_word_probs[idx][-1]
                    hyp_graph.add(word, history, word_prob=word_prob, cost=score)
                if new_hyp_samples[idx][-1] == 0:
                    sample.append(copy.copy(new_hyp_samples[idx]))
                    sample_score.append(new_hyp_scores[idx])
                    sample_word_probs.append(new_word_probs[idx])
                    if return_alignment:
                        for inputnum in range(len(xs)):
                            alignments[inputnum].append(new_hyp_alignment[inputnum][idx])
                    dead_k += 1
                else:
                    new_live_k += 1
                    hyp_samples.append(copy.copy(new_hyp_samples[idx]))
                    hyp_scores.append(new_hyp_scores[idx])
                    hyp_states.append(copy.copy(new_hyp_states[idx]))
                    word_probs.append(new_word_probs[idx])
                    if return_alignment:
                        for inputnum in range(len(xs)):
                            hyp_alignment[inputnum].append(new_hyp_alignment[inputnum][idx])

            hyp_scores = numpy.array(hyp_scores)

            live_k = new_live_k

            if new_live_k < 1:
                break
            if dead_k >= k:
                break

            next_w = numpy.array([w[-1] for w in hyp_samples])
            next_state = [numpy.array(state) for state in zip(*hyp_states)]

    # dump every remaining one
    if not argmax and live_k > 0:
        for idx in xrange(live_k):
            sample.append(hyp_samples[idx])
            sample_score.append(hyp_scores[idx])
            sample_word_probs.append(word_probs[idx])
            if return_alignment:
                for inputnum in range(len(xs)):
                    alignments[inputnum].append(hyp_alignment[inputnum][idx])

    if not return_alignment:
        alignments = []
        for _ in xs:
            alignments.append([None for i in range(len(sample))])

    return sample, sample_score, sample_word_probs, alignments, hyp_graph


# calculate the log probablities on a given corpus using translation model
def pred_probs(f_log_probs, prepare_data, options, iterator, verbose=True, normalization_alpha=0.0, alignweights=False):
    probs = []
    n_done = 0

    alignments_json = []
    costs_per_word = []

    for xs, y in iterator:
        x = xs[0]

        # ensure consistency in number of factors
        if len(x[0][0]) != options['factors']:
            logger.error(
                'Mismatch between number of factors in settings ({0}), and number in validation corpus ({1})\n'.format(
                    options['factors'], len(x[0][0])))
            sys.exit(1)

        n_done += len(x)

        x, x_mask, y, y_mask = prepare_data(x, y, n_words_src=options['n_words_src'],
                                            n_words=options['n_words'],
                                            n_factors=options['factors'])

        ### in optional save weights mode.
        if alignweights:
            pprobs, attention, cost_per_word = f_log_probs(x, x_mask, y, y_mask)
            for jdata in get_alignments(attention, x_mask, y_mask):
                alignments_json.append(jdata)
        else:
            pprobs, cost_per_word = f_log_probs(x, x_mask, y, y_mask)

        costs_per_word.extend(cost_per_word.T)

        # normalize scores according to output length
        if normalization_alpha:
            adjusted_lengths = numpy.array([numpy.count_nonzero(s) ** normalization_alpha for s in y_mask.T])
            pprobs /= adjusted_lengths

        for pp in pprobs:
            probs.append(pp)

        logger.debug('%d samples computed' % (n_done))

    return numpy.array(probs), alignments_json, costs_per_word


# calculate the log probablities on a given corpus using translation model (multi-source version
def multi_pred_probs(f_log_probs, multi_prepare_data, options, iterator, verbose=True, normalization_alpha=0.0,
                     alignweights=False):

    probs = []
    n_done = 0

    # list of alignments for each input source
    alignments_json = [[] for _ in range(len(options['extra_sources'])+1)] # list for multi-source
    costs_per_word = []

    for inputs, y in iterator:

        # for potentially multiple inputs
        for input in inputs:
            # ensure consistency in number of factors
            if len(input[0][0]) != options['factors']:
                logger.error('Mismatch between number of factors in settings ({0}), '
                              'and number in validation corpus ({1})\n'.format(options['factors'], len(input[0][0])))
                sys.exit(1)

        n_done += len(inputs[0])
        xs, x_masks, y, y_mask = multi_prepare_data(inputs, y, n_words_src=options['n_words_src'],
                                                    n_words=options['n_words'],
                                                    n_factors=options['factors'])

        # in optional save weights mode.
        inps = [z for (x, x_mask) in zip(xs, x_masks) for z in (x, x_mask)] + [y, y_mask]  # list of inputs

        if alignweights:
            ret = f_log_probs(*inps)

            pprobs = ret[0]
            attentions = ret[1:-1]
            cost_per_word = ret[-1]

            for i, attention in enumerate(attentions):
                alignment_json = []
                for jdata in get_alignments(attention, x_masks[i], y_mask):
                    alignment_json.append(jdata)
                alignments_json[i].extend(alignment_json)
        else:
            pprobs, cost_per_word = f_log_probs(*inps)

        costs_per_word.extend(cost_per_word.T)

        # normalize scores according to output length
        if normalization_alpha:
            adjusted_lengths = numpy.array([numpy.count_nonzero(s) ** normalization_alpha for s in y_mask.T])
            pprobs /= adjusted_lengths

        for pp in pprobs:
            probs.append(pp)

        logger.debug('%d samples computed' % (n_done))

    # if init-decoder, only have one attention (the other will be empty anyway)
    if options['multisource_type'] == 'init-decoder':
        alignments_json = [alignments_json[0]]

    return numpy.array(probs), alignments_json, costs_per_word


def train(dim_word=512,  # word vector dimensionality
          dim=1000,  # the number of LSTM units
          enc_depth=1,  # number of layers in the encoder
          dec_depth=1,  # number of layers in the decoder
          enc_recurrence_transition_depth=1,
          # number of GRU transition operations applied in the encoder. Minimum is 1. (Only applies to gru)
          dec_base_recurrence_transition_depth=2,
          # number of GRU transition operations applied in the first layer of the decoder. Minimum is 2. (Only applies to gru_cond)
          dec_high_recurrence_transition_depth=1,
          # number of GRU transition operations applied in the higher layers of the decoder. Minimum is 1. (Only applies to gru)
          dec_deep_context=False,  # include context vectors in deeper layers of the decoder
          enc_depth_bidirectional=None,  # first n encoder layers are bidirectional (default: all)
          factors=1,  # input factors
          dim_per_factor=None,
          # list of word vector dimensionalities (one per factor): [250,200,50] for total dimensionality of 500
          encoder='gru',
          decoder='gru_cond',
          decoder_deep='gru',
          patience=10,  # early stopping patience
          max_epochs=5000,
          finish_after=10000000,  # finish after this many updates
          dispFreq=1000,
          decay_c=0.,  # L2 regularization penalty
          map_decay_c=0.,  # L2 regularization penalty towards original weights
          clip_c=-1.,  # gradient clipping threshold
          lrate=0.0001,  # learning rate
          n_words_src=None,  # source vocabulary size
          n_words=None,  # target vocabulary size
          maxlen=100,  # maximum length of the description
          optimizer='adam',
          batch_size=16,
          valid_batch_size=16,
          saveto='model.npz',
          validFreq=10000,
          saveFreq=30000,  # save the parameters after every saveFreq updates
          sampleFreq=10000,  # generate some samples after every sampleFreq
          datasets=[  # path to training datasets (source and target)
              None,
              None],
          valid_datasets=[None,  # path to validation datasets (source and target)
                          None],
          dictionaries=[
              # path to dictionaries (json file created with ../data/build_dictionary.py). One dictionary per input factor; last dictionary is target-side dictionary.
              None,
              None],
          use_dropout=False,
          dropout_embedding=0.2,  # dropout for input embeddings (0: no dropout)
          dropout_hidden=0.2,  # dropout for hidden layers (0: no dropout)
          dropout_source=0,  # dropout source words (0: no dropout)
          dropout_target=0,  # dropout target words (0: no dropout)
          reload_=False,
          reload_training_progress=True,  # reload training progress (only used if reload_ is True)
          overwrite=False,
          external_validation_script=None,
          shuffle_each_epoch=True,
          sort_by_length=True,
          use_domain_interpolation=False,
          # interpolate between an out-domain training corpus and an in-domain training corpus
          domain_interpolation_min=0.1,  # minimum (initial) fraction of in-domain training data
          domain_interpolation_max=1.0,  # maximum fraction of in-domain training data
          domain_interpolation_inc=0.1,
          # interpolation increment to be applied each time patience runs out, until maximum amount of interpolation is reached
          domain_interpolation_indomain_datasets=[None, None],  # in-domain parallel training corpus (source and target)
          anneal_restarts=0,  # when patience run out, restart with annealed learning rate X times before early stopping
          anneal_decay=0.5,  # decay learning rate by this amount on each restart
          maxibatch_size=20,  # How many minibatches to load at one time
          objective="CE",
          # CE: cross-entropy; MRT: minimum risk training (see https://www.aclweb.org/anthology/P/P16/P16-1159.pdf)
          mrt_alpha=0.005,
          mrt_samples=100,
          mrt_samples_meanloss=10,
          mrt_reference=False,
          mrt_loss="SENTENCEBLEU n=4",  # loss function for minimum risk training
          mrt_ml_mix=0,  # interpolate mrt loss with ML loss
          model_version=0.1,  # store version used for training for compatibility
          prior_model=None,  # Prior model file, used for MAP
          tie_encoder_decoder_embeddings=False,
          # Tie the input embeddings of the encoder and the decoder (first factor only)
          tie_decoder_embeddings=False,  # Tie the input embeddings of the decoder with the softmax output embeddings
          encoder_truncate_gradient=-1,
          # Truncate BPTT gradients in the encoder to this value. Use -1 for no truncation
          decoder_truncate_gradient=-1,
          # Truncate BPTT gradients in the decoder to this value. Use -1 for no truncation
          layer_normalisation=False,  # layer normalisation https://arxiv.org/abs/1607.06450
          weight_normalisation=False,  # normalize weights
          extra_sources=None,  # path to training datasets (extra source inputs for multi-source)
          extra_valid_sources=None,  # validation datasets for extra input (multi-source)
          extra_source_dicts=[],  # dictionaries for secondary input (multi-source)
          extra_source_dicts_nums=None,  # number of auxiliary dictionaries for extra input
          extra_n_words_src=[],
          multisource_type=None, # multisource combination type
          debugm=False,
          tie_encoder_embeddings=False, # multiple encoders: share embedding parameters
          extra_sources_types=[]  # types of extra sources: whether they are source side or target side
          ):
    # ---------------- Model options ----------------
    model_options = OrderedDict(sorted(locals().copy().items()))

    counter = 0

    if model_options['dim_per_factor'] == None:
        if factors == 1:
            model_options['dim_per_factor'] = [model_options['dim_word']]
        else:
            logger.error('Error: if using factored input, you must specify \'dim_per_factor\'\n')
            sys.exit(1)

    assert (len(dictionaries) == factors + 1)  # one dictionary per source factor + 1 for target factor
    assert (len(model_options['dim_per_factor']) == factors)  # each factor embedding has its own dimensionality
    assert (sum(model_options['dim_per_factor']) == model_options[
        'dim_word'])  # dimensionality of factor embeddings sums up to total dimensionality of input embedding vector
    assert (prior_model != None and (os.path.exists(prior_model)) or (
        map_decay_c == 0.0))  # MAP training requires a prior model file

    assert (enc_recurrence_transition_depth >= 1)  # enc recurrence transition depth must be at least 1.
    assert (dec_base_recurrence_transition_depth >= 2)  # dec base recurrence transition depth must be at least 2.
    assert (dec_high_recurrence_transition_depth >= 1)  # dec higher recurrence transition depth must be at least 1.

    if model_options['enc_depth_bidirectional'] is None:
        model_options['enc_depth_bidirectional'] = model_options['enc_depth']
    # first layer is always bidirectional; make sure people don't forget to increase enc_depth as well
    assert (model_options['enc_depth_bidirectional'] >= 1 and
            model_options['enc_depth_bidirectional'] <= model_options['enc_depth'])

    # ---------------- Sanity check on multisource inputs ---------------
    # all sets must be specified
    if model_options["multisource_type"] is not None:
        num_encoders = len(model_options['extra_sources']) + 1
        assert extra_sources is not None
        multisource = True
    else:
        multisource = False
        model_options['extra_sources'] = []
        extra_sources = []

    # ---------------- load dictionaries and invert them ----------------
    worddicts = []
    worddicts_r = []

    # Structure of dictionaries = one list of dictionaries for each input.
    # First list of dictionaries are source (possibly factored) (:-1) and target (-1).
    # All subsequent lists of dictionaries are for extra sources (possible factored)

    # structure dictionaries by number of dictionary per input (if dictionaries are provided)
    extra_source_dicts2 = []
    j = 0
    for i in range(len(extra_source_dicts_nums)):
        extra_source_dicts2.append(extra_source_dicts[j:j+extra_source_dicts_nums[i]])
        j += extra_source_dicts_nums[i]
    extra_source_dicts = extra_source_dicts2

    # if no dictionaries were provided, reuse main dictionaries
    if len(extra_source_dicts) == 0 and len(extra_sources) > 0:
        extra_source_dicts = [dictionaries[:-1] * len(extra_sources)]
        extra_source_dicts_nums = [len(dictionaries[:-1]) * len(extra_sources)]

    for i, dicts in enumerate([dictionaries] + extra_source_dicts):

        # main source dictionary must be provided
        if len(dicts) == 0 and i == 0:
            logger.error("Dictionaries must be provided for main source inputs and target.")

        # for extra dictionaries, if none provided, reuse main source dictionaries for each extra input
        elif len(dicts) == 0 and i-1 < len(extra_sources):
            logger.warn('Reusing main src dicts for extra input #%s' % str(i))
            dicts = dictionaries[:-1] # only copy source dictionaries
            model_options['extra_source_dicts'].append(dictionaries[:-1])

        # no extra sources so ignore looking for extra source dicts
        elif i > len(extra_sources):
            continue

        # if dictionaries are specified
        worddicts1 = [None] * len(dicts)
        worddicts_r1 = [None] * len(dicts)
        for ii, dd in enumerate(dicts):
            worddicts1[ii] = load_dict(dd)
            worddicts_r1[ii] = dict()
            for kk, vv in worddicts1[ii].iteritems():
                worddicts_r1[ii][vv] = kk
        worddicts.append(worddicts1)
        worddicts_r.append(worddicts_r1)

    # vocabulary sizes for each of the input sources (words)
    all_n_words_src = [max(wd[0].values())+1 for w, wd in enumerate(worddicts)]

    model_options['n_words_src'] = all_n_words_src

    # vocabulary size for the target
    if n_words is None:
        n_words = max(worddicts[0][-1].values())+1
        model_options['n_words'] = n_words

    if tie_encoder_decoder_embeddings:
        assert all(nws == n_words for nws in model_options['n_words_src']), \
            "When tying encoder and decoder embeddings, source and target vocabulary size must be the same"
        if any(wd[0] != worddicts[1] for wd in worddicts):
            logger.warning("Encoder-decoder embedding tying is enabled with different source and target dictionaries. "
                            "This is usually a configuration error")

    if model_options['objective'] == 'MRT':
        # in CE mode parameters are updated once per batch; in MRT mode parameters are updated once
        # per pair of train sentences (== per batch of samples), so we set batch_size to 1 to make
        # model saving, validation, etc trigger after the same number of updates as before
        logger.info('Running in MRT mode, minibatch size set to 1 sentence')
        batch_size = 1

    # ---------------- Initialise training progress ---------------
    training_progress = TrainingProgress()
    best_p = None
    best_opt_p = None
    training_progress.bad_counter = 0
    training_progress.anneal_restarts_done = 0
    training_progress.uidx = 0
    training_progress.eidx = 0
    training_progress.estop = False
    training_progress.history_errs = []
    training_progress.domain_interpolation_cur = domain_interpolation_min if use_domain_interpolation else None
    # reload training progress
    training_progress_file = saveto + '.progress.json'
    if reload_ and reload_training_progress and os.path.exists(training_progress_file):
        logger.info('Reloading training progress')
        training_progress.load_from_json(training_progress_file)
        if (training_progress.estop == True) or (training_progress.eidx > max_epochs) or (
                    training_progress.uidx >= finish_after):
            logger.warning(
                'Training is already complete. Disable reloading of training progress (--no_reload_training_progress) or remove or modify progress file (%s) to train anyway.' % training_progress_file)
            return numpy.inf

    # adjust learning rate if we resume process that has already entered annealing phase
    if training_progress.anneal_restarts_done > 0:
        lrate *= anneal_decay ** training_progress.anneal_restarts_done

    # ---------------- Loading data ---------------
    logger.info('Loading data')
    # TODO: multi-source
    if use_domain_interpolation:
        logger.info('Using domain interpolation with initial ratio %s, final ratio %s, increase rate %s' % (
            training_progress.domain_interpolation_cur, domain_interpolation_max, domain_interpolation_inc))
        train = DomainInterpolatorTextIterator(datasets[0], datasets[1],
                                               dictionaries[:-1], dictionaries[1],
                                               n_words_source=all_n_words_src, n_words_target=n_words,
                                               batch_size=batch_size,
                                               maxlen=maxlen,
                                               skip_empty=True,
                                               shuffle_each_epoch=shuffle_each_epoch,
                                               sort_by_length=sort_by_length,
                                               indomain_source=domain_interpolation_indomain_datasets[0],
                                               indomain_target=domain_interpolation_indomain_datasets[1],
                                               interpolation_rate=training_progress.domain_interpolation_cur,
                                               use_factor=(factors > 1),
                                               maxibatch_size=maxibatch_size)
    else:
        train = TextIterator(datasets[0], datasets[1],
                             dictionaries[:-1], dictionaries[-1],
                             n_words_source=n_words_src, n_words_target=n_words,
                             batch_size=batch_size,
                             maxlen=maxlen,
                             skip_empty=True,
                             shuffle_each_epoch=shuffle_each_epoch,
                             sort_by_length=sort_by_length,
                             use_factor=(factors > 1),
                             maxibatch_size=maxibatch_size,
                             extra_sources=extra_sources,
                             extra_source_dicts=extra_source_dicts,
                             extra_source_dicts_nums=extra_source_dicts_nums,
                             extra_n_words_source=extra_n_words_src)

    if valid_datasets and validFreq:
        valid = TextIterator(valid_datasets[0], valid_datasets[1],
                             dictionaries[:-1], dictionaries[-1],
                             n_words_source=n_words_src, n_words_target=n_words,
                             batch_size=valid_batch_size,
                             use_factor=(factors > 1),
                             maxlen=maxlen,
                             extra_sources=extra_valid_sources,
                             extra_source_dicts=extra_source_dicts,
                             extra_source_dicts_nums=extra_source_dicts_nums,
                             extra_n_words_source=extra_n_words_src)
    else:
        valid = None

    comp_start = time.time()

    logger.info('Building model')

    # ---------------- Initialise parameters ----------------
    params = init_params(model_options)

    optimizer_params = {}
    # prepare parameters
    if reload_ and os.path.exists(saveto):
        logger.info('Reloading model parameters')
        params = load_params(saveto, params)
        logger.info('Reloading optimizer parameters')
        try:
            logger.info('trying to load optimizer params from {0} or {1}'.format(saveto + '.gradinfo',
                                                                                  saveto + '.gradinfo.npz'))
            optimizer_params = load_optimizer_params(saveto + '.gradinfo', optimizer)
        except IOError:
            logger.warning(
                '{0}(.npz) not found. Trying to load optimizer params from {1}(.npz)'.format(saveto + '.gradinfo',
                                                                                             saveto))
            optimizer_params = load_optimizer_params(saveto, optimizer)
    elif prior_model:
        logger.info('Initializing model parameters from prior')
        params = load_params(prior_model, params)

    # ---------------- load prior model if specified ----------------
    if prior_model:
        logger.info('Loading prior model parameters')
        params = load_params(prior_model, params, with_prefix='prior_')

    tparams = init_theano_params(params)

    # ---------------- build model ----------------
    if multisource_type is not None:
        trng, use_noise, xs, x_masks, y, y_mask, opt_ret, cost = build_multisource_model(tparams, model_options)

        #inps = [xs[0], x_masks[0], xs[1], x_masks[1], y, y_mask]
        inps = [z for (x, x_mask) in zip(xs, x_masks) for z in (x, x_mask)] + [y, y_mask]

    else:
        trng, use_noise, x, x_mask, y, y_mask, opt_ret, cost = build_model(tparams, model_options)
        inps = [x, x_mask, y, y_mask]


    # ---------------- build model ----------------
    if validFreq or sampleFreq:
        logger.info('Building sampler')
        if multisource_type is not None:
            f_init, f_next = build_multi_sampler(tparams, model_options, use_noise, trng)
        else:
            f_init, f_next = build_sampler(tparams, model_options, use_noise, trng)
    if model_options['objective'] == 'MRT':
        logger.info('Building MRT sampler')
        # TODO: multisource
        f_sampler = build_full_sampler(tparams, model_options, use_noise, trng)

    # before any regularizer
    logger.info('Building f_log_probs...')
    f_log_probs = theano.function(inps, (cost, opt_ret['cost_per_word']), profile=profile)
    logger.info('Done')

    if model_options['objective'] == 'CE':
        cost = cost.mean()
    elif model_options['objective'] == 'MRT':
        # MRT objective function
        cost, loss = mrt_cost(cost, y_mask, model_options)
        inps += [loss]
    else:
        logger.error('Objective must be one of ["CE", "MRT"]')
        sys.exit(1)

    # apply L2 regularization on weights
    if decay_c > 0.:
        decay_c = theano.shared(numpy_floatX(decay_c), name='decay_c')
        weight_decay = 0.
        for kk, vv in tparams.iteritems():
            if kk.startswith('prior_'):
                continue
            weight_decay += (vv ** 2).sum()
        weight_decay *= decay_c
        cost += weight_decay

    # apply L2 regularisation to loaded model (map training)
    if map_decay_c > 0:
        map_decay_c = theano.shared(numpy_floatX(map_decay_c), name="map_decay_c")
        weight_map_decay = 0.
        for kk, vv in tparams.iteritems():
            if kk.startswith('prior_'):
                continue
            init_value = tparams['prior_' + kk]
            weight_map_decay += ((vv - init_value) ** 2).sum()
        weight_map_decay *= map_decay_c
        cost += weight_map_decay

    updated_params = OrderedDict(tparams)

    # don't update prior model parameters
    if prior_model:
        updated_params = OrderedDict(
            [(key, value) for (key, value) in updated_params.iteritems() if not key.startswith('prior_')])

    logger.info('Computing gradient...')

    grads = tensor.grad(cost, wrt=itemlist(updated_params))
    logger.info('Done')

    # apply gradient clipping here
    if clip_c > 0.:
        g2 = 0.
        for g in grads:
            g2 += (g ** 2).sum()
        new_grads = []
        for g in grads:
            new_grads.append(tensor.switch(g2 > (clip_c ** 2),
                                           g / tensor.sqrt(g2) * clip_c,
                                           g))
        grads = new_grads

    # compile the optimizer, the actual computational graph is compiled here
    lr = tensor.scalar(name='lr')

    logger.info('Building optimizers...')
    f_update, optimizer_tparams = eval(optimizer)(lr, updated_params,
                                                  grads, inps, cost,
                                                  profile=profile,
                                                  optimizer_params=optimizer_params)
    logger.info('Done')

    logger.info('Total compilation time: {0:.1f}s'.format(time.time() - comp_start))

    if validFreq == -1 or saveFreq == -1 or sampleFreq == -1:
        logger.info('Computing number of training batches')
        num_batches = len(train)
        logger.info('There are {} batches in the train set'.format(num_batches))

        if validFreq == -1:
            validFreq = num_batches
        if saveFreq == -1:
            saveFreq = num_batches
        if sampleFreq == -1:
            sampleFreq = num_batches

    logger.info('Optimization')

    # save model options
    json.dump(model_options, open('%s.json' % saveto, 'wb'), indent=2)

    valid_err = None

    cost_sum = 0
    cost_batches = 0
    last_disp_samples = 0
    last_words = 0
    ud_start = time.time()
    p_validation = None
    for training_progress.eidx in xrange(training_progress.eidx, max_epochs):
        n_samples = 0

        for xs, y in train:
            # ease of manipulation
            if multisource_type is not None:
                x = xs[0]
                extra_xs = xs[1:]
            else:
                x = xs[0]

            training_progress.uidx += 1
            use_noise.set_value(1.)

            # ensure consistency in number of factors
            if len(x) and len(x[0]) and len(x[0][0]) != factors:
                logger.error(
                    'Mismatch between number of factors in settings ({0}), and number in training corpus ({1})\n'.format(
                        factors, len(x[0][0])))
                sys.exit(1)
            xlen = len(x)

            if multisource_type is not None:
                # ensure consistency in number of factors
                if any(len(xx) and len(xx[0]) and len(xx[0][0]) != factors for xx in extra_xs):
                    logger.error('Auxiliary input: Mismatch between number of factors in settings ({0}), and number in training corpus\n'.format(
                            factors))
                    sys.exit(1)
                extra_xlens = [len(xx) for xx in extra_xs]
                assert all(xlen == xxlen for xxlen in extra_xlens)  # must be the same size

            n_samples += xlen

            if model_options['objective'] == 'CE':

                xs, x_masks, y, y_mask = prepare_multi_data(xs, y,
                                                            maxlen=maxlen,
                                                            n_factors=factors,
                                                            n_words_src=all_n_words_src,
                                                            n_words=n_words)

                if any(xx is None for xx in xs):
                    logger.warning('Multisource: Minibatch with zero sample under length %d' % maxlen)
                    training_progress.uidx -= 1
                    continue

                cost_batches += 1
                last_disp_samples += xlen

                last_words += (numpy.sum(x_masks[0]) + numpy.sum(y_mask)) / 2.0

                # TODO: make generic
                # compute cost, grads and update parameters
                if multisource_type is not None:
                    if debugm:
                        debug = open('debugtrain.txt', 'a')
                        debug.write(str(counter)+": \n")
                        debug.write(len(xs), str(len(x_masks))+"\n")
                        debug.write(xs[0].shape, xs[1].shape, x_masks[0].shape, str(x_masks[1].shape)+"\n")
                        debug.write(str(xs[0])+"\n")
                        debug.write(str(xs[1])+"\n")
                        counter += xs.shape[1]
                        debug.close()

                    inps = [lrate] + [z for (x, x_mask) in zip(xs, x_masks) for z in (x, x_mask)] + [y, y_mask]

                    cost = f_update(*inps)
                else:
                    cost = f_update(lrate, xs[0], x_masks[0], y, y_mask)

                cost_sum += cost

            # TODO: multi-source
            elif model_options['objective'] == 'MRT':
                assert maxlen is not None and maxlen > 0

                xy_pairs = [(x_i, y_i) for (x_i, y_i) in zip(x, y) if len(x_i) < maxlen and len(y_i) < maxlen]
                if not xy_pairs:
                    training_progress.uidx -= 1
                    continue

                for x_s, y_s in xy_pairs:

                    # add EOS and prepare factored data
                    x, _, _, _ = prepare_data([x_s], [y_s], maxlen=None, n_factors=factors, n_words_src=n_words_src,
                                              n_words=n_words)

                    # draw independent samples to compute mean reward
                    if model_options['mrt_samples_meanloss']:
                        use_noise.set_value(0.)
                        samples, _ = f_sampler(x, model_options['mrt_samples_meanloss'], maxlen)
                        use_noise.set_value(1.)

                        samples = [numpy.trim_zeros(item) for item in zip(*samples)]

                        # map integers to words (for character-level metrics)
                        samples = [seqs2words(sample, worddicts_r[-1]) for sample in samples]
                        ref = seqs2words(y_s, worddicts_r[-1])

                        # scorers expect tokenized hypotheses/references
                        ref = ref.split(" ")
                        samples = [sample.split(" ") for sample in samples]

                        # get negative smoothed BLEU for samples
                        scorer = ScorerProvider().get(model_options['mrt_loss'])
                        scorer.set_reference(ref)
                        mean_loss = numpy.array(scorer.score_matrix(samples), dtype=floatX).mean()
                    else:
                        mean_loss = 0.

                    # create k samples
                    use_noise.set_value(0.)
                    samples, _ = f_sampler(x, model_options['mrt_samples'], maxlen)
                    use_noise.set_value(1.)

                    samples = [numpy.trim_zeros(item) for item in zip(*samples)]

                    # remove duplicate samples
                    samples.sort()
                    samples = [s for s, _ in itertools.groupby(samples)]

                    # add gold translation [always in first position]
                    if model_options['mrt_reference'] or model_options['mrt_ml_mix']:
                        samples = [y_s] + [s for s in samples if s != y_s]

                    # create mini-batch with masking
                    x, x_mask, y, y_mask = prepare_data([x_s for _ in xrange(len(samples))], samples,
                                                        maxlen=None,
                                                        n_factors=factors,
                                                        n_words_src=n_words_src,
                                                        n_words=n_words)

                    cost_batches += 1
                    last_disp_samples += xlen
                    last_words += (numpy.sum(x_mask) + numpy.sum(y_mask)) / 2.0

                    # map integers to words (for character-level metrics)
                    samples = [seqs2words(sample, worddicts_r[-1]) for sample in samples]
                    y_s = seqs2words(y_s, worddicts_r[-1])

                    # scorers expect tokenized hypotheses/references
                    y_s = y_s.split(" ")
                    samples = [sample.split(" ") for sample in samples]

                    # get negative smoothed BLEU for samples
                    scorer = ScorerProvider().get(model_options['mrt_loss'])
                    scorer.set_reference(y_s)
                    loss = mean_loss - numpy.array(scorer.score_matrix(samples), dtype=floatX)

                    # compute cost, grads and update parameters
                    cost = f_update(lrate, x, x_mask, y, y_mask, loss)

                    cost_sum += cost

            # check for bad numbers, usually we remove non-finite elements
            # and continue training - but not done here
            if numpy.isnan(cost) or numpy.isinf(cost):
                logger.warning('NaN detected')
                return 1., 1., 1.

            # verbose
            if numpy.mod(training_progress.uidx, dispFreq) == 0:
                ud = time.time() - ud_start
                sps = last_disp_samples / float(ud)
                wps = last_words / float(ud)
                cost_avg = cost_sum / float(cost_batches)
                logger.info(
                    'Epoch {epoch} Update {update} Cost {cost} UD {ud} {sps} {wps}'.format(
                        epoch=training_progress.eidx,
                        update=training_progress.uidx,
                        cost=cost_avg,
                        ud=ud,
                        sps="{0:.2f} sents/s".format(sps),
                        wps="{0:.2f} words/s".format(wps)
                    )
                )
                ud_start = time.time()
                cost_batches = 0
                last_disp_samples = 0
                last_words = 0
                cost_sum = 0

            # save the best model so far, in addition, save the latest model
            # into a separate file with the iteration number for external eval
            if numpy.mod(training_progress.uidx, saveFreq) == 0:
                logger.info('Saving the best model...')
                if best_p is not None:
                    params = best_p
                    optimizer_params = best_opt_p
                else:
                    params = unzip_from_theano(tparams, excluding_prefix='prior_')
                    optimizer_params = unzip_from_theano(optimizer_tparams, excluding_prefix='prior_')

                save(params, optimizer_params, training_progress, saveto)
                logger.info('Done')

                # save with uidx
                if not overwrite:
                    logger.info('Saving the model at iteration {}...'.format(training_progress.uidx))
                    saveto_uidx = '{}.iter{}.npz'.format(
                        os.path.splitext(saveto)[0], training_progress.uidx)

                    params = unzip_from_theano(tparams, excluding_prefix='prior_')
                    optimizer_params = unzip_from_theano(optimizer_tparams, excluding_prefix='prior_')
                    save(params, optimizer_params, training_progress, saveto_uidx)
                    logger.info('Done')

            # generate some samples with the model and display them
            if sampleFreq and numpy.mod(training_progress.uidx, sampleFreq) == 0:
                # FIXME: random selection?
                for jj in xrange(numpy.minimum(5, xs[0].shape[2])):
                    stochastic = True

                    # the main input x
                    x_current = xs[0][:, :, jj][:, :, None]
                    # remove padding
                    x_current = x_current[:, :x_masks[0].astype('int64')[:, jj].sum(), :]

                    # extra current inputs x
                    extra_x_current = [None] * len(extra_sources)
                    for i in range(len(extra_sources)):
                        extra_x_current[i] = (xs[i+1][:, :, jj][:, :, None])
                        # remove padding
                        extra_x_current[i] = extra_x_current[i][:, :x_masks[i+1].astype('int64')[:, jj].sum(), :]

                    if multisource_type == 'init-decoder':
                        using_init = True
                    else:
                        using_init = False

                    sample, score, sample_word_probs, alignment, hyp_graph = gen_sample([f_init], [f_next],
                                                                                        x_current,
                                                                                        trng=trng, k=1,
                                                                                        maxlen=30,
                                                                                        stochastic=stochastic,
                                                                                        argmax=False,
                                                                                        suppress_unk=False,
                                                                                        return_hyp_graph=False,
                                                                                        extra_xs=extra_x_current,
                                                                                        init_decoder=using_init)

                    # TODO: only accepting 2 inputs at present
                    print '\nSource ', jj, ': ',
                    for pos in range(xs[0].shape[1]):

                        if xs[0][0, pos, jj] == 0:
                            break
                        for factor in range(factors):
                            vv = xs[0][factor, pos, jj]

                            #print(vv)
                            if vv in worddicts_r[0][factor]:
                                sys.stdout.write(worddicts_r[0][factor][vv])
                            else:
                                sys.stdout.write('UNK')
                            if factor + 1 < factors:
                                sys.stdout.write('|')
                            else:
                                sys.stdout.write(' ')
                    if multisource_type is not None:
                        print '\nAuxiliary source ', jj, ': ',
                        for pos in range(xs[1].shape[1]):
                            if xs[1][0, pos, jj] == 0:
                                break
                            for factor in range(factors):
                                vv = xs[1][factor, pos, jj]
                                if vv in worddicts_r[1][factor]:
                                    sys.stdout.write(worddicts_r[1][factor][vv])
                                else:
                                    sys.stdout.write('UNK')
                                if factor + 1 < factors:
                                    sys.stdout.write('|')
                                else:
                                    sys.stdout.write(' ')
                    print
                    print 'Truth ', jj, ' : ',
                    for vv in y[:, jj]:
                        if vv == 0:
                            break
                        #print(vv)
                        if vv in worddicts_r[0][-1]:
                            print worddicts_r[0][-1][vv],
                        else:
                            print 'UNK',
                    print
                    print 'Sample ', jj, ': ',
                    if stochastic:
                        ss = sample[0]
                    else:
                        score = score / numpy.array([len(s) for s in sample])
                        ss = sample[score.argmin()]
                    for vv in ss:
                        if vv == 0:
                            break
                        if vv in worddicts_r[0][-1]:
                            print worddicts_r[0][-1][vv],
                        else:
                            print 'UNK',
                    print

            # validate model on validation set and early stop if necessary
            if valid is not None and validFreq and numpy.mod(training_progress.uidx, validFreq) == 0:
                use_noise.set_value(0.)
                if multisource_type is not None:
                    valid_errs, alignments, _ = multi_pred_probs(f_log_probs, prepare_multi_data, model_options, valid)
                else:
                    valid_errs, alignment, _ = multi_pred_probs(f_log_probs, prepare_multi_data, model_options, valid)

                valid_err = valid_errs.mean()
                training_progress.history_errs.append(float(valid_err))

                if training_progress.uidx == 0 or valid_err <= numpy.array(training_progress.history_errs).min():
                    best_p = unzip_from_theano(tparams, excluding_prefix='prior_')
                    best_opt_p = unzip_from_theano(optimizer_tparams, excluding_prefix='prior_')
                    training_progress.bad_counter = 0
                if valid_err >= numpy.array(training_progress.history_errs).min():
                    training_progress.bad_counter += 1
                    if training_progress.bad_counter > patience:

                        # change mix of in-domain and out-of-domain data
                        if use_domain_interpolation and (
                                    training_progress.domain_interpolation_cur < domain_interpolation_max):
                            training_progress.domain_interpolation_cur = min(
                                training_progress.domain_interpolation_cur + domain_interpolation_inc,
                                domain_interpolation_max)
                            logger.info(
                                'No progress on the validation set, increasing domain interpolation rate to %s and resuming from best params' % training_progress.domain_interpolation_cur)
                            train.adjust_domain_interpolation_rate(training_progress.domain_interpolation_cur)
                            if best_p is not None:
                                zip_to_theano(best_p, tparams)
                                zip_to_theano(best_opt_p, optimizer_tparams)
                            training_progress.bad_counter = 0

                        # anneal learning rate and reset optimizer parameters
                        elif training_progress.anneal_restarts_done < anneal_restarts:
                            logger.info(
                                'No progress on the validation set, annealing learning rate and resuming from best params.')
                            lrate *= anneal_decay
                            training_progress.anneal_restarts_done += 1
                            training_progress.bad_counter = 0

                            # reload best parameters
                            if best_p is not None:
                                zip_to_theano(best_p, tparams)

                            # reset optimizer parameters
                            for item in optimizer_tparams.values():
                                item.set_value(numpy.array(item.get_value()) * 0.)

                        # stop
                        else:
                            logger.info('Valid {}'.format(valid_err))
                            logger.info('Early Stop!')
                            training_progress.estop = True
                            break

                logger.info('Valid {}'.format(valid_err))

                if external_validation_script:
                    logger.info("Calling external validation script")
                    if p_validation is not None and p_validation.poll() is None:
                        logger.info("Waiting for previous validation run to finish")
                        logger.info(
                            "If this takes too long, consider increasing validation interval, reducing validation set size, or speeding up validation by using multiple processes")
                        valid_wait_start = time.time()
                        p_validation.wait()
                        logger.info("Waited for {0:.1f} seconds".format(time.time() - valid_wait_start))
                    logger.info('Saving  model...')
                    params = unzip_from_theano(tparams, excluding_prefix='prior_')
                    optimizer_params = unzip_from_theano(optimizer_tparams, excluding_prefix='prior_')
                    save(params, optimizer_params, training_progress, saveto + '.dev')
                    json.dump(model_options, open('%s.dev.npz.json' % saveto, 'wb'), indent=2)
                    logger.info('Done')
                    p_validation = Popen([external_validation_script])

            # finish after this many updates
            if training_progress.uidx >= finish_after:
                logger.info('Finishing after %d iterations!' % training_progress.uidx)
                training_progress.estop = True
                break

        logger.info('Seen %d samples' % n_samples)

        if training_progress.estop:
            break

    if best_p is not None:
        zip_to_theano(best_p, tparams)
        zip_to_theano(best_opt_p, optimizer_tparams)

    if valid is not None:
        use_noise.set_value(0.)
        if multisource_type is not None:
            valid_errs, alignments, _ = multi_pred_probs(f_log_probs, prepare_multi_data, model_options, valid)
        else:
            valid_errs, alignments, _ = multi_pred_probs(f_log_probs, prepare_multi_data, model_options, valid)
        valid_err = valid_errs.mean()

        logger.info('Valid {}'.format(valid_err))

    if best_p is not None:
        params = copy.copy(best_p)
        optimizer_params = copy.copy(best_opt_p)

    else:
        params = unzip_from_theano(tparams, excluding_prefix='prior_')
        optimizer_params = unzip_from_theano(optimizer_tparams, excluding_prefix='prior_')

    save(params, optimizer_params, training_progress, saveto)

    return valid_err


def _set_up_logging(args):

    log_path = args.saveto + ".log"
    if args.reload_:
        write_mode = "w+"
    else:
        write_mode = "w"

    # log to logfile
    logging.basicConfig(level=logging.DEBUG,
                        format='[%(asctime)s:%(levelname)s:%(name)s:%(funcName)s] %(message)s',
                        filename=log_path,
                        filemode=write_mode)

    level = logging.INFO

    # log to STDERR
    console = logging.StreamHandler()
    console.setLevel(level)
    formatter = logging.Formatter('[%(asctime)s:%(levelname)s:%(name)s:%(funcName)s] %(message)s')
    console.setFormatter(formatter)
    logging.getLogger("").addHandler(console)

    logger.info(args)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    data = parser.add_argument_group('data sets; model loading and saving')
    data.add_argument('--datasets', type=str, required=True, metavar='PATH', nargs=2,
                      help="parallel training corpus (source and target)")
    data.add_argument('--dictionaries', type=str, required=True, metavar='PATH', nargs="+",
                      help="network vocabularies (one per source factor, plus target vocabulary)")
    data.add_argument('--model', type=str, default='model.npz', metavar='PATH', dest='saveto',
                      help="model file name (default: %(default)s)")
    data.add_argument('--saveFreq', type=int, default=30000, metavar='INT',
                      help="save frequency (default: %(default)s)")
    data.add_argument('--reload', action='store_true', dest='reload_',
                      help="load existing model (if '--model' points to existing model)")
    data.add_argument('--no_reload_training_progress', action='store_false', dest='reload_training_progress',
                      help="don't reload training progress (only used if --reload is enabled)")
    data.add_argument('--overwrite', action='store_true',
                      help="write all models to same file")

    network = parser.add_argument_group('network parameters')
    network.add_argument('--dim_word', type=int, default=512, metavar='INT',
                         help="embedding layer size (default: %(default)s)")
    network.add_argument('--dim', type=int, default=1000, metavar='INT',
                         help="hidden layer size (default: %(default)s)")
    network.add_argument('--n_words_src', type=int, default=None, metavar='INT',
                         help="source vocabulary size (default: %(default)s)")
    network.add_argument('--n_words', type=int, default=None, metavar='INT',
                         help="target vocabulary size (default: %(default)s)")
    network.add_argument('--enc_depth', type=int, default=1, metavar='INT',
                         help="number of encoder layers (default: %(default)s)")
    network.add_argument('--dec_depth', type=int, default=1, metavar='INT',
                         help="number of decoder layers (default: %(default)s)")

    network.add_argument('--enc_recurrence_transition_depth', type=int, default=1, metavar='INT',
                         help="number of GRU transition operations applied in the encoder. Minimum is 1. (Only applies to gru). (default: %(default)s)")
    network.add_argument('--dec_base_recurrence_transition_depth', type=int, default=2, metavar='INT',
                         help="number of GRU transition operations applied in the first layer of the decoder. Minimum is 2.  (Only applies to gru_cond). (default: %(default)s)")
    network.add_argument('--dec_high_recurrence_transition_depth', type=int, default=1, metavar='INT',
                         help="number of GRU transition operations applied in the higher layers of the decoder. Minimum is 1. (Only applies to gru). (default: %(default)s)")

    network.add_argument('--dec_deep_context', action='store_true',
                         help="pass context vector (from first layer) to deep decoder layers")
    network.add_argument('--enc_depth_bidirectional', type=int, default=None, metavar='INT',
                         help="number of bidirectional encoder layer; if enc_depth is greater, remaining layers are unidirectional; by default, all layers are bidirectional.")

    network.add_argument('--factors', type=int, default=1, metavar='INT',
                         help="number of input factors (default: %(default)s)")
    network.add_argument('--dim_per_factor', type=int, default=None, nargs='+', metavar='INT',
                         help="list of word vector dimensionalities (one per factor): '--dim_per_factor 250 200 50' for total dimensionality of 500 (default: %(default)s)")
    network.add_argument('--use_dropout', action="store_true",
                         help="use dropout layer (default: %(default)s)")
    network.add_argument('--dropout_embedding', type=float, default=0.2, metavar="FLOAT",
                         help="dropout for input embeddings (0: no dropout) (default: %(default)s)")
    network.add_argument('--dropout_hidden', type=float, default=0.2, metavar="FLOAT",
                         help="dropout for hidden layer (0: no dropout) (default: %(default)s)")
    network.add_argument('--dropout_source', type=float, default=0, metavar="FLOAT",
                         help="dropout source words (0: no dropout) (default: %(default)s)")
    network.add_argument('--dropout_target', type=float, default=0, metavar="FLOAT",
                         help="dropout target words (0: no dropout) (default: %(default)s)")
    network.add_argument('--layer_normalisation', action="store_true",
                         help="use layer normalisation (default: %(default)s)")
    network.add_argument('--weight_normalisation', action="store_true",
                         help=" normalize weights (default: %(default)s)")
    network.add_argument('--tie_encoder_decoder_embeddings', action="store_true", dest="tie_encoder_decoder_embeddings",
                         help="tie the input embeddings of the encoder and the decoder (first factor only). Source and target vocabulary size must the same")
    network.add_argument('--tie_decoder_embeddings', action="store_true", dest="tie_decoder_embeddings",
                         help="tie the input embeddings of the decoder with the softmax output embeddings")
    # network.add_argument('--encoder', type=str, default='gru',
    # choices=['gru'],
    # help='encoder recurrent layer')
    # network.add_argument('--decoder', type=str, default='gru_cond',
    # choices=['gru_cond'],
    # help='first decoder recurrent layer')
    network.add_argument('--decoder_deep', type=str, default='gru',
                         choices=['gru', 'gru_cond'],
                         help='decoder recurrent layer after first one')

    training = parser.add_argument_group('training parameters')
    training.add_argument('--maxlen', type=int, default=100, metavar='INT',
                          help="maximum sequence length (default: %(default)s)")
    training.add_argument('--optimizer', type=str, default="adam",
                          choices=['adam', 'adadelta', 'rmsprop', 'sgd', 'sgdmomentum'],
                          help="optimizer (default: %(default)s)")
    training.add_argument('--batch_size', type=int, default=80, metavar='INT',
                          help="minibatch size (default: %(default)s)")
    training.add_argument('--max_epochs', type=int, default=5000, metavar='INT',
                          help="maximum number of epochs (default: %(default)s)")
    training.add_argument('--finish_after', type=int, default=10000000, metavar='INT',
                          help="maximum number of updates (minibatches) (default: %(default)s)")
    training.add_argument('--decay_c', type=float, default=0, metavar='FLOAT',
                          help="L2 regularization penalty (default: %(default)s)")
    training.add_argument('--map_decay_c', type=float, default=0, metavar='FLOAT',
                          help="L2 regularization penalty towards original weights (default: %(default)s)")
    training.add_argument('--clip_c', type=float, default=1, metavar='FLOAT',
                          help="gradient clipping threshold (default: %(default)s)")
    training.add_argument('--lrate', type=float, default=0.0001, metavar='FLOAT',
                          help="learning rate (default: %(default)s)")
    training.add_argument('--no_shuffle', action="store_false", dest="shuffle_each_epoch",
                          help="disable shuffling of training data (for each epoch)")
    training.add_argument('--no_sort_by_length', action="store_false", dest="sort_by_length",
                          help='do not sort sentences in maxibatch by length')
    training.add_argument('--maxibatch_size', type=int, default=20, metavar='INT',
                          help='size of maxibatch (number of minibatches that are sorted by length) (default: %(default)s)')
    training.add_argument('--objective', choices=['CE', 'MRT'], default='CE',
                          help='training objective. CE: cross-entropy minimization (default); MRT: Minimum Risk Training (https://www.aclweb.org/anthology/P/P16/P16-1159.pdf)')
    training.add_argument('--encoder_truncate_gradient', type=int, default=-1, metavar='INT',
                          help="truncate BPTT gradients in the encoder to this value. Use -1 for no truncation (default: %(default)s)")
    training.add_argument('--decoder_truncate_gradient', type=int, default=-1, metavar='INT',
                          help="truncate BPTT gradients in the encoder to this value. Use -1 for no truncation (default: %(default)s)")

    validation = parser.add_argument_group('validation parameters')
    validation.add_argument('--valid_datasets', type=str, default=None, metavar='PATH', nargs=2,
                            help="parallel validation corpus (source and target) (default: %(default)s)")
    validation.add_argument('--valid_batch_size', type=int, default=80, metavar='INT',
                            help="validation minibatch size (default: %(default)s)")
    validation.add_argument('--validFreq', type=int, default=10000, metavar='INT',
                            help="validation frequency (default: %(default)s)")
    validation.add_argument('--patience', type=int, default=10, metavar='INT',
                            help="early stopping patience (default: %(default)s)")
    validation.add_argument('--anneal_restarts', type=int, default=0, metavar='INT',
                            help="when patience runs out, restart training INT times with annealed learning rate (default: %(default)s)")
    validation.add_argument('--anneal_decay', type=float, default=0.5, metavar='FLOAT',
                            help="learning rate decay on each restart (default: %(default)s)")
    validation.add_argument('--external_validation_script', type=str, default=None, metavar='PATH',
                            help="location of validation script (to run your favorite metric for validation) (default: %(default)s)")

    display = parser.add_argument_group('display parameters')
    display.add_argument('--dispFreq', type=int, default=1000, metavar='INT',
                         help="display loss after INT updates (default: %(default)s)")
    display.add_argument('--sampleFreq', type=int, default=10000, metavar='INT',
                         help="display some samples after INT updates (default: %(default)s)")

    mrt = parser.add_argument_group('minimum risk training parameters')
    mrt.add_argument('--mrt_alpha', type=float, default=0.005, metavar='FLOAT',
                     help="MRT alpha (default: %(default)s)")
    mrt.add_argument('--mrt_samples', type=int, default=100, metavar='INT',
                     help="samples per source sentence (default: %(default)s)")
    mrt.add_argument('--mrt_samples_meanloss', type=int, default=10, metavar='INT',
                     help="draw n independent samples to calculate mean loss (which is subtracted from loss) (default: %(default)s)")
    mrt.add_argument('--mrt_loss', type=str, default='SENTENCEBLEU n=4', metavar='STR',
                     help='loss used in MRT (default: %(default)s)')
    mrt.add_argument('--mrt_reference', action="store_true",
                     help='add reference to MRT samples.')
    mrt.add_argument('--mrt_ml_mix', type=float, default=0, metavar='FLOAT',
                     help="mix in ML objective in MRT training with this scaling factor (default: %(default)s)")

    domain_interpolation = parser.add_argument_group('domain interpolation parameters')
    domain_interpolation.add_argument('--use_domain_interpolation', action='store_true',
                                      dest='use_domain_interpolation',
                                      help="interpolate between an out-domain training corpus and an in-domain training corpus")
    domain_interpolation.add_argument('--domain_interpolation_min', type=float, default=0.1, metavar='FLOAT',
                                      help="minimum (initial) fraction of in-domain training data (default: %(default)s)")
    domain_interpolation.add_argument('--domain_interpolation_max', type=float, default=1.0, metavar='FLOAT',
                                      help="maximum fraction of in-domain training data (default: %(default)s)")
    domain_interpolation.add_argument('--domain_interpolation_inc', type=float, default=0.1, metavar='FLOAT',
                                      help="interpolation increment to be applied each time patience runs out, until "
                                           "maximum amount of interpolation is reached (default: %(default)s)")
    domain_interpolation.add_argument('--domain_interpolation_indomain_datasets', type=str, metavar='PATH', nargs=2,
                                      help="indomain parallel training corpus (source and target)")

    multi = parser.add_argument_group('multiple source input parameters')
    multi.add_argument('--extra_sources', type=str, metavar='PATH', nargs='+',
                       help="auxiliary parallel training corpus (source)", default=[])
    multi.add_argument('--extra_sources_types', type=str, metavar='STR', default=[], nargs='+',
                       choices=["s", "t"],
                       help="describe type of extra sources: s=source side or t=target side"
                            "same order as extra sources, separated by space.")
    multi.add_argument('--extra_source_dicts', type=str, metavar='PATH', nargs="+", default=[],
                       help="auxiliary network vocabularies (one per source factor) in order of extra inputs")
    multi.add_argument('--extra_source_dicts_nums', type=int, metavar='INT', nargs="+", default=[],
                       help="number of auxiliary network vocabularies per extra input (in the same order")
    multi.add_argument('--extra_valid_sources', type=str, metavar='PATH', default=[], nargs='+',
                       help="auxiliary parallel validation corpora (source)")
    multi.add_argument('--multisource_type', choices=("att-concat", "att-gate","att-gate2", "att-hier", "init-decoder"), default=None)
    multi.add_argument('--extra_n_words_src', type=int, nargs="+", default=[], metavar='INT',
                         help="extra source vocabulary size (default: %(default)s)")
    multi.add_argument('--debugm', default=False, action='store_true')
    multi.add_argument('--tie_encoder_embeddings', default=False, action='store_true',
                       help="For several source encoders, tie their embeddings.")


    args = parser.parse_args()

    # set up logging
    _set_up_logging(args)

    # print vars(args)
    train(**vars(args))

# Profile peak GPU memory usage by uncommenting next line and enabling theano CUDA memory profiling (http://deeplearning.net/software/theano/tutorial/profiling.html)
#    print theano.sandbox.cuda.theano_allocated()
