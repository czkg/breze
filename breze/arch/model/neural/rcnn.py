from breze.arch.component import corrupt

__author__ = 'apuigdom'
# -*- coding: utf-8 -*-

import theano
import theano.tensor as T
from theano.tensor.nnet import conv
from theano.tensor.extra_ops import repeat
from theano.tensor.signal import downsample
from theano.sandbox.neighbours import images2neibs

from ...util import lookup
from ...component import transfer, loss as loss_
import numpy as np


# TODO document



def define_recurrent_params(spec, current_layer, size_hidden, recurrency):
    if recurrency == 'full':
        spec['recurrent_%i' % current_layer] = (size_hidden, size_hidden)
    elif recurrency == 'brnn':
        spec['recurrent_%i' % current_layer] = (size_hidden, size_hidden)
        spec['recurrent_%i_bwd' % current_layer] = (size_hidden, size_hidden)
        spec['initial_hiddens_%i_bwd' % current_layer] = size_hidden
    elif recurrency == 'lstm':
        spec['recurrent_%i' % current_layer] = (size_hidden, size_hidden * 4)
        spec['ingate_peephole_%i' % current_layer] = (size_hidden,)
        spec['outgate_peephole_%i' % current_layer] = (size_hidden,)
        spec['forgetgate_peephole_%i' % current_layer] = (size_hidden,)
    elif recurrency == 'single':
        spec['recurrent_%i' % current_layer] = size_hidden
    elif recurrency[0] == 'double':
        spec['recurrent_%i_0' % current_layer] = (size_hidden, recurrency[1])
        spec['recurrent_%i_1' % current_layer] = (recurrency[1], size_hidden)
    elif recurrency[0] == 'conv':
        spec['recurrent_%i' % current_layer] = (1, 1, recurrency[1], 1)
    else:
        raise AttributeError('Recurrency format is not correct')
    return spec


def parameters(n_inpt, n_hidden_conv, n_hidden_full, n_output,
               filter_shapes, image_shapes, recurrency):
    spec = dict(in_to_hidden=(n_hidden_conv[0], n_inpt[2],
                              filter_shapes[0][0], filter_shapes[0][1]),
                hidden_to_out=(n_hidden_full[-1], n_output),
                hidden_conv_bias_0=n_hidden_conv[0],
                out_bias=n_output)

    if recurrency[0]:
        size_hidden = np.prod(image_shapes[1][-3:])
        spec = define_recurrent_params(spec, 0, size_hidden, recurrency[0])
        spec['initial_hiddens_0'] = size_hidden

    zipped = zip(n_hidden_conv[:-1], n_hidden_conv[1:], filter_shapes[1:],
                 image_shapes[2:], recurrency[1:len(n_hidden_conv)])
    for i, (inlayer, outlayer, filter_shape, image_shape, rec) in enumerate(zipped):
        spec['hidden_conv_to_hidden_conv_%i' % i] = (
            outlayer, inlayer, filter_shape[0], filter_shape[1])
        spec['hidden_conv_bias_%i' % (i + 1)] = outlayer
        if rec:
            size_hidden = np.prod(image_shape[-3:])
            spec = define_recurrent_params(spec, i + 1, size_hidden, rec)
            spec['initial_hiddens_%i' % (i + 1)] = size_hidden

    current_layer = len(n_hidden_conv) + 1
    spec['hidden_conv_to_hidden_full'] = (np.prod(image_shapes[current_layer - 1][-3:]),
                                          image_shapes[current_layer][-1])
    size_hidden = image_shapes[current_layer][-1]
    spec['hidden_full_bias_0'] = size_hidden
    if recurrency[current_layer - 1]:
        if recurrency[current_layer - 1] == 'lstm':
            size_hidden /= 4
            spec['state_%i' % (current_layer-1)] = size_hidden
        spec = define_recurrent_params(spec, current_layer-1, size_hidden, recurrency[current_layer - 1])
        spec['initial_hiddens_%i' % (current_layer-1)] = size_hidden


    zipped = zip(n_hidden_full[:-1], n_hidden_full[1:],
                 recurrency[len(n_hidden_conv) + 1:])
    for i, (inlayer, outlayer, rec) in enumerate(zipped):
        spec['hidden_full_to_hidden_full_%i' % i] = (inlayer, outlayer)
        spec['hidden_full_bias_%i' % (i + 1)] = outlayer
        if rec:
            spec = define_recurrent_params(spec, i + 1 + len(n_hidden_conv), outlayer, rec)
            spec['initial_hiddens_%i' % (i + 1 + len(n_hidden_conv))] = outlayer
            if rec == 'lstm':
                spec['hidden_full_to_hidden_full_%i' % i] = (inlayer, 4 * outlayer)
                spec['hidden_full_bias_%i' % (i + 1)] = 4 * outlayer
                spec['state_%i' % (i + 1 + len(n_hidden_conv))] = outlayer


    print spec
    return spec


