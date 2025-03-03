#   Copyright (c) 2022 PaddlePaddle Authors. All Rights Reserved.
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

import os
import io
import copy
import logging
import json

import paddle
import paddle.nn as nn
from paddle.nn import functional as F
from dataclasses import dataclass, field

from ..layers.model_outputs import BaseModelOutputWithPoolingAndCrossAttentions, ModelOutput
from ..layers.transformer import TransformerEncoderLayer, TransformerEncoder


class ErnieEmbeddings(nn.Layer):
    r"""
    Include embeddings from word, position and token_type embeddings.
    """

    def __init__(self,
                 vocab_size,
                 hidden_size=768,
                 hidden_dropout_prob=0.1,
                 max_position_embeddings=512,
                 type_vocab_size=2,
                 pad_token_id=0,
                 weight_attr=None,
                 task_type_vocab_size=3,
                 task_id=0,
                 use_task_id=False):
        super(ErnieEmbeddings, self).__init__()

        self.word_embeddings = nn.Embedding(
            vocab_size,
            hidden_size,
            padding_idx=pad_token_id,
            weight_attr=weight_attr)
        self.position_embeddings = nn.Embedding(
            max_position_embeddings, hidden_size, weight_attr=weight_attr)
        self.type_vocab_size = type_vocab_size
        if self.type_vocab_size > 0:
            self.token_type_embeddings = nn.Embedding(
                type_vocab_size, hidden_size, weight_attr=weight_attr)
        self.use_task_id = use_task_id
        self.task_id = task_id
        if self.use_task_id:
            self.task_type_embeddings = nn.Embedding(
                task_type_vocab_size, hidden_size, weight_attr=weight_attr)
        self.layer_norm = nn.LayerNorm(hidden_size)
        self.dropout = nn.Dropout(hidden_dropout_prob)

    def forward(self,
                input_ids,
                token_type_ids=None,
                position_ids=None,
                task_type_ids=None,
                inputs_embeds=None,
                past_key_values_length=None):
        if input_ids is not None:
            input_shape = paddle.shape(input_ids)
            input_embeddings = self.word_embeddings(input_ids)
        else:
            input_shape = paddle.shape(inputs_embeds)[:-1]
            input_embeddings = inputs_embeds

        if position_ids is None:
            # maybe need use shape op to unify static graph and dynamic graph
            #seq_length = input_ids.shape[1]
            ones = paddle.ones(input_shape, dtype="int64")
            seq_length = paddle.cumsum(ones, axis=1)
            position_ids = seq_length - ones
            if past_key_values_length is not None:
                position_ids += past_key_values_length
            position_ids.stop_gradient = True

        position_embeddings = self.position_embeddings(position_ids)
        embeddings = input_embeddings + position_embeddings

        if self.type_vocab_size > 0:
            if token_type_ids is None:
                token_type_ids = paddle.zeros(input_shape, dtype="int64")
            token_type_embeddings = self.token_type_embeddings(token_type_ids)
            embeddings = embeddings + token_type_embeddings

        if self.use_task_id:
            if task_type_ids is None:
                task_type_ids = paddle.ones(
                    input_shape, dtype="int64") * self.task_id
            task_type_embeddings = self.task_type_embeddings(task_type_ids)
            embeddings = embeddings + task_type_embeddings
        embeddings = self.layer_norm(embeddings)
        embeddings = self.dropout(embeddings)
        return embeddings


class ErniePooler(nn.Layer):
    def __init__(self, hidden_size, weight_attr=None):
        super(ErniePooler, self).__init__()
        self.dense = nn.Linear(
            hidden_size, hidden_size, weight_attr=weight_attr)
        self.activation = nn.Tanh()

    def forward(self, hidden_states):
        # We "pool" the model by simply taking the hidden state corresponding
        # to the first token.
        first_token_tensor = hidden_states[:, 0]
        pooled_output = self.dense(first_token_tensor)
        pooled_output = self.activation(pooled_output)
        return pooled_output


