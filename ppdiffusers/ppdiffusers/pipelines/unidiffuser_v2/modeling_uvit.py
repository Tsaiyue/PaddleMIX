import paddle
import math
from typing import Optional, Union
from ...configuration_utils import ConfigMixin, register_to_config
from ...models import ModelMixin
from ...models.attention import AdaLayerNorm, FeedForward
from ...models.attention_processor import Attention
from ...models.embeddings import TimestepEmbedding, Timesteps, get_2d_sincos_pos_embed
from ...models.transformer_2d import Transformer2DModelOutput
from ...utils import logging
logger = logging.get_logger(__name__)


def _no_grad_trunc_normal_(tensor, mean, std, a, b):
    def norm_cdf(x):
        return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

    if mean < a - 2 * std or mean > b + 2 * std:
        logger.warning(
            'mean is more than 2 std from [a, b] in nn.init.trunc_normal_. The distribution of values may be incorrect.'
        )
    with paddle.no_grad():
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)
        tensor = tensor.uniform(min=2 * l - 1, max=2 * u - 1)
        tensor = tensor.erfinv()
        tensor = tensor.multiply(std * math.sqrt(2.0))
        tensor = tensor.add(y=paddle.to_tensor(mean))
        tensor = tensor.clip(min=a, max=b)
        return tensor


def trunc_normal_(tensor, mean=0.0, std=1.0, a=-2.0, b=2.0):
    """Fills the input Tensor with values drawn from a truncated
    normal distribution. The values are effectively drawn from the normal distribution :math:`\\mathcal{N}(\\text{mean},
    \\text{std}^2)` with values outside :math:`[a, b]` redrawn until they are within the bounds. The method used for
    generating the random values works best when :math:`a \\leq \\text{mean} \\leq b`.

    Args:
        tensor: an n-dimensional `paddle.Tensor`
        mean: the mean of the normal distribution
        std: the standard deviation of the normal distribution
        a: the minimum cutoff value
        b: the maximum cutoff value
    Examples:
        >>> w = paddle.empty(3, 5) >>> nn.init.trunc_normal_(w)
    """
    return _no_grad_trunc_normal_(tensor, mean, std, a, b)


class PatchEmbed(paddle.nn.Layer):
    """2D Image to Patch Embedding"""

    def __init__(self,
                 height=224,
                 width=224,
                 patch_size=16,
                 in_channels=3,
                 embed_dim=768,
                 layer_norm=False,
                 flatten=True,
                 bias=True,
                 use_pos_embed=True):
        super().__init__()
        num_patches = height // patch_size * (width // patch_size)
        self.flatten = flatten
        self.layer_norm = layer_norm
        self.proj = paddle.nn.Conv2D(
            in_channels=in_channels,
            out_channels=embed_dim,
            kernel_size=(patch_size, patch_size),
            stride=patch_size,
            bias_attr=bias)
        if layer_norm:
            self.norm = paddle.nn.LayerNorm(
                normalized_shape=embed_dim,
                weight_attr=False,
                bias_attr=False,
                epsilon=1e-06)
        else:
            self.norm = None
        self.use_pos_embed = use_pos_embed
        if self.use_pos_embed:
            pos_embed = get_2d_sincos_pos_embed(embed_dim,
                                                int(num_patches**0.5))
            self.register_buffer(
                name='pos_embed',
                tensor=paddle.to_tensor(
                    data=pos_embed).astype(dtype='float32').unsqueeze(axis=0),
                persistable=False)

    def forward(self, latent):
        latent = self.proj(latent)
        if self.flatten:
            x = latent.flatten(start_axis=2)
            perm_67 = list(range(x.ndim))
            perm_67[1] = 2
            perm_67[2] = 1
            latent = x.transpose(perm=perm_67)
        if self.layer_norm:
            latent = self.norm(latent)
        if self.use_pos_embed:
            return latent + self.pos_embed
        else:
            return latent


class SkipBlock(paddle.nn.Layer):
    def __init__(self, dim: int):
        super().__init__()
        self.skip_linear = paddle.nn.Linear(
            in_features=2 * dim, out_features=dim)
        self.norm = paddle.nn.LayerNorm(normalized_shape=dim)

    def forward(self, x, skip):
        x = self.skip_linear(paddle.concat(x=[x, skip], axis=-1))
        x = self.norm(x)
        return x


