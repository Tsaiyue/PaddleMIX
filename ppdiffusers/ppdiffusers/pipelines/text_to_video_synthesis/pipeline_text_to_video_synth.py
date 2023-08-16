# Copyright (c) 2023 PaddlePaddle Authors. All Rights Reserved.
# Copyright 2023 The HuggingFace Team. All rights reserved.
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

import inspect
from typing import Any, Callable, Dict, List, Optional, Union

import numpy as np
import paddle
import inspect
from typing import Any, Callable, Dict, List, Optional, Union
import numpy as np
from ...loaders import LoraLoaderMixin, TextualInversionLoaderMixin
from ...models import AutoencoderKL, UNet3DConditionModel
from ...schedulers import KarrasDiffusionSchedulers
from ...utils import logging, randn_tensor, replace_example_docstring
from ..pipeline_utils import DiffusionPipeline
from . import TextToVideoSDPipelineOutput
logger = logging.get_logger(__name__)
EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> import paddle
        >>> from ppdiffusers import TextToVideoSDPipeline
        >>> from ppdiffusers.utils import export_to_video

        >>> pipe = TextToVideoSDPipeline.from_pretrained(
        ...     "damo-vilab/text-to-video-ms-1.7b", paddle_dtype=paddle.float16, variant="fp16"
        ... )

        >>> prompt = "Spiderman is surfing"
        >>> video_frames = pipe(prompt).frames
        >>> video_path = export_to_video(video_frames)
        >>> video_path
        ```
"""


def tensor2vid(video: paddle.Tensor, mean=[0.5, 0.5, 0.5],
               std=[0.5, 0.5, 0.5]) -> List[np.ndarray]:
    mean = paddle.to_tensor(data=mean).reshape(1, -1, 1, 1, 1)
    std = paddle.to_tensor(data=std).reshape(1, -1, 1, 1, 1)
    video = video.multiply(std).add(y=paddle.to_tensor(mean))
    video.clip_(min=0, max=1)
    i, c, f, h, w = video.shape
    images = video.transpose(perm=[2, 3, 0, 4, 1]).reshape(f, h, i * w, c)
    images = images.unbind(axis=0)
    images = [(image.cpu().numpy() * 255).astype('uint8') for image in images]
    return images


class TextToVideoSDPipeline(DiffusionPipeline, TextualInversionLoaderMixin,
                            LoraLoaderMixin):
    """
    Pipeline for text-to-video generation.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder ([clip-vit-large-patch14](https://huggingface.co/openai/clip-vit-large-patch14)).
        tokenizer (`CLIPTokenizer`):
            A [`~transformers.CLIPTokenizer`] to tokenize text.
        unet ([`UNet3DConditionModel`]):
            A [`UNet3DConditionModel`] to denoise the encoded video latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
    """

    def __init__(self,
                 vae: AutoencoderKL,
                 text_encoder: CLIPTextModel,
                 tokenizer: CLIPTokenizer,
                 unet: UNet3DConditionModel,
                 scheduler: KarrasDiffusionSchedulers):
        super().__init__()
        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler)
        self.vae_scale_factor = 2**(len(self.vae.config.block_out_channels) - 1)

    def enable_vae_slicing(self):
        """
        Enable sliced VAE decoding. When this option is enabled, the VAE will split the input tensor in slices to
        compute decoding in several steps. This is useful to save some memory and allow larger batch sizes.
        """
        self.vae.enable_slicing()

    def disable_vae_slicing(self):
        """
        Disable sliced VAE decoding. If `enable_vae_slicing` was previously enabled, this method will go back to
        computing decoding in one step.
        """
        self.vae.disable_slicing()

    def enable_vae_tiling(self):
        """
        Enable tiled VAE decoding. When this option is enabled, the VAE will split the input tensor into tiles to
        compute decoding and encoding in several steps. This is useful for saving a large amount of memory and to allow
        processing larger images.
        """
        self.vae.enable_tiling()

    def disable_vae_tiling(self):
        """
        Disable tiled VAE decoding. If `enable_vae_tiling` was previously enabled, this method will go back to
        computing decoding in one step.
        """
        self.vae.disable_tiling()

    def _encode_prompt(self,
                       prompt,
                       num_images_per_prompt,
                       do_classifier_free_guidance,
                       negative_prompt=None,
                       prompt_embeds: Optional[paddle.Tensor]=None,
                       negative_prompt_embeds: Optional[paddle.Tensor]=None,
                       lora_scale: Optional[float]=None):
        """
        Encodes the prompt into text encoder hidden states.

        Args:
             prompt (`str` or `List[str]`, *optional*):
                prompt to be encoded
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            lora_scale (`float`, *optional*):
                A lora scale that will be applied to all LoRA layers of the text encoder if LoRA layers are loaded.
        """
        if lora_scale is not None and isinstance(self, LoraLoaderMixin):
            self._lora_scale = lora_scale
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]
        if prompt_embeds is None:
            if isinstance(self, TextualInversionLoaderMixin):
                prompt = self.maybe_convert_prompt(prompt, self.tokenizer)
            text_inputs = self.tokenizer(
                prompt,
                padding='max_length',
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors='pd')
            text_input_ids = text_inputs.input_ids
            untruncated_ids = self.tokenizer(
                prompt, padding='longest', return_tensors='pd').input_ids
            if untruncated_ids.shape[-1] >= text_input_ids.shape[
                    -1] and not paddle.equal_all(
                        x=text_input_ids, y=untruncated_ids).item():
                removed_text = self.tokenizer.batch_decode(
                    untruncated_ids[:, self.tokenizer.model_max_length - 1:-1])
                logger.warning(
                    f'The following part of your input was truncated because CLIP can only handle sequences up to {self.tokenizer.model_max_length} tokens: {removed_text}'
                )
            if hasattr(self.text_encoder.config, 'use_attention_mask'
                       ) and self.text_encoder.config.use_attention_mask:
                attention_mask = text_inputs.attention_mask
            else:
                attention_mask = None
            prompt_embeds = self.text_encoder(
                text_input_ids, attention_mask=attention_mask)
            prompt_embeds = prompt_embeds[0]
        prompt_embeds = prompt_embeds.cast(dtype=self.text_encoder.dtype)
        bs_embed, seq_len, _ = prompt_embeds.shape
        prompt_embeds = prompt_embeds.tile(
            repeat_times=[1, num_images_per_prompt, 1])
        prompt_embeds = prompt_embeds.reshape(
            [bs_embed * num_images_per_prompt, seq_len, -1])
        if do_classifier_free_guidance and negative_prompt_embeds is None:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [''] * batch_size
            elif prompt is not None and type(prompt) is not type(
                    negative_prompt):
                raise TypeError(
                    f'`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} != {type(prompt)}.'
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f'`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`: {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches the batch size of `prompt`.'
                )
            else:
                uncond_tokens = negative_prompt
            if isinstance(self, TextualInversionLoaderMixin):
                uncond_tokens = self.maybe_convert_prompt(uncond_tokens,
                                                          self.tokenizer)
            max_length = prompt_embeds.shape[1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding='max_length',
                max_length=max_length,
                truncation=True,
                return_tensors='pd')
            if hasattr(self.text_encoder.config, 'use_attention_mask'
                       ) and self.text_encoder.config.use_attention_mask:
                attention_mask = uncond_input.attention_mask
            else:
                attention_mask = None
            negative_prompt_embeds = self.text_encoder(
                uncond_input.input_ids, attention_mask=attention_mask)
            negative_prompt_embeds = negative_prompt_embeds[0]
        if do_classifier_free_guidance:
            seq_len = negative_prompt_embeds.shape[1]
            negative_prompt_embeds = negative_prompt_embeds.cast(
                dtype=self.text_encoder.dtype)
            negative_prompt_embeds = negative_prompt_embeds.tile(
                repeat_times=[1, num_images_per_prompt, 1])
            negative_prompt_embeds = negative_prompt_embeds.reshape(
                [batch_size * num_images_per_prompt, seq_len, -1])
            prompt_embeds = paddle.concat(
                x=[negative_prompt_embeds, prompt_embeds])
        return prompt_embeds

    def decode_latents(self, latents):
        latents = 1 / self.vae.config.scaling_factor * latents
        batch_size, channels, num_frames, height, width = latents.shape
        latents = latents.transpose(perm=[0, 2, 1, 3, 4]).reshape(
            batch_size * num_frames, channels, height, width)
        image = self.vae.decode(latents).sample
        video = image[None, :].reshape((batch_size, num_frames, -1) +
                                       image.shape[2:]).transpose(
                                           perm=[0, 2, 1, 3, 4])
        video = video.astype(dtype='float32')
        return video

    def prepare_extra_step_kwargs(self, generator, eta):
        accepts_eta = 'eta' in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs['eta'] = eta
        accepts_generator = 'generator' in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs['generator'] = generator
        return extra_step_kwargs

    def check_inputs(self,
                     prompt,
                     height,
                     width,
                     callback_steps,
                     negative_prompt=None,
                     prompt_embeds=None,
                     negative_prompt_embeds=None):
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f'`height` and `width` have to be divisible by 8 but are {height} and {width}.'
            )
        if callback_steps is None or callback_steps is not None and (
                not isinstance(callback_steps, int) or callback_steps <= 0):
            raise ValueError(
                f'`callback_steps` has to be a positive integer but is {callback_steps} of type {type(callback_steps)}.'
            )
        if prompt is not None and prompt_embeds is not None:
            raise ValueError(
                f'Cannot forward both `prompt`: {prompt} and `prompt_embeds`: {prompt_embeds}. Please make sure to only forward one of the two.'
            )
        elif prompt is None and prompt_embeds is None:
            raise ValueError(
                'Provide either `prompt` or `prompt_embeds`. Cannot leave both `prompt` and `prompt_embeds` undefined.'
            )
        elif prompt is not None and (not isinstance(prompt, str) and
                                     not isinstance(prompt, list)):
            raise ValueError(
                f'`prompt` has to be of type `str` or `list` but is {type(prompt)}'
            )
        if negative_prompt is not None and negative_prompt_embeds is not None:
            raise ValueError(
                f'Cannot forward both `negative_prompt`: {negative_prompt} and `negative_prompt_embeds`: {negative_prompt_embeds}. Please make sure to only forward one of the two.'
            )
        if prompt_embeds is not None and negative_prompt_embeds is not None:
            if prompt_embeds.shape != negative_prompt_embeds.shape:
                raise ValueError(
                    f'`prompt_embeds` and `negative_prompt_embeds` must have the same shape when passed directly, but got: `prompt_embeds` {prompt_embeds.shape} != `negative_prompt_embeds` {negative_prompt_embeds.shape}.'
                )

    def prepare_latents(self,
                        batch_size,
                        num_channels_latents,
                        num_frames,
                        height,
                        width,
                        dtype,
                        generator,
                        latents=None):
        shape = (batch_size, num_channels_latents, num_frames, height //
                 self.vae_scale_factor, width // self.vae_scale_factor)
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f'You have passed a list of generators of length {len(generator)}, but requested an effective batch size of {batch_size}. Make sure the batch size matches the length of the generators.'
            )
        if latents is None:
            latents = randn_tensor(shape, generator=generator, dtype=dtype)
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    @paddle.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
            self,
            prompt: Union[str, List[str]]=None,
            height: Optional[int]=None,
            width: Optional[int]=None,
            num_frames: int=16,
            num_inference_steps: int=50,
            guidance_scale: float=9.0,
            negative_prompt: Optional[Union[str, List[str]]]=None,
            eta: float=0.0,
            generator: Optional[Union[paddle.Generator, List[
                paddle.Generator]]]=None,
            latents: Optional[paddle.Tensor]=None,
            prompt_embeds: Optional[paddle.Tensor]=None,
            negative_prompt_embeds: Optional[paddle.Tensor]=None,
            output_type: Optional[str]='np',
            return_dict: bool=True,
            callback: Optional[Callable[[int, int, paddle.Tensor], None]]=None,
            callback_steps: int=1,
            cross_attention_kwargs: Optional[Dict[str, Any]]=None):
        """
        The call function to the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide image generation. If not defined, you need to pass `prompt_embeds`.
            height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The height in pixels of the generated video.
            width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The width in pixels of the generated video.
            num_frames (`int`, *optional*, defaults to 16):
                The number of video frames that are generated. Defaults to 16 frames which at 8 frames per seconds
                amounts to 2 seconds of video.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality videos at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                A higher guidance scale value encourages the model to generate images closely linked to the text
                `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide what to not include in image generation. If not defined, you need to
                pass `negative_prompt_embeds` instead. Ignored when not using guidance (`guidance_scale < 1`).
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) from the [DDIM](https://arxiv.org/abs/2010.02502) paper. Only applies
                to the [`~schedulers.DDIMScheduler`], and is ignored in other schedulers.
            generator (`paddle.Generator` or `List[paddle.Generator]`, *optional*):
                A [`paddle.Generator`](https://pytorch.org/docs/stable/generated/paddle.Generator.html) to make
                generation deterministic.
            latents (`paddle.Tensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for video
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`. Latents should be of shape
                `(batch_size, num_channel, num_frames, height, width)`.
            prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs (prompt weighting). If not
                provided, text embeddings are generated from the `prompt` input argument.
            negative_prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs (prompt weighting). If
                not provided, `negative_prompt_embeds` are generated from the `negative_prompt` input argument.
            output_type (`str`, *optional*, defaults to `"np"`):
                The output format of the generated video. Choose between `paddle.Tensor` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] instead
                of a plain tuple.
            callback (`Callable`, *optional*):
                A function that calls every `callback_steps` steps during inference. The function is called with the
                following arguments: `callback(step: int, timestep: int, latents: paddle.Tensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function is called. If not specified, the callback is called at
                every step.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the [`AttentionProcessor`] as defined in
                [`self.processor`](https://github.com/huggingface/diffusers/blob/main/src/diffusers/models/cross_attention.py).

        Examples:

        Returns:
            [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.text_to_video_synthesis.TextToVideoSDPipelineOutput`] is
                returned, otherwise a `tuple` is returned where the first element is a list with the generated frames.
        """
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor
        num_images_per_prompt = 1
        self.check_inputs(prompt, height, width, callback_steps,
                          negative_prompt, prompt_embeds,
                          negative_prompt_embeds)
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]
        do_classifier_free_guidance = guidance_scale > 1.0
        text_encoder_lora_scale = cross_attention_kwargs.get(
            'scale', None) if cross_attention_kwargs is not None else None
        prompt_embeds = self._encode_prompt(
            prompt,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            lora_scale=text_encoder_lora_scale)
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps = self.scheduler.timesteps
        num_channels_latents = self.unet.config.in_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt, num_channels_latents,
            num_frames, height, width, prompt_embeds.dtype, generator, latents)
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        num_warmup_steps = len(
            timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                latent_model_input = paddle.concat(
                    x=[latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(
                    latent_model_input, t)
                noise_pred = self.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=prompt_embeds,
                    cross_attention_kwargs=cross_attention_kwargs,
                    return_dict=False)[0]
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(
                        chunks=2)
                    noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond)
                bsz, channel, frames, width, height = latents.shape
                latents = latents.transpose(perm=[0, 2, 1, 3, 4]).reshape(
                    bsz * frames, channel, width, height)
                noise_pred = noise_pred.transpose(perm=[0, 2, 1, 3, 4]).reshape(
                    bsz * frames, channel, width, height)
                latents = self.scheduler.step(noise_pred, t, latents,
                                              **extra_step_kwargs).prev_sample
                latents = latents[None, :].reshape(
                    bsz, frames, channel, width,
                    height).transpose(perm=[0, 2, 1, 3, 4])
                if i == len(timesteps) - 1 or i + 1 > num_warmup_steps and (
                        i + 1) % self.scheduler.order == 0:
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)
        if output_type == 'latent':
            return TextToVideoSDPipelineOutput(frames=latents)
        video_tensor = self.decode_latents(latents)
        if output_type == 'pd':
            video = video_tensor
        else:
            video = tensor2vid(video_tensor)

        if not return_dict:
            return video,
        return TextToVideoSDPipelineOutput(frames=video)