class ErnieModel(nn.Layer):
    r"""
    The bare ERNIE Model transformer outputting raw hidden-states.

    This model inherits from :class:`~paddlenlp.transformers.model_utils.PretrainedModel`.
    Refer to the superclass documentation for the generic methods.

    This model is also a Paddle `paddle.nn.Layer <https://www.paddlepaddle.org.cn/documentation
    /docs/en/api/paddle/fluid/dygraph/layers/Layer_en.html>`__ subclass. Use it as a regular Paddle Layer
    and refer to the Paddle documentation for all matter related to general usage and behavior.

    Args:
        vocab_size (int):
            Vocabulary size of `inputs_ids` in `ErnieModel`. Also is the vocab size of token embedding matrix.
            Defines the number of different tokens that can be represented by the `inputs_ids` passed when calling `ErnieModel`.
        hidden_size (int, optional):
            Dimensionality of the embedding layer, encoder layers and pooler layer. Defaults to `768`.
        num_hidden_layers (int, optional):
            Number of hidden layers in the Transformer encoder. Defaults to `12`.
        num_attention_heads (int, optional):
            Number of attention heads for each attention layer in the Transformer encoder.
            Defaults to `12`.
        intermediate_size (int, optional):
            Dimensionality of the feed-forward (ff) layer in the encoder. Input tensors
            to ff layers are firstly projected from `hidden_size` to `intermediate_size`,
            and then projected back to `hidden_size`. Typically `intermediate_size` is larger than `hidden_size`.
            Defaults to `3072`.
        hidden_act (str, optional):
            The non-linear activation function in the feed-forward layer.
            ``"gelu"``, ``"relu"`` and any other paddle supported activation functions
            are supported. Defaults to `"gelu"`.
        hidden_dropout_prob (float, optional):
            The dropout probability for all fully connected layers in the embeddings and encoder.
            Defaults to `0.1`.
        attention_probs_dropout_prob (float, optional):
            The dropout probability used in MultiHeadAttention in all encoder layers to drop some attention target.
            Defaults to `0.1`.
        max_position_embeddings (int, optional):
            The maximum value of the dimensionality of position encoding, which dictates the maximum supported length of an input
            sequence. Defaults to `512`.
        type_vocab_size (int, optional):
            The vocabulary size of the `token_type_ids`.
            Defaults to `2`.
        initializer_range (float, optional):
            The standard deviation of the normal initializer for initializing all weight matrices.
            Defaults to `0.02`.
            
            .. note::
                A normal_initializer initializes weight matrices as normal distributions.
                See :meth:`ErniePretrainedModel._init_weights()` for how weights are initialized in `ErnieModel`.

        pad_token_id(int, optional):
            The index of padding token in the token vocabulary.
            Defaults to `0`.

    """

    def __init__(self,
                 vocab_size,
                 hidden_size=768,
                 num_hidden_layers=12,
                 num_attention_heads=12,
                 intermediate_size=3072,
                 hidden_act="gelu",
                 hidden_dropout_prob=0.1,
                 attention_probs_dropout_prob=0.1,
                 max_position_embeddings=512,
                 type_vocab_size=2,
                 initializer_range=0.02,
                 pad_token_id=0,
                 task_type_vocab_size=3,
                 task_id=0,
                 use_task_id=False,
                 use_recompute=False):
        super(ErnieModel, self).__init__()
        self.pad_token_id = pad_token_id
        self.initializer_range = initializer_range

        self.hidden_size = hidden_size
        self.vocab_size = vocab_size
        self.hidden_act = hidden_act

        weight_attr = paddle.ParamAttr(
            initializer=nn.initializer.TruncatedNormal(
                mean=0.0, std=self.initializer_range))
        self.embeddings = ErnieEmbeddings(
            vocab_size, hidden_size, hidden_dropout_prob,
            max_position_embeddings, type_vocab_size, pad_token_id,
            weight_attr, task_type_vocab_size, task_id, use_task_id)

        encoder_layer = TransformerEncoderLayer(
            hidden_size,
            num_attention_heads,
            intermediate_size,
            dropout=hidden_dropout_prob,
            activation=hidden_act,
            attn_dropout=attention_probs_dropout_prob,
            act_dropout=0,
            weight_attr=weight_attr,
            normalize_before=False)
        self.encoder = TransformerEncoder(
            encoder_layer, num_hidden_layers, enable_recompute=use_recompute)

        self.pooler = ErniePooler(hidden_size, weight_attr)
        self.apply(self.init_weights)

    def get_input_embeddings(self):
        return self.embeddings.word_embeddings

    def set_input_embeddings(self, value):
        self.embeddings.word_embeddings = value

    def forward(self,
                input_ids,
                token_type_ids=None,
                position_ids=None,
                attention_mask=None,
                task_type_ids=None,
                past_key_values=None,
                inputs_embeds=None,
                use_cache=None,
                output_hidden_states=False,
                output_attentions=False,
                return_dict=False):
        r"""
        Args:
            input_ids (Tensor):
                Indices of input sequence tokens in the vocabulary. They are
                numerical representations of tokens that build the input sequence.
                It's data type should be `int64` and has a shape of [batch_size, sequence_length].
            token_type_ids (Tensor, optional):
                Segment token indices to indicate different portions of the inputs.
                Selected in the range ``[0, type_vocab_size - 1]``.
                If `type_vocab_size` is 2, which means the inputs have two portions.
                Indices can either be 0 or 1:

                - 0 corresponds to a *sentence A* token,
                - 1 corresponds to a *sentence B* token.

                Its data type should be `int64` and it has a shape of [batch_size, sequence_length].
                Defaults to `None`, which means we don't add segment embeddings.
            position_ids (Tensor, optional):
                Indices of positions of each input sequence tokens in the position embeddings. Selected in the range ``[0,
                max_position_embeddings - 1]``.
                Shape as `[batch_size, num_tokens]` and dtype as int64. Defaults to `None`.
            attention_mask (Tensor, optional):
                Mask used in multi-head attention to avoid performing attention on to some unwanted positions,
                usually the paddings or the subsequent positions.
                Its data type can be int, float and bool.
                When the data type is bool, the `masked` tokens have `False` values and the others have `True` values.
                When the data type is int, the `masked` tokens have `0` values and the others have `1` values.
                When the data type is float, the `masked` tokens have `-INF` values and the others have `0` values.
                It is a tensor with shape broadcasted to `[batch_size, num_attention_heads, sequence_length, sequence_length]`.
                For example, its shape can be  [batch_size, sequence_length], [batch_size, sequence_length, sequence_length],
                [batch_size, num_attention_heads, sequence_length, sequence_length].
                We use whole-word-mask in ERNIE, so the whole word will have the same value. For example, "使用" as a word,
                "使" and "用" will have the same value.
                Defaults to `None`, which means nothing needed to be prevented attention to.
             inputs_embeds (Tensor, optional):
                If you want to control how to convert `inputs_ids` indices into associated vectors, you can
                pass an embedded representation directly instead of passing `inputs_ids`.
            past_key_values (tuple(tuple(Tensor)), optional):
                The length of tuple equals to the number of layers, and each inner
                tuple haves 4 tensors of shape `(batch_size, num_heads, sequence_length - 1, embed_size_per_head)`)
                which contains precomputed key and value hidden states of the attention blocks.
                If `past_key_values` are used, the user can optionally input only the last `input_ids` (those that
                don't have their past key value states given to this model) of shape `(batch_size, 1)` instead of all
                `input_ids` of shape `(batch_size, sequence_length)`.
            use_cache (`bool`, optional):
                If set to `True`, `past_key_values` key value states are returned.
                Defaults to `None`.
            output_hidden_states (bool, optional):
                Whether to return the hidden states of all layers.
                Defaults to `False`.
            output_attentions (bool, optional):
                Whether to return the attentions tensors of all attention layers.
                Defaults to `False`.
            return_dict (bool, optional):
                Whether to return a :class:`~paddlenlp.transformers.model_outputs.ModelOutput` object. If `False`, the output
                will be a tuple of tensors. Defaults to `False`.

        Returns:
            An instance of :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPoolingAndCrossAttentions` if
            `return_dict=True`. Otherwise it returns a tuple of tensors corresponding
            to ordered and not None (depending on the input arguments) fields of
            :class:`~paddlenlp.transformers.model_outputs.BaseModelOutputWithPoolingAndCrossAttentions`.

        Example:
            .. code-block::

                import paddle
                from paddlenlp.transformers import ErnieModel, ErnieTokenizer

                tokenizer = ErnieTokenizer.from_pretrained('ernie-1.0')
                model = ErnieModel.from_pretrained('ernie-1.0')

                inputs = tokenizer("Welcome to use PaddlePaddle and PaddleNLP!")
                inputs = {k:paddle.to_tensor([v]) for (k, v) in inputs.items()}
                sequence_output, pooled_output = model(**inputs)

        """
        if input_ids is not None and inputs_embeds is not None:
            raise ValueError(
                "You cannot specify both input_ids and inputs_embeds at the same time."
            )
        elif input_ids is not None:
            input_shape = paddle.shape(input_ids)
        elif inputs_embeds is not None:
            input_shape = paddle.shape(inputs_embeds)[:-1]
        else:
            raise ValueError(
                "You have to specify either input_ids or inputs_embeds")

        past_key_values_length = None
        if past_key_values is not None:
            past_key_values_length = past_key_values[0][0].shape[2]

        if attention_mask is None:
            attention_mask = paddle.unsqueeze(
                (input_ids == self.pad_token_id
                 ).astype(self.pooler.dense.weight.dtype) * -1e4,
                axis=[1, 2])
            if past_key_values is not None:
                batch_size = past_key_values[0][0].shape[0]
                past_mask = paddle.zeros(
                    [batch_size, 1, 1, past_key_values_length],
                    dtype=attention_mask.dtype)
                attention_mask = paddle.concat(
                    [past_mask, attention_mask], axis=-1)
        # For 2D attention_mask from tokenizer
        elif attention_mask.ndim == 2:
            attention_mask = paddle.unsqueeze(
                attention_mask, axis=[1, 2]).astype(paddle.get_default_dtype())
            attention_mask = (1.0 - attention_mask) * -1e4
        attention_mask.stop_gradient = True

        embedding_output = self.embeddings(
            input_ids=input_ids,
            position_ids=position_ids,
            token_type_ids=token_type_ids,
            task_type_ids=task_type_ids,
            inputs_embeds=inputs_embeds,
            past_key_values_length=past_key_values_length)

        self.encoder._use_cache = use_cache  # To be consistent with HF
        encoder_outputs = self.encoder(
            embedding_output,
            src_mask=attention_mask,
            cache=past_key_values,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict)
        if isinstance(encoder_outputs, type(embedding_output)):
            sequence_output = encoder_outputs
            pooled_output = self.pooler(sequence_output)
            return (sequence_output, pooled_output)
        else:
            sequence_output = encoder_outputs[0]
            pooled_output = self.pooler(sequence_output)
            if not return_dict:
                return (sequence_output, pooled_output) + encoder_outputs[1:]
            return BaseModelOutputWithPoolingAndCrossAttentions(
                last_hidden_state=sequence_output,
                pooler_output=pooled_output,
                past_key_values=encoder_outputs.past_key_values,
                hidden_states=encoder_outputs.hidden_states,
                attentions=encoder_outputs.attentions)

    def init_weights(self, layer):
        """ Initialization hook """
        if isinstance(layer, (nn.Linear, nn.Embedding)):
            # only support dygraph, use truncated_normal and make it inplace
            # and configurable later
            if isinstance(layer.weight, paddle.Tensor):
                layer.weight.set_value(
                    paddle.tensor.normal(
                        mean=0.0,
                        std=self.initializer_range
                        if hasattr(self, "initializer_range") else
                        self.ernie.initializer_range,
                        shape=layer.weight.shape))
        elif isinstance(layer, nn.LayerNorm):
            layer._epsilon = 1e-12