class UTransformerBlock(paddle.nn.Layer):
    """
    A modification of BasicTransformerBlock which supports pre-LayerNorm and post-LayerNorm configurations.

    Parameters:
        dim (`int`): The number of channels in the input and output.
        num_attention_heads (`int`): The number of heads to use for multi-head attention.
        attention_head_dim (`int`): The number of channels in each head.
        dropout (`float`, *optional*, defaults to 0.0): The dropout probability to use.
        cross_attention_dim (`int`, *optional*): The size of the encoder_hidden_states vector for cross attention.
        activation_fn (`str`, *optional*, defaults to `"geglu"`):
            Activation function to be used in feed-forward.
        num_embeds_ada_norm (:obj: `int`, *optional*):
            The number of diffusion steps used during training. See `Transformer2DModel`.
        attention_bias (:obj: `bool`, *optional*, defaults to `False`):
            Configure if the attentions should contain a bias parameter.
        only_cross_attention (`bool`, *optional*):
            Whether to use only cross-attention layers. In this case two cross attention layers are used.
        double_self_attention (`bool`, *optional*):
            Whether to use two self-attention layers. In this case no cross attention layers are used.
        upcast_attention (`bool`, *optional*):
            Whether to upcast the query and key to float32 when performing the attention calculation.
        norm_elementwise_affine (`bool`, *optional*):
            Whether to use learnable per-element affine parameters during layer normalization.
        norm_type (`str`, defaults to `"layer_norm"`):
            The layer norm implementation to use.
        pre_layer_norm (`bool`, *optional*):
            Whether to perform layer normalization before the attention and feedforward operations ("pre-LayerNorm"),
            as opposed to after ("post-LayerNorm"). Note that `BasicTransformerBlock` uses pre-LayerNorm, e.g.
            `pre_layer_norm = True`.
        final_dropout (`bool`, *optional*):
            Whether to use a final Dropout layer after the feedforward network.
    """

    def __init__(self,
                 dim: int,
                 num_attention_heads: int,
                 attention_head_dim: int,
                 dropout=0.0,
                 cross_attention_dim: Optional[int]=None,
                 activation_fn: str='geglu',
                 num_embeds_ada_norm: Optional[int]=None,
                 attention_bias: bool=False,
                 only_cross_attention: bool=False,
                 double_self_attention: bool=False,
                 upcast_attention: bool=False,
                 norm_elementwise_affine: bool=True,
                 norm_type: str='layer_norm',
                 pre_layer_norm: bool=True,
                 final_dropout: bool=False):
        super().__init__()
        self.only_cross_attention = only_cross_attention
        self.use_ada_layer_norm = (num_embeds_ada_norm is not None and
                                   norm_type == 'ada_norm')
        self.pre_layer_norm = pre_layer_norm
        if norm_type in ('ada_norm', 'ada_norm_zero'
                         ) and num_embeds_ada_norm is None:
            raise ValueError(
                f'`norm_type` is set to {norm_type}, but `num_embeds_ada_norm` is not defined. Please make sure to define `num_embeds_ada_norm` if setting `norm_type` to {norm_type}.'
            )
        self.attn1 = Attention(
            query_dim=dim,
            heads=num_attention_heads,
            dim_head=attention_head_dim,
            dropout=dropout,
            bias=attention_bias,
            cross_attention_dim=cross_attention_dim
            if only_cross_attention else None,
            upcast_attention=upcast_attention)
        if cross_attention_dim is not None or double_self_attention:
            self.attn2 = Attention(
                query_dim=dim,
                cross_attention_dim=cross_attention_dim
                if not double_self_attention else None,
                heads=num_attention_heads,
                dim_head=attention_head_dim,
                dropout=dropout,
                bias=attention_bias,
                upcast_attention=upcast_attention)
        else:
            self.attn2 = None
        if self.use_ada_layer_norm:
            self.norm1 = AdaLayerNorm(dim, num_embeds_ada_norm)
        else:
            self.norm1 = paddle.nn.LayerNorm(
                normalized_shape=dim,
                weight_attr=norm_elementwise_affine,
                bias_attr=norm_elementwise_affine)
        if cross_attention_dim is not None or double_self_attention:
            self.norm2 = AdaLayerNorm(
                dim, num_embeds_ada_norm
            ) if self.use_ada_layer_norm else paddle.nn.LayerNorm(
                normalized_shape=dim,
                weight_attr=norm_elementwise_affine,
                bias_attr=norm_elementwise_affine)
        else:
            self.norm2 = None
        self.norm3 = paddle.nn.LayerNorm(
            normalized_shape=dim,
            weight_attr=norm_elementwise_affine,
            bias_attr=norm_elementwise_affine)
        self.ff = FeedForward(
            dim,
            dropout=dropout,
            activation_fn=activation_fn,
            final_dropout=final_dropout)

    def forward(self,
                hidden_states,
                attention_mask=None,
                encoder_hidden_states=None,
                encoder_attention_mask=None,
                timestep=None,
                cross_attention_kwargs=None,
                class_labels=None):
        if self.pre_layer_norm:
            if self.use_ada_layer_norm:
                norm_hidden_states = self.norm1(hidden_states, timestep)
            else:
                norm_hidden_states = self.norm1(hidden_states)
        else:
            norm_hidden_states = hidden_states
        cross_attention_kwargs = (cross_attention_kwargs if
                                  cross_attention_kwargs is not None else {})
        attn_output = self.attn1(
            norm_hidden_states,
            encoder_hidden_states=encoder_hidden_states
            if self.only_cross_attention else None,
            attention_mask=attention_mask,
            **cross_attention_kwargs)
        if not self.pre_layer_norm:
            if self.use_ada_layer_norm:
                attn_output = self.norm1(attn_output, timestep)
            else:
                attn_output = self.norm1(attn_output)
        hidden_states = attn_output + hidden_states
        if self.attn2 is not None:
            if self.pre_layer_norm:
                norm_hidden_states = self.norm2(
                    hidden_states,
                    timestep) if self.use_ada_layer_norm else self.norm2(
                        hidden_states)
            else:
                norm_hidden_states = hidden_states
            attn_output = self.attn2(
                norm_hidden_states,
                encoder_hidden_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                **cross_attention_kwargs)
            if not self.pre_layer_norm:
                attn_output = self.norm2(
                    attn_output,
                    timestep) if self.use_ada_layer_norm else self.norm2(
                        attn_output)
            hidden_states = attn_output + hidden_states
        if self.pre_layer_norm:
            norm_hidden_states = self.norm3(hidden_states)
        else:
            norm_hidden_states = hidden_states
        ff_output = self.ff(norm_hidden_states)
        if not self.pre_layer_norm:
            ff_output = self.norm3(ff_output)
        hidden_states = ff_output + hidden_states
        return hidden_states


