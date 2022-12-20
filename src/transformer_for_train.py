
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Transformer for training."""
import numpy as np
from mindspore import jit

from mindspore.common.initializer import initializer
import mindspore as ms
import mindspore.ops as ops
import mindspore.nn as nn
from mindspore.common.tensor import Tensor
from mindspore.common.parameter import Parameter
from mindspore.nn.wrap.grad_reducer import DistributedGradReducer
from mindspore.communication.management import get_group_size
from mindspore.context import ParallelMode

from .transformer_model import TransformerModel

GRADIENT_CLIP_TYPE = 1
GRADIENT_CLIP_VALUE = 5.0

clip_grad = ops.MultitypeFuncGraph("clip_grad")


@clip_grad.register("Number", "Number", "Tensor")
def _clip_grad(clip_type, clip_value, grad):
    """
    Clip gradients.

    Inputs:
        clip_type (int): The way to clip, 0 for 'value', 1 for 'norm'.
        clip_value (float): Specifies how much to clip.
        grad (tuple[Tensor]): Gradients.

    Outputs:
        tuple[Tensor], clipped gradients.
    """
    if clip_type not in (0, 1):
        return grad
    dt = ops.dtype(grad)
    if clip_type == 0:
        new_grad = ops.clip_by_value(grad, ops.cast(ops.tuple_to_array((-clip_value,)), dt),
                                     ops.cast(ops.tuple_to_array((clip_value,)), dt))
    else:
        new_grad = nn.ClipByNorm()(grad, ops.cast(ops.tuple_to_array((clip_value,)), dt))
    return new_grad


class TransformerTrainingLoss(nn.Cell):
    """
    Provide transformer training loss.

    Args:
        config (TransformerConfig): The config of Transformer.

    Returns:
        Tensor, total loss.
    """
    def __init__(self, config):
        super(TransformerTrainingLoss, self).__init__(auto_prefix=False)
        self.vocab_size = config.vocab_size
        self.onehot = ops.OneHot()
        self.on_value = Tensor(float(1 - config.label_smoothing), ms.float32)
        self.off_value = Tensor(config.label_smoothing / float(self.vocab_size - 1), ms.float32)
        self.reduce_sum = ops.ReduceSum()
        self.reduce_mean = ops.ReduceMean()
        self.reshape = ops.Reshape()
        self.last_idx = (-1,)
        self.flatten = ops.Flatten()
        self.neg = ops.Neg()
        self.cast = ops.Cast()
        self.batch_size = config.batch_size

    def construct(self, prediction_scores, label_ids, label_weights, seq_length):
        """Defines the computation performed."""
        flat_shape = (self.batch_size * seq_length,)
        label_ids = self.reshape(label_ids, flat_shape)
        label_weights = self.cast(self.reshape(label_weights, flat_shape), ms.float32)
        one_hot_labels = self.onehot(label_ids, self.vocab_size, self.on_value, self.off_value)

        per_example_loss = self.neg(self.reduce_sum(prediction_scores * one_hot_labels, self.last_idx))
        numerator = self.reduce_sum(label_weights * per_example_loss, ())
        denominator = self.reduce_sum(label_weights, ()) + \
                      self.cast(ops.tuple_to_array((1e-5,)), ms.float32)
        loss = numerator / denominator
        return loss


class TransformerNetworkWithLoss(nn.Cell):
    """
    Provide  transformer training loss through network.

    Args:
        config (TransformerConfig): The config of Transformer.
        is_training (bool): Specifies whether to use the training mode.
        use_one_hot_embeddings (bool): Specifies whether to use one-hot for embeddings. Default: False.

    Returns:
        Tensor, the loss of the network.
    """
    def __init__(self, config, is_training, use_one_hot_embeddings=False):
        super(TransformerNetworkWithLoss, self).__init__(auto_prefix=False)
        self.transformer = TransformerModel(config, is_training, use_one_hot_embeddings)
        self.loss = TransformerTrainingLoss(config)
        self.cast = ops.Cast()
        self.shape = ops.Shape()

    def construct(self,
                  source_ids,
                  source_mask,
                  target_ids,
                  target_mask,
                  label_ids,
                  label_weights):
        """Transformer network with loss."""
        prediction_scores = self.transformer(source_ids, source_mask, target_ids, target_mask)
        seq_length = self.shape(source_ids)[1]
        total_loss = self.loss(prediction_scores, label_ids, label_weights, seq_length)
        return self.cast(total_loss, ms.float32)


class TransformerTrainOneStepCell(nn.TrainOneStepCell):
    """
    Encapsulation class of transformer network training.

    Append an optimizer to the training network after that the construct
    function can be called to create the backward graph.

    Args:
        network (Cell): The training network. Note that loss function should have been added.
        optimizer (Optimizer): Optimizer for updating the weights.
        sens (Number): The adjust parameter. Default: 1.0.
    """
    def __init__(self, network, optimizer, sens=1.0):
        super(TransformerTrainOneStepCell, self).__init__(network, optimizer, sens)

        self.cast = ops.Cast()
        self.hyper_map = ops.HyperMap()
        self.enable_tuple_broaden = True

    def set_sens(self, value):
        self.sens = value

    @jit
    def clip_grads(self, grads):
        grads = self.hyper_map(ops.partial(clip_grad, GRADIENT_CLIP_TYPE, GRADIENT_CLIP_VALUE), grads)
        return grads

    def construct(self,
                  source_eos_ids,
                  source_eos_mask,
                  target_sos_ids,
                  target_sos_mask,
                  target_eos_ids,
                  target_eos_mask,):
        """Defines the computation performed."""
        source_ids = source_eos_ids
        source_mask = source_eos_mask
        target_ids = target_sos_ids
        target_mask = target_sos_mask
        label_ids = target_eos_ids
        label_weights = target_eos_mask

        weights = self.weights
        loss = self.network(source_ids,
                            source_mask,
                            target_ids,
                            target_mask,
                            label_ids,
                            label_weights)
        grads = self.grad(self.network, weights)(source_ids,
                                                 source_mask,
                                                 target_ids,
                                                 target_mask,
                                                 label_ids,
                                                 label_weights,
                                                 self.cast(ops.tuple_to_array((self.sens,)),
                                                           ms.float32))
        grads = self.clip_grads(grads)
        # apply grad reducer on grads
        grads = self.grad_reducer(grads)
        self.optimizer(grads)
        return loss


grad_scale = ops.MultitypeFuncGraph("grad_scale")
reciprocal = ops.Reciprocal()


@grad_scale.register("Tensor", "Tensor")
def tensor_grad_scale(scale, grad):
    return grad * ops.cast(reciprocal(scale), ops.dtype(grad))

_grad_overflow = ops.MultitypeFuncGraph("_grad_overflow")
grad_overflow = ops.FloatStatus()

@_grad_overflow.register("Tensor")
def _tensor_grad_overflow(grad):
    return grad_overflow(grad)

class TransformerTrainOneStepWithLossScaleCell(nn.TrainOneStepWithLossScaleCell):
    """
    Encapsulation class of Transformer network training.

    Append an optimizer to the training network after that the construct
    function can be called to create the backward graph.

    Args:
        network (Cell): The training network. Note that loss function should have been added.
        optimizer (Optimizer): Optimizer for updating the weights.
        scale_update_cell (Cell): Cell to do the loss scale. Default: None.
    """
    def __init__(self, network, optimizer, scale_update_cell=None):
        super(TransformerTrainOneStepWithLossScaleCell, self).__init__(network, optimizer, scale_update_cell)
        self.cast = ops.Cast()
        self.degree = 1
        if self.reducer_flag:
            self.degree = get_group_size()
            self.grad_reducer = DistributedGradReducer(optimizer.parameters, False, self.degree)

        self.loss_scale = None
        self.loss_scaling_manager = scale_update_cell
        if scale_update_cell:
            self.loss_scale = Parameter(Tensor(scale_update_cell.get_loss_scale(), dtype=ms.float32))
        self.enable_tuple_broaden = True

    @jit
    def clip_grads(self, grads):
        grads = self.hyper_map(ops.partial(clip_grad, GRADIENT_CLIP_TYPE, GRADIENT_CLIP_VALUE), grads)
        return grads

    @jit
    def clip_scale_grads(self, scale, grads):
        grads = self.hyper_map(ops.partial(grad_scale, scale * self.degree), grads)
        return grads

    def construct(self,
                  source_eos_ids,
                  source_eos_mask,
                  target_sos_ids,
                  target_sos_mask,
                  target_eos_ids,
                  target_eos_mask,
                  sens=None):
        """Defines the computation performed."""
        source_ids = source_eos_ids
        source_mask = source_eos_mask
        target_ids = target_sos_ids
        target_mask = target_sos_mask
        label_ids = target_eos_ids
        label_weights = target_eos_mask

        weights = self.weights
        loss = self.network(source_ids,
                            source_mask,
                            target_ids,
                            target_mask,
                            label_ids,
                            label_weights)
        if sens is None:
            scaling_sens = self.loss_scale
        else:
            scaling_sens = sens
        status, scaling_sens = self.start_overflow_check(loss, scaling_sens)
        grads = self.grad(self.network, weights)(source_ids,
                                                 source_mask,
                                                 target_ids,
                                                 target_mask,
                                                 label_ids,
                                                 label_weights,
                                                 self.cast(scaling_sens,
                                                           ms.float32))

        # apply grad reducer on grads
        grads = self.grad_reducer(grads)
        grads = self.clip_scale_grads(scaling_sens, grads)
        grads = self.clip_grads(grads)

        cond = self.get_overflow_status(status, grads)
        overflow = cond
        if sens is None:
            overflow = self.loss_scaling_manager(self.loss_scale, cond)
        if not overflow:
            self.optimizer(grads)
        return (loss, cond, scaling_sens.value())


cast = ops.Cast()
add_grads = ops.MultitypeFuncGraph("add_grads")


@add_grads.register("Tensor", "Tensor")
def _add_grads(accu_grad, grad):
    return accu_grad + cast(grad, ms.float32)

update_accu_grads = ops.MultitypeFuncGraph("update_accu_grads")

@update_accu_grads.register("Tensor", "Tensor")
def _update_accu_grads(accu_grad, grad):
    succ = True
    return ops.depend(succ, ops.assign(accu_grad, cast(grad, ms.float32)))

accumulate_accu_grads = ops.MultitypeFuncGraph("accumulate_accu_grads")

@accumulate_accu_grads.register("Tensor", "Tensor")
def _accumulate_accu_grads(accu_grad, grad):
    succ = True
    return ops.depend(succ, ops.assign_add(accu_grad, cast(grad, ms.float32)))


zeroslike = ops.ZerosLike()
reset_accu_grads = ops.MultitypeFuncGraph("reset_accu_grads")


@reset_accu_grads.register("Tensor")
def _reset_accu_grads(accu_grad):
    succ = True
    return ops.depend(succ, ops.assign(accu_grad, zeroslike(accu_grad)))


class TransformerTrainAccumulationAllReducePostWithLossScaleCell(nn.Cell):
    """
    Encapsulation class of bert network training.

    Append an optimizer to the training network after that the construct
    function can be called to create the backward graph.

    To mimic higher batch size, gradients are accumulated N times before weight update.

    For distribution mode, allreduce will only be implemented in the weight updated step,
    i.e. the sub-step after gradients accumulated N times.

    Args:
        network (Cell): The training network. Note that loss function should have been added.
        optimizer (Optimizer): Optimizer for updating the weights.
        scale_update_cell (Cell): Cell to do the loss scale. Default: None.
        accumulation_steps (int): Number of accumulation steps before gradient update. The global batch size =
                                batch_size * accumulation_steps. Default: 1.
    """

    def __init__(self, network, optimizer, scale_update_cell=None, accumulation_steps=8, enable_global_norm=False):
        super(TransformerTrainAccumulationAllReducePostWithLossScaleCell, self).__init__(auto_prefix=False)
        self.network = network
        self.network.set_grad()
        self.weights = optimizer.parameters
        self.optimizer = optimizer
        self.accumulation_steps = accumulation_steps
        self.enable_global_norm = enable_global_norm
        self.one = Tensor(np.array([1]).astype(np.int32))
        self.zero = Tensor(np.array([0]).astype(np.int32))
        self.local_step = Parameter(initializer(0, [1], ms.int32))
        self.accu_grads = self.weights.clone(prefix="accu_grads", init='zeros')
        self.accu_overflow = Parameter(initializer(0, [1], ms.int32))
        self.accu_loss = Parameter(initializer(0, [1], ms.float32))

        self.grad = ops.GradOperation(get_by_list=True, sens_param=True)
        self.reducer_flag = False
        self.parallel_mode = ms.get_auto_parallel_context("parallel_mode")
        if self.parallel_mode in [ParallelMode.DATA_PARALLEL, ParallelMode.HYBRID_PARALLEL]:
            self.reducer_flag = True
        self.grad_reducer = ops.identity
        self.degree = 1
        if self.reducer_flag:
            self.degree = get_group_size()
            self.grad_reducer = DistributedGradReducer(optimizer.parameters, False, self.degree)
        self.is_distributed = (self.parallel_mode != ParallelMode.STAND_ALONE)
        self.overflow_reducer = ops.identity
        if self.is_distributed:
            self.overflow_reducer = ops.AllReduce()
        self.cast = ops.Cast()
        self.alloc_status = ops.NPUAllocFloatStatus()
        self.get_status = ops.NPUGetFloatStatus()
        self.clear_status = ops.NPUClearFloatStatus()
        self.reduce_sum = ops.ReduceSum(keep_dims=False)
        self.base = Tensor(1, ms.float32)
        self.less_equal = ops.LessEqual()
        self.logical_or = ops.LogicalOr()
        self.not_equal = ops.NotEqual()
        self.select = ops.Select()
        self.reshape = ops.Reshape()
        self.hyper_map = ops.HyperMap()
        self.loss_scale = None
        self.loss_scaling_manager = scale_update_cell
        if scale_update_cell:
            self.loss_scale = Parameter(Tensor(scale_update_cell.get_loss_scale(), dtype=ms.float32))
        self.enable_tuple_broaden = True

    @jit
    def clip_grads(self, grads):
        grads = self.hyper_map(ops.partial(clip_grad, GRADIENT_CLIP_TYPE, GRADIENT_CLIP_VALUE), grads)
        return grads

    @jit
    def clip_scale_grads(self, scale, grads):
        grads = self.hyper_map(ops.partial(grad_scale, scale), grads)
        return grads

    @jit
    def clip_accumlate_hyper_map(self, grads):
        return self.hyper_map(accumulate_accu_grads, self.accu_grads, grads)

    @jit
    def clip_reset_hyper_map(self):
        return self.hyper_map(reset_accu_grads, self.accu_grads)


    def construct(self,
                  source_eos_ids,
                  source_eos_mask,
                  target_sos_ids,
                  target_sos_mask,
                  target_eos_ids,
                  target_eos_mask,
                  sens=None):
        """Defines the computation performed."""
        source_ids = source_eos_ids
        source_mask = source_eos_mask
        target_ids = target_sos_ids
        target_mask = target_sos_mask
        label_ids = target_eos_ids
        label_weights = target_eos_mask

        weights = self.weights
        loss = self.network(source_ids,
                            source_mask,
                            target_ids,
                            target_mask,
                            label_ids,
                            label_weights)
        if sens is None:
            scaling_sens = self.loss_scale
        else:
            scaling_sens = sens
        # alloc status and clear should be right before gradoperation
        init = self.alloc_status()
        init = ops.depend(init, loss)
        clear_status = self.clear_status(init)
        scaling_sens = ops.depend(scaling_sens, clear_status)
        # update accumulation parameters
        is_accu_step = self.not_equal(self.local_step, self.accumulation_steps)
        self.local_step = self.select(is_accu_step, self.local_step + self.one, self.one)
        self.accu_loss = self.select(is_accu_step, self.accu_loss + loss, loss)
        mean_loss = self.accu_loss / self.local_step
        is_accu_step = self.not_equal(self.local_step, self.accumulation_steps)

        grads = self.grad(self.network, weights)(source_ids,
                                                 source_mask,
                                                 target_ids,
                                                 target_mask,
                                                 label_ids,
                                                 label_weights,
                                                 self.cast(scaling_sens,
                                                           ms.float32))

        accu_succ = self.clip_accumlate_hyper_map(grads)
        mean_loss = ops.depend(mean_loss, accu_succ)

        init = ops.depend(init, mean_loss)
        get_status = self.get_status(init)
        init = ops.depend(init, get_status)
        flag_sum = self.reduce_sum(init, (0,))
        overflow = self.less_equal(self.base, flag_sum)
        overflow = self.logical_or(self.not_equal(self.accu_overflow, self.zero), overflow)
        accu_overflow = self.select(overflow, self.one, self.zero)
        self.accu_overflow = self.select(is_accu_step, accu_overflow, self.zero)

        if not is_accu_step:
            # apply grad reducer on grads
            grads = self.grad_reducer(self.accu_grads)
            scaling = scaling_sens * self.degree * self.accumulation_steps
            grads = self.clip_scale_grads(scaling, grads)
            if self.enable_global_norm:
                grads = ops.clip_by_global_norm(grads, 1.0, None)
            else:
                grads = self.clip_grads(grads)
            accu_overflow = ops.depend(accu_overflow, grads)
            accu_overflow = self.overflow_reducer(accu_overflow)
            overflow = self.less_equal(self.base, accu_overflow)
            accu_succ = self.clip_reset_hyper_map()
            overflow = ops.depend(overflow, accu_succ)
            overflow = self.reshape(overflow, (()))
            if sens is None:
                overflow = self.loss_scaling_manager(self.loss_scale, overflow)
            if not overflow:
                self.optimizer(grads)

        return (mean_loss, overflow, scaling_sens.value())