class ErnieLMPredictionHead(nn.Layer):
    r"""
    Ernie Model with a `language modeling` head on top.
    """

    def __init__(
            self,
            hidden_size,
            vocab_size,
            activation,
            embedding_weights=None,
            weight_attr=None, ):
        super(ErnieLMPredictionHead, self).__init__()

        self.transform = nn.Linear(
            hidden_size, hidden_size, weight_attr=weight_attr)
        self.activation = getattr(nn.functional, activation)
        self.layer_norm = nn.LayerNorm(hidden_size)
        self.decoder_weight = self.create_parameter(
            shape=[vocab_size, hidden_size],
            dtype=self.transform.weight.dtype,
            attr=weight_attr,
            is_bias=False) if embedding_weights is None else embedding_weights
        self.decoder_bias = self.create_parameter(
            shape=[vocab_size], dtype=self.decoder_weight.dtype, is_bias=True)

    def forward(self, hidden_states, masked_positions=None):
        if masked_positions is not None:
            hidden_states = paddle.reshape(hidden_states,
                                           [-1, hidden_states.shape[-1]])
            hidden_states = paddle.tensor.gather(hidden_states,
                                                 masked_positions)
        # gather masked tokens might be more quick
        hidden_states = self.transform(hidden_states)
        hidden_states = self.activation(hidden_states)
        hidden_states = self.layer_norm(hidden_states)
        hidden_states = paddle.tensor.matmul(
            hidden_states, self.decoder_weight,
            transpose_y=True) + self.decoder_bias
        return hidden_states