class UniDiffuserBlock(paddle.nn.Layer):
    """
    A modification of BasicTransformerBlock which supports pre-LayerNorm and post-LayerNorm configurations and puts the
    LayerNorms on the residual backbone of the block. This matches the transformer block in the [original UniDiffuser
    implementation](https://github.com/thu-ml/unidiffuser/blob/main/libs/uvit_multi_post_ln_v1.py#L104).

    Parameters:
        dim (`int`): The number of channels in the input and output.
        num_attention_heads (`int`): The number of heads to use for multi-head attention.
        attention_head_dim (`int`): The number of channels in each head.
        dropout (`float`, *optional*, defaults to 0.0): The dropout probability to use.
        cross_attention_dim (`int`, *optional*): The size of the encoder_hidden_states vector for cross attention.
        activation_fn (`str`, *optional*, defaults to `"geglu"`):
            Activation function to be used in feed-forward.
        num_embeds_ada_norm (:obj: `int`, *optional*):
            The number of diffusion steps used during training. See `Transformer2DModel`.
        attention_bias (:obj: `bool`, *optional*, defaults to `False`):
            Configure if the attentions should contain a bias parameter.
        only_cross_attention (`bool`, *optional*):
            Whether to use only cross-attention layers. In this case two cross attention layers are used.
        double_self_attention (`bool`, *optional*):
            Whether to use two self-attention layers. In this case no cross attention layers are used.
        upcast_attention (`bool`, *optional*):
            Whether to upcast the query and key to float() when performing the attention calculation.
        norm_elementwise_affine (`bool`, *optional*):
            Whether to use learnable per-element affine parameters during layer normalization.
        norm_type (`str`, defaults to `"layer_norm"`):
            The layer norm implementation to use.
        pre_layer_norm (`bool`, *optional*):
            Whether to perform layer normalization before the attention and feedforward operations ("pre-LayerNorm"),
            as opposed to after ("post-LayerNorm"). The original UniDiffuser implementation is post-LayerNorm
            (`pre_layer_norm = False`).
        final_dropout (`bool`, *optional*):
            Whether to use a final Dropout layer after the feedforward network.
    """

    def __init__(self,
                 dim: int,
                 num_attention_heads: int,
                 attention_head_dim: int,
                 dropout=0.0,
                 cross_attention_dim: Optional[int]=None,
                 activation_fn: str='geglu',
                 num_embeds_ada_norm: Optional[int]=None,
                 attention_bias: bool=False,
                 only_cross_attention: bool=False,
                 double_self_attention: bool=False,
                 upcast_attention: bool=False,
                 norm_elementwise_affine: bool=True,
                 norm_type: str='layer_norm',
                 pre_layer_norm: bool=False,
                 final_dropout: bool=True):
        super().__init__()
        self.only_cross_attention = only_cross_attention
        self.use_ada_layer_norm = (num_embeds_ada_norm is not None and
                                   norm_type == 'ada_norm')
        self.pre_layer_norm = pre_layer_norm
        if norm_type in ('ada_norm', 'ada_norm_zero'
                         ) and num_embeds_ada_norm is None:
            raise ValueError(
                f'`norm_type` is set to {norm_type}, but `num_embeds_ada_norm` is not defined. Please make sure to define `num_embeds_ada_norm` if setting `norm_type` to {norm_type}.'
            )
        self.attn1 = Attention(
            query_dim=dim,
            heads=num_attention_heads,
            dim_head=attention_head_dim,
            dropout=dropout,
            bias=attention_bias,
            cross_attention_dim=cross_attention_dim
            if only_cross_attention else None,
            upcast_attention=upcast_attention)
        if cross_attention_dim is not None or double_self_attention:
            self.attn2 = Attention(
                query_dim=dim,
                cross_attention_dim=cross_attention_dim
                if not double_self_attention else None,
                heads=num_attention_heads,
                dim_head=attention_head_dim,
                dropout=dropout,
                bias=attention_bias,
                upcast_attention=upcast_attention)
        else:
            self.attn2 = None
        if self.use_ada_layer_norm:
            self.norm1 = AdaLayerNorm(dim, num_embeds_ada_norm)
        else:
            self.norm1 = paddle.nn.LayerNorm(
                normalized_shape=dim,
                weight_attr=norm_elementwise_affine,
                bias_attr=norm_elementwise_affine)
        if cross_attention_dim is not None or double_self_attention:
            self.norm2 = AdaLayerNorm(
                dim, num_embeds_ada_norm
            ) if self.use_ada_layer_norm else paddle.nn.LayerNorm(
                normalized_shape=dim,
                weight_attr=norm_elementwise_affine,
                bias_attr=norm_elementwise_affine)
        else:
            self.norm2 = None
        self.norm3 = paddle.nn.LayerNorm(
            normalized_shape=dim,
            weight_attr=norm_elementwise_affine,
            bias_attr=norm_elementwise_affine)
        self.ff = FeedForward(
            dim,
            dropout=dropout,
            activation_fn=activation_fn,
            final_dropout=final_dropout)

    def forward(self,
                hidden_states,
                attention_mask=None,
                encoder_hidden_states=None,
                encoder_attention_mask=None,
                timestep=None,
                cross_attention_kwargs=None,
                class_labels=None):
        if self.pre_layer_norm:
            if self.use_ada_layer_norm:
                hidden_states = self.norm1(hidden_states, timestep)
            else:
                hidden_states = self.norm1(hidden_states)
        cross_attention_kwargs = (cross_attention_kwargs if
                                  cross_attention_kwargs is not None else {})
        attn_output = self.attn1(
            hidden_states,
            encoder_hidden_states=encoder_hidden_states
            if self.only_cross_attention else None,
            attention_mask=attention_mask,
            **cross_attention_kwargs)
        hidden_states = attn_output + hidden_states
        if not self.pre_layer_norm:
            if self.use_ada_layer_norm:
                hidden_states = self.norm1(hidden_states, timestep)
            else:
                hidden_states = self.norm1(hidden_states)
        if self.attn2 is not None:
            if self.pre_layer_norm:
                hidden_states = self.norm2(
                    hidden_states,
                    timestep) if self.use_ada_layer_norm else self.norm2(
                        hidden_states)
            attn_output = self.attn2(
                hidden_states,
                encoder_hidden_states=encoder_hidden_states,
                attention_mask=encoder_attention_mask,
                **cross_attention_kwargs)
            hidden_states = attn_output + hidden_states
            if not self.pre_layer_norm:
                hidden_states = self.norm2(
                    hidden_states,
                    timestep) if self.use_ada_layer_norm else self.norm2(
                        hidden_states)
        if self.pre_layer_norm:
            hidden_states = self.norm3(hidden_states)
        ff_output = self.ff(hidden_states)
        hidden_states = ff_output + hidden_states
        if not self.pre_layer_norm:
            hidden_states = self.norm3(hidden_states)
        return hidden_states


