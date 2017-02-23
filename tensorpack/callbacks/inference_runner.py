#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: inference_runner.py
# Author: Yuxin Wu <ppwwyyxxc@gmail.com>

import tensorflow as tf
from collections import namedtuple
import six
from six.moves import zip, range

from ..utils import logger, get_tqdm
from ..dataflow import DataFlow
from ..tfutils.common import get_op_tensor_name
from ..tfutils import TowerContext
from ..train.input_data import TensorInput
from ..predict import PredictorTowerBuilder

from .base import Triggerable
from .inference import Inferencer

__all__ = ['InferenceRunner', 'FeedfreeInferenceRunner']


class OutputTensorDispatcher(object):
    def __init__(self):
        self._names = []
        self._idxs = []

    def add_entry(self, names):
        v = []
        for n in names:
            tensorname = get_op_tensor_name(n)[1]
            if tensorname in self._names:
                v.append(self._names.index(tensorname))
            else:
                self._names.append(tensorname)
                v.append(len(self._names) - 1)
        self._idxs.append(v)

    def get_all_names(self):
        return self._names

    def get_idx_for_each_entry(self):
        return self._idxs

    def get_names_for_each_entry(self):
        ret = []
        for t in self._idxs:
            ret.append([self._names[k] for k in t])
        return ret


def summary_inferencer(trainer, infs):
    for inf in infs:
        ret = inf.after_inference()
        for k, v in six.iteritems(ret):
            try:
                v = float(v)
            except:
                logger.warn("{} returns a non-scalar statistics!".format(type(inf).__name__))
                continue
            trainer.add_scalar_summary(k, v)


class InferenceRunner(Triggerable):
    """
    A callback that runs a list of :class:`Inferencer` on some
    :class:`DataFlow`.
    """

    _IOTensor = namedtuple('IOTensor', ['index', 'isOutput'])

    def __init__(self, ds, infs, input_tensor_names=None):
        """
        Args:
            ds (DataFlow): the DataFlow to run inferencer on.
            infs (list): a list of `Inferencer` instances.
            input_tensor_names(list): list of tensors to feed the dataflow to.
                Defaults to all the input placeholders.
        """
        assert isinstance(ds, DataFlow), ds
        self.ds = ds
        if not isinstance(infs, list):
            self.infs = [infs]
        else:
            self.infs = infs
        for v in self.infs:
            assert isinstance(v, Inferencer), v
        self.input_names = input_tensor_names  # names actually

    def _setup_graph(self):
        self._find_input_tensors()  # these are all tensor names
        self._find_output_tensors()  # may be either tensor name or op name
        self.predictor = self.trainer.get_predictor(
            self.input_names, self.output_names)

    def _find_input_tensors(self):
        if self.input_names is None:
            input_vars = self.trainer.model.get_reused_placehdrs()
            # TODO even if it works here, sparse still is unavailable
            # because get_tensor_by_name doesn't work for sparse

            def get_name(x):
                if isinstance(x, tf.SparseTensor):
                    return x.op.name.split('/')[0]
                return x.name
            self.input_names = [get_name(x) for x in input_vars]

    def _find_output_tensors(self):
        dispatcher = OutputTensorDispatcher()
        for inf in self.infs:
            dispatcher.add_entry(inf.get_output_tensors())
        all_names = dispatcher.get_all_names()

        IOTensor = InferenceRunner._IOTensor
        self.output_names = list(filter(
            lambda x: x not in self.input_names, all_names))

        def find_tensors(names):
            ret = []
            for name in names:
                if name in self.input_names:
                    ret.append(IOTensor(self.input_names.index(name), False))
                else:
                    ret.append(IOTensor(self.output_names.index(name), True))
            return ret
        self.inf_to_tensors = [find_tensors(t) for t in dispatcher.get_names_for_each_entry()]
        # list of list of IOTensor

    def _trigger(self):
        for inf in self.infs:
            inf.before_inference()

        self.ds.reset_state()
        with get_tqdm(total=self.ds.size()) as pbar:
            for dp in self.ds.get_data():
                outputs = self.predictor(dp)
                for inf, tensormap in zip(self.infs, self.inf_to_tensors):
                    inf_output = [(outputs if k.isOutput else dp)[k.index]
                                  for k in tensormap]
                    inf.datapoint(inf_output)
                pbar.update()
        self._write_summary_after_inference()

    def _write_summary_after_inference(self):
        summary_inferencer(self.trainer, self.infs)


