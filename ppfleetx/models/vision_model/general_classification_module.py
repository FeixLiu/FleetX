# Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
# 
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
# 
#     http://www.apache.org/licenses/LICENSE-2.0
# 
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import sys
import copy
import importlib
from collections import defaultdict
import numpy as np

import paddle
import paddleslim
from paddle.static import InputSpec
from ppfleetx.utils.log import logger

from ppfleetx.core.module.basic_module import BasicModule

from .factory import build


class GeneralClsModule(BasicModule):
    def __init__(self, configs):
        self.nranks = paddle.distributed.get_world_size()
        self.model_configs = copy.deepcopy(configs.Model)
        self.model_configs.pop('module')
        self.quant_mode = False
        if 'Quantization' in configs and configs.Quantization.enable:
            self.quant_mode = True
            self.qat_config = copy.deepcopy(configs.Quantization)

        # must init before loss function
        super(GeneralClsModule, self).__init__(configs)

        assert 'train' in self.model_configs.loss
        self.loss_fn = build(self.model_configs.loss.train)
        self.eval_loss_fn = None
        if 'eval' in self.model_configs.loss:
            self.eval_loss_fn = build(self.model_configs.loss.eval)

        if 'train' in self.model_configs.metric:
            self.train_metric_fn = build(self.model_configs.metric.train)
        if 'eval' in self.model_configs.metric:
            self.eval_metric_fn = build(self.model_configs.metric.eval)

        self.train_batch_size = None
        self.eval_batch_size = None
        self.best_metric = 0.0
        self.acc_list = []

    def get_model(self):
        if not hasattr(self, 'model') or self.model is None:
            self.model = build(self.model_configs.model)

        if self.quant_mode:
            self.qat_model()

        return self.model

    def qat_model(self):
        self.quanter = paddleslim.dygraph.quant.QAT(config=self.qat_config)
        self.quanter.quantize(self.model)

    def forward(self, inputs):
        return self.model(inputs)

    def training_step(self, batch):
        inputs, labels = batch

        if self.train_batch_size is None:
            self.train_batch_size = inputs.shape[
                0] * paddle.distributed.get_world_size()

        inputs.stop_gradient = True
        labels.stop_gradient = True

        logits = self(inputs)
        loss = self.loss_fn(logits, labels)

        return loss

    def training_step_end(self, log_dict):
        ips = self.train_batch_size / log_dict['train_cost']
        logger.info(
            "[train] epoch: %d, step: [%d/%d], learning rate: %.7f, loss: %.9f, batch_cost: %.5f sec, ips: %.2f images/sec"
            % (log_dict['epoch'], log_dict['batch'], log_dict['total_batch'],
               log_dict['lr'], log_dict['loss'], log_dict['train_cost'], ips))

    def validation_step(self, batch):
        inputs, labels = batch

        batch_size = inputs.shape[0]

        inputs.stop_gradient = True
        labels.stop_gradient = True

        logits = self(inputs)
        loss = self.eval_loss_fn(logits, labels)

        if paddle.distributed.get_world_size() > 1:
            label_list = []
            paddle.distributed.all_gather(label_list, labels)
            labels = paddle.concat(label_list, 0)

            pred_list = []
            paddle.distributed.all_gather(pred_list, logits)
            logits = paddle.concat(pred_list, 0)

        if self.eval_batch_size is None:
            self.eval_batch_size = logits.shape[0]

        acc = self.eval_metric_fn(logits, labels)
        self.acc_list.append(acc)
        return loss

    def validation_step_end(self, log_dict):
        ips = self.eval_batch_size / log_dict['eval_cost']
        speed = self.configs['Engine']['logging_freq'] / log_dict['eval_cost']
        logger.info(
            "[eval] epoch: %d, step: [%d/%d], loss: %.9f, batch_cost: %.5f sec, ips: %.2f images/sec"
            % (log_dict['epoch'], log_dict['batch'], log_dict['total_batch'],
               log_dict['loss'], log_dict['eval_cost'], ips))

    def input_spec(self):
        return [
            InputSpec(
                shape=[None, 3, 224, 224], name="images", dtype='float32')
        ]

    def training_epoch_end(self, log_dict):
        logger.info("[Training] epoch: %d, total time: %.5f sec" %
                    (log_dict['epoch'], log_dict['train_cost']))

    def validation_epoch_end(self, log_dict):
        msg = ''
        if len(self.acc_list) > 0:
            ret = defaultdict(list)

            for item in self.acc_list:
                for key, val in item.items():
                    ret[key].append(val)

            for k, v in ret.items():
                ret[k] = np.mean(v)

            if 'metric' in ret and ret['metric'] > self.best_metric:
                self.best_metric = ret['metric']

            if 'metric' in ret:
                ret['best_metric'] = self.best_metric

            msg = ', '
            msg += ", ".join([f'{k} = {v:.6f}' for k, v in ret.items()])
            self.acc_list.clear()

        logger.info("[Eval] epoch: %d, total time: %.5f sec%s" %
                    (log_dict['epoch'], log_dict['eval_cost'], msg))
