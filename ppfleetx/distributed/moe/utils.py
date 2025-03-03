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
#
# The file has been adapted from the file:
#     https://github.com/laekov/fastmoe/blob/master/fmoe/functions.py
#     Git commit hash: 295a615aacce7e54a37e7935274ba15e901c78e4
# We retain the following license from the original files:
#     Copyright 2021, Jiaao He. All rights reserved.
#   Licensed under the Apache License, Version 2.0 (the "License").

import paddle
from paddle.distributed.models.moe.utils import _number_count, _limit_by_capacity, _prune_gate_by_capacity, _assign_pos
from paddle.fluid.framework import in_dygraph_mode


def prepare_forward(gate, num_expert, world_size, moe_group):
    pos, local_expert_count, global_expert_count = count_by_gate(
        gate, num_expert, world_size, group=moe_group)
    with paddle.no_grad():
        fwd_expert_count = global_expert_count.reshape_(
            [world_size, num_expert]).sum(axis=0)
        fwd_batch_size = int(fwd_expert_count.sum().item())
    return (
        pos,
        local_expert_count,
        global_expert_count,
        fwd_expert_count,
        fwd_batch_size, )


def _alltoall(in_tensor_list, group=None, use_calc_stream=True):
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = paddle.distributed.collective._get_default_group(
        ) if group is None else group
        out = paddle.empty(in_tensor_list.shape, in_tensor_list.dtype)
        task = group.process_group.alltoall(in_tensor_list, out)
        task.wait()
        return out
    else:
        ring_id = 0 if group is None else group.id
        return paddle._legacy_C_ops.alltoall(in_tensor_list, 'use_calc_stream',
                                             use_calc_stream, 'ring_id',
                                             ring_id)


def _local_scatter(inp, pos):
    if pos.shape != [0]:
        inp_buf = paddle.index_select(inp, pos, 0)
    else:
        inp_buf = paddle.empty([0, inp.shape[1]], dtype=inp.dtype)
    return inp_buf


def _local_gather(inp, pos, out_batch_size, maybe_overlap=True):
    if pos.shape != [0]:
        origin_dtype = inp.dtype
        inp = paddle.cast(inp, dtype="float32")
        inp_buf = paddle.scatter(
            paddle.zeros(
                shape=[out_batch_size, inp.shape[-1]], dtype="float32"),
            pos,
            inp,
            overwrite=True)
        inp_buf = paddle.cast(inp_buf, dtype=origin_dtype)
    else:
        inp_buf = paddle.zeros(
            [out_batch_size, inp.shape[-1]], dtype=inp.dtype)
    return inp_buf


def _all_gather(tensor, group=None, use_calc_stream=True):
    if group is not None and not group.is_member():
        return

    if in_dygraph_mode():
        group = paddle.distributed.collective._get_default_group(
        ) if group is None else group
        tensor_shape = list(tensor.shape)
        tensor_shape[0] *= group.nranks
        out = paddle.empty(tensor_shape, tensor.dtype)

        task = group.process_group.all_gather(tensor, out)
        task.wait()
        return out
    else:
        ring_id = 0 if group is None else group.id
        nranks = paddle.distributed.collective._get_global_group(
        ).nranks if group is None else group.nranks
        return paddle._legacy_C_ops.c_allgather(tensor, 'use_calc_stream',
                                                use_calc_stream, 'ring_id',
                                                ring_id, 'nranks', nranks)


def count_by_gate(gate, num_expert, world_size, require_pos=True, group=None):
    total_expert_count = num_expert * world_size
    with paddle.no_grad():
        local_expert_count = _number_count(gate, total_expert_count)

        if world_size > 1:
            global_expert_count = _alltoall(local_expert_count, group=group)
        else:
            global_expert_count = local_expert_count
        if not require_pos:
            pos = None
        else:
            lec_cum = paddle.cumsum(local_expert_count, axis=0)
            pos = _assign_pos(gate, lec_cum)
    return pos, local_expert_count, global_expert_count


def limit_by_capacity(topk_idx, num_expert, world_size, capacity, group=None):
    with paddle.no_grad():
        capacity = paddle.ones(
            shape=[num_expert], dtype=paddle.int64) * capacity
        pos, lec, gec = count_by_gate(
            topk_idx, num_expert, world_size, require_pos=False, group=group)
        new_gec = _limit_by_capacity(gec, capacity, world_size)
        if world_size > 1:
            assert group.nranks == world_size
            new_lec = _alltoall(new_gec, group=group)
        else:
            new_lec = new_gec

        topk_idx = _prune_gate_by_capacity(topk_idx, new_lec, num_expert,
                                           world_size)

    return new_lec, new_gec, topk_idx