class FeedfreeInferenceRunner(Triggerable):
    """ A callback that runs a list of :class:`Inferencer` on some
    :class:`FeedfreeInput`, such as some tensor from a TensorFlow data reading
    pipeline.
    """

    def __init__(self, input, infs, input_names=None, prefix=''):
        """
        Args:
            input (FeedfreeInput): the input to use. Must have ``size()``.
            infs (list): list of :class:`Inferencer` to run.
            input_names (list): must be a subset of the names in InputDesc.
            prefix(str): an prefix used to build the tower. Must be set
                differently if more than one :class:`FeedfreeInferenceRunner` are used.
        """
        assert isinstance(input, TensorInput), input
        self._input_data = input
        if not isinstance(infs, list):
            self.infs = [infs]
        else:
            self.infs = infs
        for v in self.infs:
            assert isinstance(v, Inferencer), v
        if input_names is not None:
            assert isinstance(input_names, list)
        self._input_names = input_names

        try:
            self._size = input.size()
        except NotImplementedError:
            raise ValueError("Input used in FeedfreeInferencecRunner must have a size!")
        self._prefix = prefix

    def _setup_graph(self):
        self._find_input_tensors()  # tensors

        # TODO can we reuse predictor factory?
        with tf.variable_scope(tf.get_variable_scope(), reuse=True):
            def fn(_):
                self.trainer.model.build_graph(self._input_tensors)
            PredictorTowerBuilder(fn, self._prefix).build(0)
        self._tower_prefix = TowerContext.get_predict_tower_name(0, self._prefix)

        self._find_output_tensors()

    def _find_input_tensors(self):
        self._input_data.setup(self.trainer.model)
        # only 1 prediction tower will be used for inference
        self._input_tensors = self._input_data.get_input_tensors()
        model_placehdrs = self.trainer.model.get_reused_placehdrs()
        if self._input_names is not None:
            raise NotImplementedError("Random code. Not tested.")
            assert len(self._input_names) == len(self._input_tensors), \
                "[FeedfreeInferenceRunner] input_names must have the same length as the input data."
            for n, tensor in zip(self.input_names, self._input_tensors):
                opname, _ = get_op_tensor_name(n)
                for idx, hdr in enumerate(model_placehdrs):
                    if hdr.name == opname:
                        model_placehdrs[idx] = tensor
                        break
                else:
                    raise ValueError(
                        "{} doesn't appear in the InputDesc of the model!".format(n))
            self._input_tensors = model_placehdrs

        assert len(self._input_tensors) == len(model_placehdrs), \
            "[FeedfreeInferenceRunner] Unmatched length of input tensors!"

    def _find_output_tensors(self):
        # TODO doesn't support output an input tensor
        dispatcher = OutputTensorDispatcher()
        for inf in self.infs:
            dispatcher.add_entry(inf.get_output_tensors())
        all_names = dispatcher.get_all_names()
        G = tf.get_default_graph()
        self._output_tensors = [G.get_tensor_by_name(
            self._tower_prefix + '/' + n) for n in all_names]

        # list of list of id
        self.inf_to_idxs = dispatcher.get_idx_for_each_entry()

    def _trigger(self):
        sess = tf.get_default_session()

        for inf in self.infs:
            inf.before_inference()

        with get_tqdm(total=self._size) as pbar:
            for _ in range(self._size):
                outputs = sess.run(fetches=self._output_tensors)
                for inf, idlist in zip(self.infs, self.inf_to_idxs):
                    inf_output = [outputs[k] for k in idlist]
                    inf.datapoint(inf_output)
                pbar.update()
        self._write_summary_after_inference()

    def _write_summary_after_inference(self):
        summary_inferencer(self.trainer, self.infs)