class ErniePretrainingHeads(nn.Layer):
    def __init__(
            self,
            hidden_size,
            vocab_size,
            activation,
            embedding_weights=None,
            weight_attr=None, ):
        super(ErniePretrainingHeads, self).__init__()
        self.predictions = ErnieLMPredictionHead(hidden_size, vocab_size,
                                                 activation, embedding_weights,
                                                 weight_attr)
        self.seq_relationship = nn.Linear(
            hidden_size, 2, weight_attr=weight_attr)

    def forward(self, sequence_output, pooled_output, masked_positions=None):
        prediction_scores = self.predictions(sequence_output, masked_positions)
        seq_relationship_score = self.seq_relationship(pooled_output)
        return prediction_scores, seq_relationship_score


class ErnieForPretraining(nn.Layer):
    r"""
    Ernie Model with a `masked language modeling` head and a `sentence order prediction` head
    on top.

    """

    def __init__(self, ernie):
        super(ErnieForPretraining, self).__init__()
        self.ernie = ernie
        weight_attr = paddle.ParamAttr(
            initializer=nn.initializer.TruncatedNormal(
                mean=0.0, std=self.ernie.initializer_range))
        self.cls = ErniePretrainingHeads(
            self.ernie.hidden_size,
            self.ernie.vocab_size,
            self.ernie.hidden_act,
            embedding_weights=self.ernie.embeddings.word_embeddings.weight,
            weight_attr=weight_attr, )

        self.apply(self.init_weights)

    def forward(self,
                input_ids,
                token_type_ids=None,
                position_ids=None,
                attention_mask=None,
                masked_positions=None,
                inputs_embeds=None,
                labels=None,
                next_sentence_label=None,
                output_hidden_states=False,
                output_attentions=False,
                return_dict=False):
        r"""
        Args:
            input_ids (Tensor):
                See :class:`ErnieModel`.
            token_type_ids (Tensor, optional):
                See :class:`ErnieModel`.
            position_ids (Tensor, optional):
                See :class:`ErnieModel`.
            attention_mask (Tensor, optional):
                See :class:`ErnieModel`.
            inputs_embeds(Tensor, optional):
                See :class:`ErnieModel`.
            labels (Tensor of shape `(batch_size, sequence_length)`, optional):
                Labels for computing the masked language modeling loss. Indices should be in `[-100, 0, ...,
                vocab_size]` (see `input_ids` docstring) Tokens with indices set to `-100` are ignored (masked),
                the loss is only computed for the tokens with labels in `[0, ..., vocab_size]`.
            next_sentence_label (Tensor of shape `(batch_size,)`, optional):
                Labels for computing the next sequence prediction (classification) loss. Input should be a sequence
                pair (see `input_ids` docstring) Indices should be in `[0, 1]`:

                - 0 indicates sequence B is a continuation of sequence A,
                - 1 indicates sequence B is a random sequence.
            output_hidden_states (bool, optional):
                Whether to return the hidden states of all layers.
                Defaults to `False`.
            output_attentions (bool, optional):
                Whether to return the attentions tensors of all attention layers.
                Defaults to `False`.
            return_dict (bool, optional):
                Whether to return a :class:`~paddlenlp.transformers.bert.ErnieForPreTrainingOutput` object. If
                `False`, the output will be a tuple of tensors. Defaults to `False`.

        Returns:
            An instance of :class:`~paddlenlp.transformers.bert.ErnieForPreTrainingOutput` if `return_dict=True`.
            Otherwise it returns a tuple of tensors corresponding to ordered and
            not None (depending on the input arguments) fields of :class:`~paddlenlp.transformers.bert.ErnieForPreTrainingOutput`.

        """
        # with paddle.static.amp.fp16_guard():
        outputs = self.ernie(
            input_ids,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            attention_mask=attention_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict)
        sequence_output, pooled_output = outputs[:2]
        prediction_scores, seq_relationship_score = self.cls(
            sequence_output, pooled_output, masked_positions)

        total_loss = None
        if labels is not None and next_sentence_label is not None:
            loss_fct = paddle.nn.CrossEntropyLoss()
            masked_lm_loss = loss_fct(
                prediction_scores.reshape(
                    (-1, paddle.shape(prediction_scores)[-1])),
                labels.reshape((-1, )))
            next_sentence_loss = loss_fct(
                seq_relationship_score.reshape((-1, 2)),
                next_sentence_label.reshape((-1, )))
            total_loss = masked_lm_loss + next_sentence_loss
        if not return_dict:
            output = (prediction_scores, seq_relationship_score) + outputs[2:]
            return (
                (total_loss, ) + output) if total_loss is not None else output

        return ErnieForPreTrainingOutput(
            loss=total_loss,
            prediction_logits=prediction_scores,
            seq_relationship_logits=seq_relationship_score,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions, )

    def init_weights(self, layer):
        """ Initialization hook """
        if isinstance(layer, (nn.Linear, nn.Embedding)):
            # only support dygraph, use truncated_normal and make it inplace
            # and configurable later
            if isinstance(layer.weight, paddle.Tensor):
                layer.weight.set_value(
                    paddle.tensor.normal(
                        mean=0.0,
                        std=self.initializer_range
                        if hasattr(self, "initializer_range") else
                        self.ernie.initializer_range,
                        shape=layer.weight.shape))
        elif isinstance(layer, nn.LayerNorm):
            layer._epsilon = 1e-12