def recurrent_layer(hidden_inpt, hidden_to_hidden, f, initial_hidden, state,
                    rec_shape, rec_type, ingate_peephole=None,
                    outgate_peephole=None, forgetgate_peephole=None):

    def step_full(x, hi_tm1):
        h_tm1 = f(hi_tm1)
        hi = T.dot(h_tm1, hidden_to_hidden) + x
        return hi

    def step_single(x, hi_tm1):
        h_tm1 = f(hi_tm1)
        hi = (h_tm1 * hidden_to_hidden) + x
        return hi

    def step_double(x, hi_tm1):
        h_tm1 = f(hi_tm1)
        hi = T.dot(T.dot(h_tm1, hidden_to_hidden[0]), hidden_to_hidden[1]) + x
        return hi

    def step_conv(x, hi_tm1):
        h_tm1 = f(hi_tm1)
        h_tm1 = h_tm1.reshape((rec_shape[1], 1, rec_shape[2], 1))
        h_tm1 = conv.conv2d(h_tm1, hidden_to_hidden, filter_shape=(1, 1, rec_type[1], 1),
                            image_shape=(rec_shape[1], 1, rec_shape[2], 1), border_mode='full')
        h_tm1 = h_tm1[:, :, :rec_shape[2], :]
        h_tm1 = h_tm1.reshape((rec_shape[1], rec_shape[2]))
        hi = h_tm1 + x
        return hi

    def lstm_step(x_t, s_tm1, h_tm1):
        x_t += T.dot(h_tm1, hidden_to_hidden)

        inpt = T.tanh(x_t[:, :n_hidden_out])
        gates = x_t[:, n_hidden_out:]
        inpeep = s_tm1 * ingate_peephole
        outpeep = s_tm1 * outgate_peephole
        forgetpeep = s_tm1 * forgetgate_peephole

        ingate = f(gates[:, :n_hidden_out] + inpeep)
        forgetgate = f(
            gates[:, n_hidden_out:2 * n_hidden_out] + forgetpeep)
        outgate = f(gates[:, 2 * n_hidden_out:] + outpeep)

        s_t = inpt * ingate + s_tm1 * forgetgate
        h_t = f(s_t) * outgate
        return [s_t, h_t]

    if rec_type == 'full' or rec_type == 'brnn':
        step = step_full
    elif rec_type == 'single':
        step = step_single
    elif rec_type == 'lstm':
        pass
    elif rec_type[0] == 'double':
        step = step_double
    elif rec_type[0] == 'conv':
        step = step_conv
    else:
        raise AttributeError('Recurrency format is not correct')

    if rec_type == 'lstm':
        n_hidden_out = hidden_to_hidden.shape[0]
        initial_hidden_b = repeat(initial_hidden, hidden_inpt.shape[1], axis=0)
        initial_hidden_b = initial_hidden_b.reshape(
            (hidden_inpt.shape[1], n_hidden_out))
        initial_state_b = repeat(state, hidden_inpt.shape[1], axis=0)
        initial_state_b = initial_state_b.reshape(
            (hidden_inpt.shape[1], n_hidden_out))
        (_, hidden_in_rec), _ = theano.scan(
            lstm_step,
            sequences=hidden_inpt,
            outputs_info=[initial_state_b, initial_hidden_b]
            )

    else:
        # Modify the initial hidden state to obtain several copies of
        # it, one per sample.
        # TODO check if this is correct; FD-RNNs do it right.
        initial_hidden_b = repeat(initial_hidden, hidden_inpt.shape[1], axis=0)
        initial_hidden_b = initial_hidden_b.reshape(
            (hidden_inpt.shape[1], hidden_inpt.shape[2]))
        hidden_in_rec, _ = theano.scan(
            step,
            sequences=hidden_inpt,
            outputs_info=[initial_hidden_b])

    return hidden_in_rec