class UTransformer2DModel(ModelMixin, ConfigMixin):
    """
    Transformer model based on the [U-ViT](https://github.com/baofff/U-ViT) architecture for image-like data. Compared
    to [`Transformer2DModel`], this model has skip connections between transformer blocks in a "U"-shaped fashion,
    similar to a U-Net. Supports only continuous (actual embeddings) inputs, which are embedded via a [`PatchEmbed`]
    layer and then reshaped to (b, t, d).

    Parameters:
        num_attention_heads (`int`, *optional*, defaults to 16): The number of heads to use for multi-head attention.
        attention_head_dim (`int`, *optional*, defaults to 88): The number of channels in each head.
        in_channels (`int`, *optional*):
            Pass if the input is continuous. The number of channels in the input.
        out_channels (`int`, *optional*):
            The number of output channels; if `None`, defaults to `in_channels`.
        num_layers (`int`, *optional*, defaults to 1): The number of layers of Transformer blocks to use.
        dropout (`float`, *optional*, defaults to 0.0): The dropout probability to use.
        norm_num_groups (`int`, *optional*, defaults to `32`):
            The number of groups to use when performing Group Normalization.
        cross_attention_dim (`int`, *optional*): The number of encoder_hidden_states dimensions to use.
        attention_bias (`bool`, *optional*):
            Configure if the TransformerBlocks' attention should contain a bias parameter.
        sample_size (`int`, *optional*): Pass if the input is discrete. The width of the latent images.
            Note that this is fixed at training time as it is used for learning a number of position embeddings. See
            `ImagePositionalEmbeddings`.
        num_vector_embeds (`int`, *optional*):
            Pass if the input is discrete. The number of classes of the vector embeddings of the latent pixels.
            Includes the class for the masked latent pixel.
        patch_size (`int`, *optional*, defaults to 2):
            The patch size to use in the patch embedding.
        activation_fn (`str`, *optional*, defaults to `"geglu"`): Activation function to be used in feed-forward.
        num_embeds_ada_norm ( `int`, *optional*): Pass if at least one of the norm_layers is `AdaLayerNorm`.
            The number of diffusion steps used during training. Note that this is fixed at training time as it is used
            to learn a number of embeddings that are added to the hidden states. During inference, you can denoise for
            up to but not more than steps than `num_embeds_ada_norm`.
        use_linear_projection (int, *optional*): TODO: Not used
        only_cross_attention (`bool`, *optional*):
            Whether to use only cross-attention layers. In this case two cross attention layers are used in each
            transformer block.
        upcast_attention (`bool`, *optional*):
            Whether to upcast the query and key to float() when performing the attention calculation.
        norm_type (`str`, *optional*, defaults to `"layer_norm"`):
            The Layer Normalization implementation to use. Defaults to `paddle.nn.LayerNorm`.
        block_type (`str`, *optional*, defaults to `"unidiffuser"`):
            The transformer block implementation to use. If `"unidiffuser"`, has the LayerNorms on the residual
            backbone of each transformer block; otherwise has them in the attention/feedforward branches (the standard
            behavior in `ppdiffusers`.)
        pre_layer_norm (`bool`, *optional*):
            Whether to perform layer normalization before the attention and feedforward operations ("pre-LayerNorm"),
            as opposed to after ("post-LayerNorm"). The original UniDiffuser implementation is post-LayerNorm
            (`pre_layer_norm = False`).
        norm_elementwise_affine (`bool`, *optional*):
            Whether to use learnable per-element affine parameters during layer normalization.
        use_patch_pos_embed (`bool`, *optional*):
            Whether to use position embeddings inside the patch embedding layer (`PatchEmbed`).
        final_dropout (`bool`, *optional*):
            Whether to use a final Dropout layer after the feedforward network.
    """

    @register_to_config
    def __init__(self,
                 num_attention_heads: int=16,
                 attention_head_dim: int=88,
                 in_channels: Optional[int]=None,
                 out_channels: Optional[int]=None,
                 num_layers: int=1,
                 dropout: float=0.0,
                 norm_num_groups: int=32,
                 cross_attention_dim: Optional[int]=None,
                 attention_bias: bool=False,
                 sample_size: Optional[int]=None,
                 num_vector_embeds: Optional[int]=None,
                 patch_size: Optional[int]=2,
                 activation_fn: str='geglu',
                 num_embeds_ada_norm: Optional[int]=None,
                 use_linear_projection: bool=False,
                 only_cross_attention: bool=False,
                 upcast_attention: bool=False,
                 norm_type: str='layer_norm',
                 block_type: str='unidiffuser',
                 pre_layer_norm: bool=False,
                 norm_elementwise_affine: bool=True,
                 use_patch_pos_embed=False,
                 ff_final_dropout: bool=False):
        super().__init__()
        self.use_linear_projection = use_linear_projection
        self.num_attention_heads = num_attention_heads
        self.attention_head_dim = attention_head_dim
        inner_dim = num_attention_heads * attention_head_dim
        assert in_channels is not None and patch_size is not None, 'Patch input requires in_channels and patch_size.'
        assert sample_size is not None, 'UTransformer2DModel over patched input must provide sample_size'
        self.height = sample_size
        self.width = sample_size
        self.patch_size = patch_size
        self.pos_embed = PatchEmbed(
            height=sample_size,
            width=sample_size,
            patch_size=patch_size,
            in_channels=in_channels,
            embed_dim=inner_dim,
            use_pos_embed=use_patch_pos_embed)
        if block_type == 'unidiffuser':
            block_cls = UniDiffuserBlock
        else:
            block_cls = UTransformerBlock
        self.transformer_in_blocks = paddle.nn.LayerList(sublayers=[
            block_cls(
                inner_dim,
                num_attention_heads,
                attention_head_dim,
                dropout=dropout,
                cross_attention_dim=cross_attention_dim,
                activation_fn=activation_fn,
                num_embeds_ada_norm=num_embeds_ada_norm,
                attention_bias=attention_bias,
                only_cross_attention=only_cross_attention,
                upcast_attention=upcast_attention,
                norm_type=norm_type,
                pre_layer_norm=pre_layer_norm,
                norm_elementwise_affine=norm_elementwise_affine,
                final_dropout=ff_final_dropout) for d in range(num_layers // 2)
        ])
        self.transformer_mid_block = block_cls(
            inner_dim,
            num_attention_heads,
            attention_head_dim,
            dropout=dropout,
            cross_attention_dim=cross_attention_dim,
            activation_fn=activation_fn,
            num_embeds_ada_norm=num_embeds_ada_norm,
            attention_bias=attention_bias,
            only_cross_attention=only_cross_attention,
            upcast_attention=upcast_attention,
            norm_type=norm_type,
            pre_layer_norm=pre_layer_norm,
            norm_elementwise_affine=norm_elementwise_affine,
            final_dropout=ff_final_dropout)
        self.transformer_out_blocks = paddle.nn.LayerList(sublayers=[
            paddle.nn.LayerDict(sublayers={
                'skip': SkipBlock(inner_dim),
                'block': block_cls(
                    inner_dim,
                    num_attention_heads,
                    attention_head_dim,
                    dropout=dropout,
                    cross_attention_dim=cross_attention_dim,
                    activation_fn=activation_fn,
                    num_embeds_ada_norm=num_embeds_ada_norm,
                    attention_bias=attention_bias,
                    only_cross_attention=only_cross_attention,
                    upcast_attention=upcast_attention,
                    norm_type=norm_type,
                    pre_layer_norm=pre_layer_norm,
                    norm_elementwise_affine=norm_elementwise_affine,
                    final_dropout=ff_final_dropout)
            }) for d in range(num_layers // 2)
        ])
        self.out_channels = (in_channels
                             if out_channels is None else out_channels)
        self.norm_out = paddle.nn.LayerNorm(normalized_shape=inner_dim)

    def forward(self,
                hidden_states,
                encoder_hidden_states=None,
                timestep=None,
                class_labels=None,
                cross_attention_kwargs=None,
                return_dict: bool=True,
                hidden_states_is_embedding: bool=False,
                unpatchify: bool=True):
        """
        Args:
            hidden_states ( When discrete, `paddle.Tensor` of shape `(batch size, num latent pixels)`.
                When continuous, `paddle.Tensor` of shape `(batch size, channel, height, width)`): Input
                hidden_states
            encoder_hidden_states ( `paddle.Tensor` of shape `(batch size, encoder_hidden_states dim)`, *optional*):
                Conditional embeddings for cross attention layer. If not given, cross-attention defaults to
                self-attention.
            timestep ( `paddle.int64`, *optional*):
                Optional timestep to be applied as an embedding in AdaLayerNorm's. Used to indicate denoising step.
            class_labels ( `paddle.Tensor` of shape `(batch size, num classes)`, *optional*):
                Optional class labels to be applied as an embedding in AdaLayerZeroNorm. Used to indicate class labels
                conditioning.
            cross_attention_kwargs (*optional*):
                Keyword arguments to supply to the cross attention layers, if used.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`models.unet_2d_condition.UNet2DConditionOutput`] instead of a plain tuple.
            hidden_states_is_embedding (`bool`, *optional*, defaults to `False`):
                Whether or not hidden_states is an embedding directly usable by the transformer. In this case we will
                ignore input handling (e.g. continuous, vectorized, etc.) and directly feed hidden_states into the
                transformer blocks.
            unpatchify (`bool`, *optional*, defaults to `True`):
                Whether to unpatchify the transformer output.

        Returns:
            [`~models.transformer_2d.Transformer2DModelOutput`] or `tuple`:
            [`~models.transformer_2d.Transformer2DModelOutput`] if `return_dict` is True, otherwise a `tuple`. When
            returning a tuple, the first element is the sample tensor.
        """
        if not unpatchify and return_dict:
            raise ValueError(
                f'Cannot both define `unpatchify`: {unpatchify} and `return_dict`: {return_dict} since when `unpatchify` is {unpatchify} the returned output is of shape (batch_size, seq_len, hidden_dim) rather than (batch_size, num_channels, height, width).'
            )
        if not hidden_states_is_embedding:
            hidden_states = self.pos_embed(hidden_states)
        skips = []
        for in_block in self.transformer_in_blocks:
            hidden_states = in_block(
                hidden_states,
                encoder_hidden_states=encoder_hidden_states,
                timestep=timestep,
                cross_attention_kwargs=cross_attention_kwargs,
                class_labels=class_labels)
            skips.append(hidden_states)
        hidden_states = self.transformer_mid_block(hidden_states)
        for out_block in self.transformer_out_blocks:
            hidden_states = out_block['skip'](hidden_states, skips.pop())
            hidden_states = out_block['block'](
                hidden_states,
                encoder_hidden_states=encoder_hidden_states,
                timestep=timestep,
                cross_attention_kwargs=cross_attention_kwargs,
                class_labels=class_labels)
        hidden_states = self.norm_out(hidden_states)
        if unpatchify:
            height = width = int(hidden_states.shape[1]**0.5)
            hidden_states = hidden_states.reshape(
                shape=(-1, height, width, self.patch_size, self.patch_size,
                       self.out_channels))
            hidden_states = paddle.einsum('nhwpqc->nchpwq', hidden_states)
            output = hidden_states.reshape(shape=(-1, self.out_channels,
                                                  height * self.patch_size,
                                                  width * self.patch_size))
        else:
            output = hidden_states
        if not return_dict:
            return output,
        return Transformer2DModelOutput(sample=output)


class UniDiffuserModel(ModelMixin, ConfigMixin):
    """
    Transformer model for a image-text [UniDiffuser](https://arxiv.org/pdf/2303.06555.pdf) model. This is a
    modification of [`UTransformer2DModel`] with input and output heads for the VAE-embedded latent image, the
    CLIP-embedded image, and the CLIP-embedded prompt (see paper for more details).

    Parameters:
        text_dim (`int`): The hidden dimension of the CLIP text model used to embed images.
        clip_img_dim (`int`): The hidden dimension of the CLIP vision model used to embed prompts.
        num_attention_heads (`int`, *optional*, defaults to 16): The number of heads to use for multi-head attention.
        attention_head_dim (`int`, *optional*, defaults to 88): The number of channels in each head.
        in_channels (`int`, *optional*):
            Pass if the input is continuous. The number of channels in the input.
        out_channels (`int`, *optional*):
            The number of output channels; if `None`, defaults to `in_channels`.
        num_layers (`int`, *optional*, defaults to 1): The number of layers of Transformer blocks to use.
        dropout (`float`, *optional*, defaults to 0.0): The dropout probability to use.
        norm_num_groups (`int`, *optional*, defaults to `32`):
            The number of groups to use when performing Group Normalization.
        cross_attention_dim (`int`, *optional*): The number of encoder_hidden_states dimensions to use.
        attention_bias (`bool`, *optional*):
            Configure if the TransformerBlocks' attention should contain a bias parameter.
        sample_size (`int`, *optional*): Pass if the input is discrete. The width of the latent images.
            Note that this is fixed at training time as it is used for learning a number of position embeddings. See
            `ImagePositionalEmbeddings`.
        num_vector_embeds (`int`, *optional*):
            Pass if the input is discrete. The number of classes of the vector embeddings of the latent pixels.
            Includes the class for the masked latent pixel.
        patch_size (`int`, *optional*, defaults to 2):
            The patch size to use in the patch embedding.
        activation_fn (`str`, *optional*, defaults to `"geglu"`): Activation function to be used in feed-forward.
        num_embeds_ada_norm ( `int`, *optional*): Pass if at least one of the norm_layers is `AdaLayerNorm`.
            The number of diffusion steps used during training. Note that this is fixed at training time as it is used
            to learn a number of embeddings that are added to the hidden states. During inference, you can denoise for
            up to but not more than steps than `num_embeds_ada_norm`.
        use_linear_projection (int, *optional*): TODO: Not used
        only_cross_attention (`bool`, *optional*):
            Whether to use only cross-attention layers. In this case two cross attention layers are used in each
            transformer block.
        upcast_attention (`bool`, *optional*):
            Whether to upcast the query and key to float32 when performing the attention calculation.
        norm_type (`str`, *optional*, defaults to `"layer_norm"`):
            The Layer Normalization implementation to use. Defaults to `paddle.nn.LayerNorm`.
        block_type (`str`, *optional*, defaults to `"unidiffuser"`):
            The transformer block implementation to use. If `"unidiffuser"`, has the LayerNorms on the residual
            backbone of each transformer block; otherwise has them in the attention/feedforward branches (the standard
            behavior in `ppdiffusers`.)
        pre_layer_norm (`bool`, *optional*):
            Whether to perform layer normalization before the attention and feedforward operations ("pre-LayerNorm"),
            as opposed to after ("post-LayerNorm"). The original UniDiffuser implementation is post-LayerNorm
            (`pre_layer_norm = False`).
        norm_elementwise_affine (`bool`, *optional*):
            Whether to use learnable per-element affine parameters during layer normalization.
        use_patch_pos_embed (`bool`, *optional*):
            Whether to use position embeddings inside the patch embedding layer (`PatchEmbed`).
        ff_final_dropout (`bool`, *optional*):
            Whether to use a final Dropout layer after the feedforward network.
        use_data_type_embedding (`bool`, *optional*):
            Whether to use a data type embedding. This is only relevant for UniDiffuser-v1 style models; UniDiffuser-v1
            is continue-trained from UniDiffuser-v0 on non-publically-available data and accepts a `data_type`
            argument, which can either be `1` to use the weights trained on non-publically-available data or `0`
            otherwise. This argument is subsequently embedded by the data type embedding, if used.
    """

    @register_to_config
    def __init__(self,
                 text_dim: int=768,
                 clip_img_dim: int=512,
                 num_text_tokens: int=77,
                 num_attention_heads: int=16,
                 attention_head_dim: int=88,
                 in_channels: Optional[int]=None,
                 out_channels: Optional[int]=None,
                 num_layers: int=1,
                 dropout: float=0.0,
                 norm_num_groups: int=32,
                 cross_attention_dim: Optional[int]=None,
                 attention_bias: bool=False,
                 sample_size: Optional[int]=None,
                 num_vector_embeds: Optional[int]=None,
                 patch_size: Optional[int]=None,
                 activation_fn: str='geglu',
                 num_embeds_ada_norm: Optional[int]=None,
                 use_linear_projection: bool=False,
                 only_cross_attention: bool=False,
                 upcast_attention: bool=False,
                 norm_type: str='layer_norm',
                 block_type: str='unidiffuser',
                 pre_layer_norm: bool=False,
                 use_timestep_embedding=False,
                 norm_elementwise_affine: bool=True,
                 use_patch_pos_embed=False,
                 ff_final_dropout: bool=True,
                 use_data_type_embedding: bool=False):
        super().__init__()
        self.inner_dim = num_attention_heads * attention_head_dim
        assert sample_size is not None, 'UniDiffuserModel over patched input must provide sample_size'
        self.sample_size = sample_size
        self.in_channels = in_channels
        self.out_channels = (in_channels
                             if out_channels is None else out_channels)
        self.patch_size = patch_size
        self.num_patches = self.sample_size // patch_size * (self.sample_size //
                                                             patch_size)
        self.vae_img_in = PatchEmbed(
            height=sample_size,
            width=sample_size,
            patch_size=patch_size,
            in_channels=in_channels,
            embed_dim=self.inner_dim,
            use_pos_embed=use_patch_pos_embed)
        self.clip_img_in = paddle.nn.Linear(
            in_features=clip_img_dim, out_features=self.inner_dim)
        self.text_in = paddle.nn.Linear(
            in_features=text_dim, out_features=self.inner_dim)
        self.timestep_img_proj = Timesteps(
            self.inner_dim, flip_sin_to_cos=True, downscale_freq_shift=0)
        self.timestep_img_embed = TimestepEmbedding(
            self.inner_dim, 4 * self.inner_dim, out_dim=self.
            inner_dim) if use_timestep_embedding else paddle.nn.Identity()
        self.timestep_text_proj = Timesteps(
            self.inner_dim, flip_sin_to_cos=True, downscale_freq_shift=0)
        self.timestep_text_embed = TimestepEmbedding(
            self.inner_dim, 4 * self.inner_dim, out_dim=self.
            inner_dim) if use_timestep_embedding else paddle.nn.Identity()
        self.num_text_tokens = num_text_tokens
        self.num_tokens = 1 + 1 + num_text_tokens + 1 + self.num_patches
        out_74 = paddle.create_parameter(
            shape=paddle.zeros(shape=[1, self.num_tokens,
                                      self.inner_dim]).shape,
            dtype=paddle.zeros(shape=[1, self.num_tokens,
                                      self.inner_dim]).numpy().dtype,
            default_initializer=paddle.nn.initializer.Assign(
                paddle.zeros(shape=[1, self.num_tokens, self.inner_dim])))
        out_74.stop_gradient = not True
        self.pos_embed = out_74
        self.pos_embed_drop = paddle.nn.Dropout(p=dropout)
        trunc_normal_(self.pos_embed, std=0.02)
        self.use_data_type_embedding = use_data_type_embedding
        if self.use_data_type_embedding:
            self.data_type_token_embedding = paddle.nn.Embedding(
                num_embeddings=2, embedding_dim=self.inner_dim)
            out_75 = paddle.create_parameter(
                shape=paddle.zeros(shape=[1, 1, self.inner_dim]).shape,
                dtype=paddle.zeros(shape=[1, 1, self.inner_dim]).numpy().dtype,
                default_initializer=paddle.nn.initializer.Assign(
                    paddle.zeros(shape=[1, 1, self.inner_dim])))
            out_75.stop_gradient = not True
            self.data_type_pos_embed_token = out_75
        self.transformer = UTransformer2DModel(
            num_attention_heads=num_attention_heads,
            attention_head_dim=attention_head_dim,
            in_channels=in_channels,
            out_channels=out_channels,
            num_layers=num_layers,
            dropout=dropout,
            norm_num_groups=norm_num_groups,
            cross_attention_dim=cross_attention_dim,
            attention_bias=attention_bias,
            sample_size=sample_size,
            num_vector_embeds=num_vector_embeds,
            patch_size=patch_size,
            activation_fn=activation_fn,
            num_embeds_ada_norm=num_embeds_ada_norm,
            use_linear_projection=use_linear_projection,
            only_cross_attention=only_cross_attention,
            upcast_attention=upcast_attention,
            norm_type=norm_type,
            block_type=block_type,
            pre_layer_norm=pre_layer_norm,
            norm_elementwise_affine=norm_elementwise_affine,
            use_patch_pos_embed=use_patch_pos_embed,
            ff_final_dropout=ff_final_dropout)
        patch_dim = patch_size**2 * out_channels
        self.vae_img_out = paddle.nn.Linear(
            in_features=self.inner_dim, out_features=patch_dim)
        self.clip_img_out = paddle.nn.Linear(
            in_features=self.inner_dim, out_features=clip_img_dim)
        self.text_out = paddle.nn.Linear(
            in_features=self.inner_dim, out_features=text_dim)

    def no_weight_decay(self):
        return {'pos_embed'}

    def forward(self,
                latent_image_embeds: paddle.Tensor,
                image_embeds: paddle.Tensor,
                prompt_embeds: paddle.Tensor,
                timestep_img: Union[paddle.Tensor, float, int],
                timestep_text: Union[paddle.Tensor, float, int],
                data_type: Optional[Union[paddle.Tensor, float, int]]=1,
                encoder_hidden_states=None,
                cross_attention_kwargs=None):
        """
        Args:
            latent_image_embeds (`paddle.Tensor` of shape `(batch size, latent channels, height, width)`):
                Latent image representation from the VAE encoder.
            image_embeds (`paddle.Tensor` of shape `(batch size, 1, clip_img_dim)`):
                CLIP-embedded image representation (unsqueezed in the first dimension).
            prompt_embeds (`paddle.Tensor` of shape `(batch size, seq_len, text_dim)`):
                CLIP-embedded text representation.
            timestep_img (`paddle.int64` or `float` or `int`):
                Current denoising step for the image.
            timestep_text (`paddle.int64` or `float` or `int`):
                Current denoising step for the text.
            data_type: (`paddle.int32` or `float` or `int`, *optional*, defaults to `1`):
                Only used in UniDiffuser-v1-style models. Can be either `1`, to use weights trained on nonpublic data,
                or `0` otherwise.
            encoder_hidden_states ( `paddle.Tensor` of shape `(batch size, encoder_hidden_states dim)`, *optional*):
                Conditional embeddings for cross attention layer. If not given, cross-attention defaults to
                self-attention.
            cross_attention_kwargs (*optional*):
                Keyword arguments to supply to the cross attention layers, if used.


        Returns:
            `tuple`: Returns relevant parts of the model's noise prediction: the first element of the tuple is tbe VAE
            image embedding, the second element is the CLIP image embedding, and the third element is the CLIP text
            embedding.
        """
        batch_size = latent_image_embeds.shape[0]
        vae_hidden_states = self.vae_img_in(latent_image_embeds)
        clip_hidden_states = self.clip_img_in(image_embeds)
        text_hidden_states = self.text_in(prompt_embeds)
        num_text_tokens, num_img_tokens = text_hidden_states.shape[
            1], vae_hidden_states.shape[1]
        if not paddle.is_tensor(x=timestep_img):
            timestep_img = paddle.to_tensor(data=[timestep_img], dtype='int64')
        timestep_img = timestep_img * paddle.ones(
            shape=batch_size, dtype=timestep_img.dtype)
        timestep_img_token = self.timestep_img_proj(timestep_img)
        timestep_img_token = timestep_img_token.cast(dtype=self.dtype)
        timestep_img_token = self.timestep_img_embed(timestep_img_token)
        timestep_img_token = timestep_img_token.unsqueeze(axis=1)
        if not paddle.is_tensor(x=timestep_text):
            timestep_text = paddle.to_tensor(
                data=[timestep_text], dtype='int64')
        timestep_text = timestep_text * paddle.ones(
            shape=batch_size, dtype=timestep_text.dtype)
        timestep_text_token = self.timestep_text_proj(timestep_text)
        timestep_text_token = timestep_text_token.cast(dtype=self.dtype)
        timestep_text_token = self.timestep_text_embed(timestep_text_token)
        timestep_text_token = timestep_text_token.unsqueeze(axis=1)
        if self.use_data_type_embedding:
            assert data_type is not None, 'data_type must be supplied if the model uses a data type embedding'
            if not paddle.is_tensor(x=data_type):
                data_type = paddle.to_tensor(data=[data_type], dtype='int32')
            data_type = data_type * paddle.ones(
                shape=batch_size, dtype=data_type.dtype)
            data_type_token = self.data_type_token_embedding(
                data_type).unsqueeze(axis=1)
            hidden_states = paddle.concat(
                x=[
                    timestep_img_token, timestep_text_token, data_type_token,
                    text_hidden_states, clip_hidden_states, vae_hidden_states
                ],
                axis=1)
        else:
            hidden_states = paddle.concat(
                x=[
                    timestep_img_token, timestep_text_token, text_hidden_states,
                    clip_hidden_states, vae_hidden_states
                ],
                axis=1)
        if self.use_data_type_embedding:
            pos_embed = paddle.concat(
                x=[
                    self.pos_embed[:, :1 + 1, :],
                    self.data_type_pos_embed_token, self.pos_embed[:, 1 + 1:, :]
                ],
                axis=1)
        else:
            pos_embed = self.pos_embed
        hidden_states = hidden_states + pos_embed
        hidden_states = self.pos_embed_drop(hidden_states)
        hidden_states = self.transformer(
            hidden_states,
            encoder_hidden_states=encoder_hidden_states,
            timestep=None,
            class_labels=None,
            cross_attention_kwargs=cross_attention_kwargs,
            return_dict=False,
            hidden_states_is_embedding=True,
            unpatchify=False)[0]
        if self.use_data_type_embedding:
            (t_img_token_out, t_text_token_out, data_type_token_out, text_out,
             img_clip_out, img_vae_out) = (hidden_states.split(
                 (1, 1, 1, num_text_tokens, 1, num_img_tokens), axis=1))
        else:
            (t_img_token_out, t_text_token_out, text_out, img_clip_out,
             img_vae_out) = (hidden_states.split(
                 (1, 1, num_text_tokens, 1, num_img_tokens), axis=1))
        img_vae_out = self.vae_img_out(img_vae_out)
        height = width = int(img_vae_out.shape[1]**0.5)
        img_vae_out = img_vae_out.reshape(
            shape=(-1, height, width, self.patch_size, self.patch_size,
                   self.out_channels))
        img_vae_out = paddle.einsum('nhwpqc->nchpwq', img_vae_out)
        img_vae_out = img_vae_out.reshape(shape=(-1, self.out_channels,
                                                 height * self.patch_size,
                                                 width * self.patch_size))
        img_clip_out = self.clip_img_out(img_clip_out)
        text_out = self.text_out(text_out)
        return img_vae_out, img_clip_out, text_out