@dataclass
class ErnieForPreTrainingOutput(ModelOutput):
    """
    Output type of [`ErnieForPreTraining`].
    Args:
        loss (*optional*, returned when `labels` is provided, `paddle.Tensor` of shape `(1,)`):
            Total loss as the sum of the masked language modeling loss and the next sequence prediction
            (classification) loss.
        prediction_logits (`paddle.Tensor` of shape `(batch_size, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head (scores for each vocabulary token before SoftMax).
        seq_relationship_logits (`paddle.Tensor` of shape `(batch_size, 2)`):
            Prediction scores of the next sequence prediction (classification) head (scores of True/False continuation
            before SoftMax).
        hidden_states (`tuple(paddle.Tensor)`, *optional*, returned when `output_hidden_states=True` is passed or when `config.output_hidden_states=True`):
            Tuple of `paddle.Tensor` (one for the output of the embeddings + one for the output of each layer) of
            shape `(batch_size, sequence_length, hidden_size)`.
            Hidden-states of the model at the output of each layer plus the initial embedding outputs.
        attentions (`tuple(paddle.Tensor)`, *optional*, returned when `output_attentions=True` is passed or when `config.output_attentions=True`):
            Tuple of `paddle.Tensor` (one for each layer) of shape `(batch_size, num_heads, sequence_length,
            sequence_length)`.
            Attentions weights after the attention softmax, used to compute the weighted average in the self-attention
            heads.
    """

    loss = None
    prediction_logits = None
    seq_relationship_logits = None
    hidden_states = None
    attentions = None