import paddle

from paddlenlp.transformers import CLIPTextModel, CLIPTokenizer

from ...loaders import TextualInversionLoaderMixin
from ...models import AutoencoderKL, UNet3DConditionModel
from ...schedulers import KarrasDiffusionSchedulers
from ...utils import logging, randn_tensor, replace_example_docstring
from ..pipeline_utils import DiffusionPipeline
from . import TextToVideoSDPipelineOutput

logger = logging.get_logger(__name__)
EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> import paddle
        >>> from ppdiffusers import TextToVideoSDPipeline
        >>> from ppdiffusers.utils import export_to_video

        >>> pipe = TextToVideoSDPipeline.from_pretrained(
        ...     "damo-vilab/text-to-video-ms-1.7b",
        ... )

        >>> prompt = "Spiderman is surfing"
        >>> video_frames = pipe(prompt).frames
        >>> video_path = export_to_video(video_frames)
        >>> video_path
        ```
"""


def tensor2vid(video: paddle.Tensor, mean=[0.5, 0.5, 0.5],
               std=[0.5, 0.5, 0.5]) -> List[np.ndarray]:
    # This code is copied from https://github.com/modelscope/modelscope/blob/1509fdb973e5871f37148a4b5e5964cafd43e64d/modelscope/pipelines/multi_modal/text_to_video_synthesis_pipeline.py#L78
    # reshape to ncfhw
    mean = paddle.to_tensor(mean).reshape((1, -1, 1, 1, 1))
    std = paddle.to_tensor(std).reshape((1, -1, 1, 1, 1))
    video = video.multiply(std)
    video = video.add(mean)

    video.clip_(min=0, max=1)
    i, c, f, h, w = video.shape
    images = video.transpose([2, 3, 0, 4, 1]).reshape((f, h, i * w, c))
    images = images.unbind(axis=0)
    images = [(image.cpu().numpy() * 255).astype("uint8") for image in images]
    return images


class TextToVideoSDPipeline(DiffusionPipeline, TextualInversionLoaderMixin):
    """
    Pipeline for text-to-video generation.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) Model to encode and decode images to and from latent representations.
        text_encoder ([`CLIPTextModel`]):
            Frozen text-encoder. Same as Stable Diffusion 2.
        tokenizer (`CLIPTokenizer`):
            Tokenizer of class CLIPTokenizer.
        unet ([`UNet3DConditionModel`]): Conditional U-Net architecture to denoise the encoded video latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
    """

    def __init__(
            self,
            vae: AutoencoderKL,
            text_encoder: CLIPTextModel,
            tokenizer: CLIPTokenizer,
            unet: UNet3DConditionModel,
            scheduler: KarrasDiffusionSchedulers, ):
        super().__init__()
        self.register_modules(
            vae=vae,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            unet=unet,
            scheduler=scheduler, )
        self.vae_scale_factor = 2**(len(self.vae.config.block_out_channels) - 1)

    def _encode_prompt(
            self,
            prompt,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt=None,
            prompt_embeds: Optional[paddle.Tensor]=None,
            negative_prompt_embeds: Optional[paddle.Tensor]=None, ):
        """
        Encodes the prompt into text encoder hidden states.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                prompt to be encoded
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the image generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
        """
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]
        if prompt_embeds is None:
            # textual inversion: procecss multi-vector tokens if necessary
            if isinstance(self, TextualInversionLoaderMixin):
                prompt = self.maybe_convert_prompt(prompt, self.tokenizer)
            text_inputs = self.tokenizer(
                prompt,
                padding="max_length",
                max_length=self.tokenizer.model_max_length,
                truncation=True,
                return_tensors="pd", )
            text_input_ids = text_inputs.input_ids
            untruncated_ids = self.tokenizer(
                prompt, padding="longest", return_tensors="pd").input_ids
            if untruncated_ids.shape[-1] >= text_input_ids.shape[
                    -1] and not paddle.equal_all(text_input_ids,
                                                 untruncated_ids):
                removed_text = self.tokenizer.batch_decode(
                    untruncated_ids[:, self.tokenizer.model_max_length - 1:-1])
                logger.warning(
                    f"The following part of your input was truncated because CLIP can only handle sequences up to {self.tokenizer.model_max_length} tokens: {removed_text}"
                )
            if hasattr(self.text_encoder.config, "use_attention_mask"
                       ) and self.text_encoder.config.use_attention_mask:
                attention_mask = text_inputs.attention_mask
            else:
                attention_mask = None
            prompt_embeds = self.text_encoder(
                text_input_ids, attention_mask=attention_mask)
            prompt_embeds = prompt_embeds[0]
        prompt_embeds = prompt_embeds.cast(self.text_encoder.dtype)
        bs_embed, seq_len, _ = prompt_embeds.shape
        # duplicate text embeddings for each generation per prompt, using mps friendly method
        prompt_embeds = prompt_embeds.tile([1, num_images_per_prompt, 1])
        prompt_embeds = prompt_embeds.reshape(
            (bs_embed * num_images_per_prompt, seq_len, -1))
        # get unconditional embeddings for classifier free guidance
        if do_classifier_free_guidance and negative_prompt_embeds is None:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [""] * batch_size
            elif type(prompt) is not type(negative_prompt):
                raise TypeError(
                    f"`negative_prompt` should be the same type to `prompt`, but got {type(negative_prompt)} != {type(prompt)}."
                )
            elif isinstance(negative_prompt, str):
                uncond_tokens = [negative_prompt]
            elif batch_size != len(negative_prompt):
                raise ValueError(
                    f"`negative_prompt`: {negative_prompt} has batch size {len(negative_prompt)}, but `prompt`: {prompt} has batch size {batch_size}. Please make sure that passed `negative_prompt` matches the batch size of `prompt`."
                )
            else:
                uncond_tokens = negative_prompt

            # textual inversion: procecss multi-vector tokens if necessary
            if isinstance(self, TextualInversionLoaderMixin):
                uncond_tokens = self.maybe_convert_prompt(uncond_tokens,
                                                          self.tokenizer)
            max_length = prompt_embeds.shape[1]
            uncond_input = self.tokenizer(
                uncond_tokens,
                padding="max_length",
                max_length=max_length,
                truncation=True,
                return_tensors="pd")
            if hasattr(self.text_encoder.config, "use_attention_mask"
                       ) and self.text_encoder.config.use_attention_mask:
                attention_mask = uncond_input.attention_mask
            else:
                attention_mask = None
            negative_prompt_embeds = self.text_encoder(
                uncond_input.input_ids, attention_mask=attention_mask)
            negative_prompt_embeds = negative_prompt_embeds[0]
        if do_classifier_free_guidance:
            # duplicate unconditional embeddings for each generation per prompt, using mps friendly method
            seq_len = negative_prompt_embeds.shape[1]
            negative_prompt_embeds = negative_prompt_embeds.cast(
                self.text_encoder.dtype)
            negative_prompt_embeds = negative_prompt_embeds.tile(
                [1, num_images_per_prompt, 1])
            negative_prompt_embeds = negative_prompt_embeds.reshape(
                (batch_size * num_images_per_prompt, seq_len, -1))
            # For classifier free guidance, we need to do two forward passes.
            # Here we concatenate the unconditional and text embeddings into a single batch
            # to avoid doing two forward passes
            prompt_embeds = paddle.concat(
                [negative_prompt_embeds, prompt_embeds])
        return prompt_embeds

    def decode_latents(self, latents):
        latents = 1 / self.vae.config.scaling_factor * latents
        batch_size, channels, num_frames, height, width = latents.shape
        latents = latents.transpose([0, 2, 1, 3, 4]).reshape(
            (batch_size * num_frames, channels, height, width))
        image = self.vae.decode(latents).sample
        video = (image[None, :].reshape((batch_size, num_frames, -1) + tuple(
            image.shape[2:])).transpose([0, 2, 1, 3, 4]))
        video = video.cast("float32")
        return video

    def prepare_extra_step_kwargs(self, generator, eta):
        # prepare extra kwargs for the scheduler step, since not all schedulers have the same signature
        # eta (η) is only used with the DDIMScheduler, it will be ignored for other schedulers.
        # eta corresponds to η in DDIM paper: https://arxiv.org/abs/2010.02502
        # and should be between [0, 1]
        accepts_eta = "eta" in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        extra_step_kwargs = {}
        if accepts_eta:
            extra_step_kwargs["eta"] = eta
        # check if the scheduler accepts generator
        accepts_generator = "generator" in set(
            inspect.signature(self.scheduler.step).parameters.keys())
        if accepts_generator:
            extra_step_kwargs["generator"] = generator
        return extra_step_kwargs

    def check_inputs(
            self,
            prompt,
            height,
            width,
            callback_steps,
            negative_prompt=None,
            prompt_embeds=None,
            negative_prompt_embeds=None, ):
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f"`height` and `width` have to be divisible by 8 but are {height} and {width}."
            )
        if (callback_steps is None or callback_steps is not None and
            (not isinstance(callback_steps, int) or callback_steps <= 0)):
            raise ValueError(
                f"`callback_steps` has to be a positive integer but is {callback_steps} of type {type(callback_steps)}."
            )
        if prompt is not None and prompt_embeds is not None:
            raise ValueError(
                f"Cannot forward both `prompt`: {prompt} and `prompt_embeds`: {prompt_embeds}. Please make sure to only forward one of the two."
            )
        elif prompt is None and prompt_embeds is None:
            raise ValueError(
                "Provide either `prompt` or `prompt_embeds`. Cannot leave both `prompt` and `prompt_embeds` undefined."
            )
        elif prompt is not None and (not isinstance(prompt, str) and
                                     not isinstance(prompt, list)):
            raise ValueError(
                f"`prompt` has to be of type `str` or `list` but is {type(prompt)}"
            )
        if negative_prompt is not None and negative_prompt_embeds is not None:
            raise ValueError(
                f"Cannot forward both `negative_prompt`: {negative_prompt} and `negative_prompt_embeds`: {negative_prompt_embeds}. Please make sure to only forward one of the two."
            )
        if prompt_embeds is not None and negative_prompt_embeds is not None:
            if prompt_embeds.shape != negative_prompt_embeds.shape:
                raise ValueError(
                    f"`prompt_embeds` and `negative_prompt_embeds` must have the same shape when passed directly, but got: `prompt_embeds` {prompt_embeds.shape} != `negative_prompt_embeds` {negative_prompt_embeds.shape}."
                )

    def prepare_latents(self,
                        batch_size,
                        num_channels_latents,
                        num_frames,
                        height,
                        width,
                        dtype,
                        generator,
                        latents=None):
        shape = (
            batch_size,
            num_channels_latents,
            num_frames,
            height // self.vae_scale_factor,
            width // self.vae_scale_factor, )
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f"You have passed a list of generators of length {len(generator)}, but requested an effective batch size of {batch_size}. Make sure the batch size matches the length of the generators."
            )
        if latents is None:
            latents = randn_tensor(shape, generator=generator, dtype=dtype)

        latents = latents * self.scheduler.init_noise_sigma
        return latents

    @paddle.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
            self,
            prompt: Union[str, List[str]]=None,
            height: Optional[int]=None,
            width: Optional[int]=None,
            num_frames: int=16,
            num_inference_steps: int=50,
            guidance_scale: float=9.0,
            negative_prompt: Optional[Union[str, List[str]]]=None,
            eta: float=0.0,
            generator: Optional[Union[paddle.Generator, List[
                paddle.Generator]]]=None,
            latents: Optional[paddle.Tensor]=None,
            prompt_embeds: Optional[paddle.Tensor]=None,
            negative_prompt_embeds: Optional[paddle.Tensor]=None,
            output_type: Optional[str]="np",
            return_dict: bool=True,
            callback: Optional[Callable[[int, int, paddle.Tensor], None]]=None,
            callback_steps: int=1,
            cross_attention_kwargs: Optional[Dict[str, Any]]=None, ):
        """
        Function invoked when calling the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts to guide the video generation. If not defined, one has to pass `prompt_embeds`.
                instead.
            height (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The height in pixels of the generated video.
            width (`int`, *optional*, defaults to self.unet.config.sample_size * self.vae_scale_factor):
                The width in pixels of the generated video.
            num_frames (`int`, *optional*, defaults to 16):
                The number of video frames that are generated. Defaults to 16 frames which at 8 frames per seconds
                amounts to 2 seconds of video.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality videos at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 7.5):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate videos that are closely linked to the text `prompt`,
                usually at the expense of lower video quality.
            negative_prompt (`str` or `List[str]`, *optional*):
                The prompt or prompts not to guide the video generation. If not defined, one has to pass
                `negative_prompt_embeds` instead. Ignored when not using guidance (i.e., ignored if `guidance_scale` is
                less than `1`).
            eta (`float`, *optional*, defaults to 0.0):
                Corresponds to parameter eta (η) in the DDIM paper: https://arxiv.org/abs/2010.02502. Only applies to
                [`schedulers.DDIMScheduler`], will be ignored for others.
            generator (`paddle.Generator` or `List[paddle.Generator]`, *optional*):
                One or a list of [paddle generator(s)]
                to make generation deterministic.
            latents (`paddle.Tensor`, *optional*):
                Pre-generated noisy latents, sampled from a Gaussian distribution, to be used as inputs for video
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor will ge generated by sampling using the supplied random `generator`. Latents should be of shape
                `(batch_size, num_channel, num_frames, height, width)`.
            prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt weighting. If not
                provided, text embeddings will be generated from `prompt` input argument.
            negative_prompt_embeds (`paddle.Tensor`, *optional*):
                Pre-generated negative text embeddings. Can be used to easily tweak text inputs, *e.g.* prompt
                weighting. If not provided, negative_prompt_embeds will be generated from `negative_prompt` input
                argument.
            output_type (`str`, *optional*, defaults to `"np"`):
                The output format of the generate video. Choose between `paddle.Tensor` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.TextToVideoSDPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that will be called every `callback_steps` steps during inference. The function will be
                called with the following arguments: `callback(step: int, timestep: int, latents: paddle.Tensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function will be called. If not specified, the callback will be
                called at every step.
            cross_attention_kwargs (`dict`, *optional*):
                A kwargs dictionary that if specified is passed along to the `AttentionProcessor` as defined under
                `self.processor` in
                [ppdiffusers.cross_attention](https://github.com/PaddlePaddle/PaddleMIX/blob/develop/ppdiffusers/ppdiffusers/models/cross_attention.py).


        Examples:

        Returns:
            [`~pipelines.stable_diffusion.TextToVideoSDPipelineOutput`] or `tuple`:
            [`~pipelines.stable_diffusion.TextToVideoSDPipelineOutput`] if `return_dict` is True, otherwise a `tuple.
            When returning a tuple, the first element is a list with the generated frames.
        """
        # 0. Default height and width to unet
        height = height or self.unet.config.sample_size * self.vae_scale_factor
        width = width or self.unet.config.sample_size * self.vae_scale_factor
        num_images_per_prompt = 1
        # 1. Check inputs. Raise error if not correct
        self.check_inputs(prompt, height, width, callback_steps,
                          negative_prompt, prompt_embeds,
                          negative_prompt_embeds)
        # 2. Define call parameters
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]
        # here `guidance_scale` is defined analog to the guidance weight `w` of equation (2)
        # of the Imagen paper: https://arxiv.org/pdf/2205.11487.pdf . `guidance_scale = 1`
        # corresponds to doing no classifier free guidance.
        do_classifier_free_guidance = guidance_scale > 1.0
        # 3. Encode input prompt
        prompt_embeds = self._encode_prompt(
            prompt,
            num_images_per_prompt,
            do_classifier_free_guidance,
            negative_prompt,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds, )

        # 4. Prepare timesteps
        self.scheduler.set_timesteps(num_inference_steps)

        timesteps = self.scheduler.timesteps

        # 5. Prepare latent variables
        num_channels_latents = self.unet.config.in_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt,
            num_channels_latents,
            num_frames,
            height,
            width,
            prompt_embeds.dtype,
            generator,
            latents, )

        # 6. Prepare extra step kwargs. TODO: Logic should ideally just be moved out of the pipeline
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        # 7. Denoising loop
        num_warmup_steps = len(
            timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                # expand the latents if we are doing classifier free guidance
                latent_model_input = paddle.concat(
                    [latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(
                    latent_model_input, t)
                # predict the noise residual
                noise_pred = self.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=prompt_embeds,
                    cross_attention_kwargs=cross_attention_kwargs, ).sample
                # perform guidance
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(
                        chunks=2)
                    noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond)
                # reshape latents
                bsz, channel, frames, width, height = latents.shape
                latents = latents.transpose([0, 2, 1, 3, 4]).reshape(
                    (bsz * frames, channel, width, height))
                noise_pred = noise_pred.transpose([0, 2, 1, 3, 4]).reshape(
                    (bsz * frames, channel, width, height))
                # compute the previous noisy sample x_t -> x_t-1
                latents = self.scheduler.step(noise_pred, t, latents,
                                              **extra_step_kwargs).prev_sample
                latents = latents[None, :].reshape(
                    (bsz, frames, channel, width, height)).transpose(
                        [0, 2, 1, 3, 4])
                # call the callback, if provided
                if i == len(timesteps) - 1 or (
                    (i + 1) > num_warmup_steps and
                    (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)
        video_tensor = self.decode_latents(latents)
        if output_type == "pd":
            video = video_tensor
        else:
            video = tensor2vid(video_tensor)
        if not return_dict:
            return (video, )
        return TextToVideoSDPipelineOutput(frames=video)