def conv_part(inpt, params, img_shape):
    w, b, fs, ps = params
    hidden_in = conv.conv2d(inpt, w, filter_shape=fs,
                            image_shape=img_shape)
    hidden_in_predown = downsample.max_pool_2d(
        hidden_in, ps, ignore_border=True)
    hidden_in_down = hidden_in_predown + b.dimshuffle('x', 0, 'x', 'x')
    return hidden_in_down


def feedforward_layer(inpt, weights, bias):
    n_time_steps = inpt.shape[0]
    n_samples = inpt.shape[1]

    n_inpt = weights.shape[0]
    n_output = weights.shape[1]

    inpt_flat = inpt.reshape((n_time_steps * n_samples, n_inpt))
    output_flat = T.dot(inpt_flat, weights)
    output = output_flat.reshape((n_time_steps, n_samples, n_output))
    output += bias.dimshuffle('x', 'x', 0)
    return output


def exprs(inpt, target, in_to_hidden, hidden_to_out, out_bias,
          hidden_conv_to_hidden_full, hidden_conv_to_hidden_conv,
          hidden_full_to_hidden_full, hidden_conv_bias,
          hidden_full_bias, hidden_conv_transfers,
          hidden_full_transfers, output_transfer, loss,
          image_shapes, filter_shapes_comp,
          pool_shapes, recurrents, initial_hiddens, weights,
          recurrent_types, p_dropout_inpt=False, p_dropout_conv=False,
          p_dropout_full=False, ingate_peephole=None, outgate_peephole=None,
          forgetgate_peephole=None, states=None, offline=False):
    if not p_dropout_inpt:
        p_dropout_inpt = 0
    if not p_dropout_conv:
        p_dropout_conv = [0] * len(hidden_conv_bias)
    if not p_dropout_full:
        p_dropout_full = [0] * (len(hidden_full_bias) - 1)
    if not isinstance(p_dropout_conv, list):
        p_dropout_conv = [p_dropout_conv] * len(hidden_conv_bias)
    if not isinstance(p_dropout_full, list):
        p_dropout_full = [p_dropout_full] * (len(hidden_full_bias) - 1)
    p_dropout_full += [0]

    print image_shapes, states
    # input shape = n_time_steps, n_samples, channels, n_frames_to_take, n_output
    #conv part: reshape to n_time_step * n_samples, channels, n_frames_to_take, n_output
    #rec part: reshape to n_time_steps, n_samples, channels * n_frames_to_take * n_output
    exprs = {}

    hidden = inpt
    if image_shapes[0][-2] != 1:
        n_time_steps, n_samples, channels, n_frames, n_features = list(image_shapes[0])
        hidden = hidden.reshape(((n_time_steps + n_frames - 1), n_samples, channels, 1, n_features))
        if not offline:
            hidden = T.concatenate([T.concatenate([hidden[j:j + 1, :, :, :, :] for j in range(i - n_frames, i)], axis=3)
                                    for i in range(n_frames, n_time_steps + n_frames)], axis=0)
        else:
            hidden = T.concatenate([T.concatenate([hidden[j:j + 1, :, :, :, :]
                                                   for j in range(i - n_frames/2, i + n_frames/2 + 1)], axis=3)
                                    for i in range(n_frames/2, n_time_steps + n_frames/2)], axis=0)
    # Convolutional part
    zipped = zip(image_shapes[1:], hidden_conv_transfers,
                 recurrents, recurrent_types, initial_hiddens, states, p_dropout_conv,
                 [in_to_hidden] + hidden_conv_to_hidden_conv, hidden_conv_bias,
                 filter_shapes_comp, pool_shapes)
    conv_shape = [np.prod(image_shapes[0][:2])] + list(image_shapes[0][2:])
    hidden = hidden.reshape(conv_shape)
    if p_dropout_inpt:
        hidden = corrupt.mask(hidden, p_dropout_inpt)
        hidden /= 1 - p_dropout_inpt
    for i, params in enumerate(zipped):
        image_shape, ft, rec, rec_type, ih, s, p_dropout = params[:7]
        f = lookup(ft, transfer)
        hidden_in_down = conv_part(hidden, params[7:], conv_shape)
        conv_shape = [np.prod(image_shape[:2])] + list(image_shape[2:])
        if rec is not None:
            rec_shape = list(image_shape[:2]) + [np.prod(image_shape[2:])]
            reshaped_hidden_in_conv_down = (hidden_in_down.reshape(image_shape)).reshape(rec_shape)

            if rec_type != 'brnn':
                hidden_in_rec = recurrent_layer(reshaped_hidden_in_conv_down, rec, f, ih, s, rec_shape, rec_type)
            else:
                hidden_in_rec_f = recurrent_layer(reshaped_hidden_in_conv_down, rec[0],
                                                  f, ih[0], s, rec_shape, rec_type)
                hidden_in_rec_b = recurrent_layer(reshaped_hidden_in_conv_down[::-1], rec[1],
                                                  f, ih[1], s, rec_shape, rec_type)

                hidden_in_rec = (hidden_in_rec_f + hidden_in_rec_b[::-1]) / 2.
            hidden_in_down = (hidden_in_rec.reshape(image_shape)).reshape(conv_shape)
        exprs['conv-hidden_in_%i' % i] = hidden_in_down
        hidden = f(hidden_in_down)
        if p_dropout:
            hidden = corrupt.mask(hidden, p_dropout)
            hidden /= 1 - p_dropout
        exprs['conv-hidden_%i' % i] = hidden


    # Non-conv part
    offset = len(hidden_conv_bias)
    zipped = zip([hidden_conv_to_hidden_full] + hidden_full_to_hidden_full,
                 hidden_full_bias, hidden_full_transfers, recurrents[offset:],
                 recurrent_types[offset:], initial_hiddens[offset:],
                 states[offset:],
                 ingate_peephole, outgate_peephole, forgetgate_peephole,
                 image_shapes[offset + 1:], p_dropout_full)
    image_shape = image_shapes[offset]
    rec_shape = list(image_shape[:2]) + [np.prod(image_shape[2:])]
    hidden = hidden.reshape(rec_shape)
    for i, (w, b, t, rec, rec_type, ih, s, ip, op, fp, image_shape, p_dropout) in enumerate(zipped):
        hidden_in = feedforward_layer(hidden, w, b)
        f = lookup(t, transfer)
        if rec is not None:
            if rec_type != 'brnn':
                hidden_in = recurrent_layer(hidden_in, rec, f, ih, s, image_shape, rec_type,
                                      ingate_peephole=ip, outgate_peephole=op,
                                      forgetgate_peephole=fp)
            else:
                hidden_in_f = recurrent_layer(hidden_in, rec[0], f, ih[0], s, image_shape, rec_type,
                                      ingate_peephole=ip, outgate_peephole=op, forgetgate_peephole=fp)
                hidden_in_b = recurrent_layer(hidden_in[::-1], rec[1], f, ih[1], s, image_shape, rec_type,
                      ingate_peephole=ip, outgate_peephole=op, forgetgate_peephole=fp)

                hidden_in = (hidden_in_f + hidden_in_b[::-1]) / 2.
        hidden = f(hidden_in)
        exprs['hidden_in_%i' % (i + offset + 1)] = hidden_in
        if p_dropout:
            hidden = corrupt.mask(hidden, p_dropout)
            hidden /= 1 - p_dropout
        exprs['hidden_%i' % (i + offset + 1)] = hidden

    f_output = lookup(output_transfer, transfer)

    output_in = feedforward_layer(hidden, hidden_to_out, out_bias)

    output = f_output(output_in)

    f_loss = lookup(loss, loss_)

    #TODO: Make this pretty
    if loss == 'fmeasure':
        loss_coordwise = f_loss(target, output*weights)#
        loss_samplewise = loss_coordwise
        overall_loss = loss_samplewise
    else:
        loss_coordwise = f_loss(target, output)#

        if weights is not None:
            loss_coordwise *= weights
        loss_samplewise = loss_coordwise.sum(axis=2)
        if weights is not None:
            weights_samplewise = weights.mean(axis=2)
            overall_loss = loss_samplewise.sum(axis=None) / weights_samplewise.sum(axis=None)
        else:
            overall_loss = loss_samplewise.mean()

    exprs.update({
        'loss_samplewise': loss_samplewise,
        'loss': overall_loss,
        'inpt': inpt,
        'target': target,
        'output_in': output_in,
        'output': output
    })

    return exprs