class ErniePretrainingCriterion(paddle.nn.Layer):
    r"""
    The loss output of Ernie Model during the pretraining:
    a `masked language modeling` head and a `next sentence prediction (classification)` head.

    """

    def __init__(self, with_nsp_loss=True):
        super(ErniePretrainingCriterion, self).__init__()
        self.with_nsp_loss = with_nsp_loss
        #self.loss_fn = paddle.nn.loss.CrossEntropyLoss(ignore_index=-1)

    def forward(self,
                prediction_scores,
                seq_relationship_score,
                masked_lm_labels,
                next_sentence_labels=None):
        """
        Args:
            prediction_scores(Tensor):
                The scores of masked token prediction. Its data type should be float32.
                If `masked_positions` is None, its shape is [batch_size, sequence_length, vocab_size].
                Otherwise, its shape is [batch_size, mask_token_num, vocab_size]
            seq_relationship_score(Tensor):
                The scores of next sentence prediction. Its data type should be float32 and
                its shape is [batch_size, 2]
            masked_lm_labels(Tensor):
                The labels of the masked language modeling, its dimensionality is equal to `prediction_scores`.
                Its data type should be int64. If `masked_positions` is None, its shape is [batch_size, sequence_length, 1].
                Otherwise, its shape is [batch_size, mask_token_num, 1]
            next_sentence_labels(Tensor):
                The labels of the next sentence prediction task, the dimensionality of `next_sentence_labels`
                is equal to `seq_relation_labels`. Its data type should be int64 and
                its shape is [batch_size, 1]

        Returns:
            Tensor: The pretraining loss, equals to the sum of `masked_lm_loss` plus the mean of `next_sentence_loss`.
            Its data type should be float32 and its shape is [1].

        """

        with paddle.static.amp.fp16_guard():
            masked_lm_loss = F.cross_entropy(
                prediction_scores,
                masked_lm_labels,
                ignore_index=-1,
                reduction='none')

            if not self.with_nsp_loss:
                return paddle.mean(masked_lm_loss)

            next_sentence_loss = F.cross_entropy(
                seq_relationship_score, next_sentence_labels, reduction='none')
            return paddle.mean(masked_lm_loss), paddle.mean(next_sentence_loss)
