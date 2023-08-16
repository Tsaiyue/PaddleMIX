import paddle
import inspect
import warnings
from typing import Callable, List, Optional, Union
from ...image_processor import VaeImageProcessor
from ...models import AutoencoderKL, Transformer2DModel, UNet2DConditionModel
from ...schedulers import KarrasDiffusionSchedulers
from ...utils import logging, randn_tensor
from ..pipeline_utils import DiffusionPipeline, ImagePipelineOutput
from .modeling_text_unet import UNetFlatConditionModel
from paddlenlp.transformers import (
    CLIPImageProcessor,
    CLIPTextModelWithProjection,
    CLIPTokenizer, )

logger = logging.get_logger(__name__)


class VersatileDiffusionTextToImagePipeline(DiffusionPipeline):
    """
    Pipeline for text-to-image generation using Versatile Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    Parameters:
        vqvae ([`VQModel`]):
            Vector-quantized (VQ) model to encode and decode images to and from latent representations.
        bert ([`LDMBertModel`]):
            Text-encoder model based on [`~transformers.BERT`].
        tokenizer ([`~transformers.BertTokenizer`]):
            A `BertTokenizer` to tokenize text.
        unet ([`UNet2DConditionModel`]):
            A `UNet2DConditionModel` to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
    """
    tokenizer: CLIPTokenizer
    image_feature_extractor: CLIPImageProcessor
    text_encoder: CLIPTextModelWithProjection
    image_unet: UNet2DConditionModel
    text_unet: UNetFlatConditionModel
    vae: AutoencoderKL
    scheduler: KarrasDiffusionSchedulers
    _optional_components = ['text_unet']

    def __init__(self,
                 tokenizer: CLIPTokenizer,
                 text_encoder: CLIPTextModelWithProjection,
                 image_unet: UNet2DConditionModel,
                 text_unet: UNetFlatConditionModel,
                 vae: AutoencoderKL,
                 scheduler: KarrasDiffusionSchedulers):
        super().__init__()
        self.register_modules(
            tokenizer=tokenizer,
            text_encoder=text_encoder,
            image_unet=image_unet,
            text_unet=text_unet,
            vae=vae,
            scheduler=scheduler)
        self.vae_scale_factor = 2**(len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor)
        if self.text_unet is not None:
            self._swap_unet_attention_blocks()

    def _swap_unet_attention_blocks(self):
        """
        Swap the `Transformer2DModel` blocks between the image and text UNets
        """
        for name, module in self.image_unet.named_modules():
            if isinstance(module, Transformer2DModel):
                parent_name, index = name.rsplit('.', 1)
                index = int(index)
                self.image_unet.get_submodule(parent_name)[
                    index], self.text_unet.get_submodule(parent_name)[
                        index] = self.text_unet.get_submodule(parent_name)[
                            index], self.image_unet.get_submodule(parent_name)[
                                index]

    def remove_unused_weights(self):
        self.register_modules(text_unet=None)

    def _encode_prompt(self, prompt, num_images_per_prompt,
                       do_classifier_free_guidance, negative_prompt):
        """
        Encodes the prompt into text encoder hidden states.

        Args:
            prompt (`str` or `List[str]`):
                prompt to be encoded
            num_images_per_prompt (`int`):
                number of images that should be generated per prompt
            do_classifier_free_guidance (`bool`):
                whether to use classifier free guidance or not
            negative_prompt (`str` or `List[str]`):
                The prompt or prompts not to guide the image generation. Ignored when not using guidance (i.e., ignored
                if `guidance_scale` is less than `1`).
        """

        def normalize_embeddings(encoder_output):
            embeds = self.text_encoder.text_projection(
                encoder_output.last_hidden_state)
            embeds_pooled = encoder_output.text_embeds
            embeds = embeds / paddle.linalg.norm(
                x=embeds_pooled.unsqueeze(axis=1), axis=-1, keepdim=True)
            return embeds

        batch_size = len(prompt) if isinstance(prompt, list) else 1
        text_inputs = self.tokenizer(
            prompt,
            padding='max_length',
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors='pd')
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(
            prompt, padding='max_length', return_tensors='pd').input_ids
        if not paddle.equal_all(x=text_input_ids, y=untruncated_ids).item():
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
        prompt_embeds = normalize_embeddings(prompt_embeds)
        bs_embed, seq_len, _ = prompt_embeds.shape
        prompt_embeds = prompt_embeds.tile(
            repeat_times=[1, num_images_per_prompt, 1])
        prompt_embeds = prompt_embeds.reshape(
            [bs_embed * num_images_per_prompt, seq_len, -1])
        if do_classifier_free_guidance:
            uncond_tokens: List[str]
            if negative_prompt is None:
                uncond_tokens = [''] * batch_size
            elif type(prompt) is not type(negative_prompt):
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
            max_length = text_input_ids.shape[-1]
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
            negative_prompt_embeds = normalize_embeddings(
                negative_prompt_embeds)
            seq_len = negative_prompt_embeds.shape[1]
            negative_prompt_embeds = negative_prompt_embeds.tile(
                repeat_times=[1, num_images_per_prompt, 1])
            negative_prompt_embeds = negative_prompt_embeds.reshape(
                [batch_size * num_images_per_prompt, seq_len, -1])
            prompt_embeds = paddle.concat(
                x=[negative_prompt_embeds, prompt_embeds])
        return prompt_embeds

    def decode_latents(self, latents):
        warnings.warn(
            'The decode_latents method is deprecated and will be removed in a future version. Please use VaeImageProcessor instead',
            FutureWarning)
        latents = 1 / self.vae.config.scaling_factor * latents
        image = self.vae.decode(latents, return_dict=False)[0]
        image = (image / 2 + 0.5).clip(min=0, max=1)
        image = image.cpu().transpose(perm=[0, 2, 3, 1]).astype(
            dtype='float32').numpy()
        return image

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
                        height,
                        width,
                        dtype,
                        generator,
                        latents=None):
        shape = (batch_size, num_channels_latents, height //
                 self.vae_scale_factor, width // self.vae_scale_factor)
        if isinstance(generator, list) and len(generator) != batch_size:
            raise ValueError(
                f'You have passed a list of generators of length {len(generator)}, but requested an effective batch size of {batch_size}. Make sure the batch size matches the length of the generators.'
            )
        if latents is None:
            latents = randn_tensor(shape, generator=generator, dtype=dtype)
        else:
            latents = latents
        latents = latents * self.scheduler.init_noise_sigma
        return latents

    @paddle.no_grad()
    def __call__(
            self,
            prompt: Union[str, List[str]],
            height: Optional[int]=None,
            width: Optional[int]=None,
            num_inference_steps: int=50,
            guidance_scale: float=7.5,
            negative_prompt: Optional[Union[str, List[str]]]=None,
            num_images_per_prompt: Optional[int]=1,
            eta: float=0.0,
            generator: Optional[Union[paddle.Generator, List[
                paddle.Generator]]]=None,
            latents: Optional[paddle.Tensor]=None,
            output_type: Optional[str]='pil',
            return_dict: bool=True,
            callback: Optional[Callable[[int, int, paddle.Tensor], None]]=None,
            callback_steps: int=1,
            **kwargs):
        """
        The call function to the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide image generation.
            height (`int`, *optional*, defaults to `self.image_unet.config.sample_size * self.vae_scale_factor`):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to `self.image_unet.config.sample_size * self.vae_scale_factor`):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 50):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
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
            generator (`paddle.Generator`, *optional*):
                A [`paddle.Generator`](https://pytorch.org/docs/stable/generated/paddle.Generator.html) to make
                generation deterministic.
            latents (`paddle.Tensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generated image. Choose between `PIL.Image` or `np.array`.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] instead of a
                plain tuple.
            callback (`Callable`, *optional*):
                A function that calls every `callback_steps` steps during inference. The function is called with the
                following arguments: `callback(step: int, timestep: int, latents: paddle.Tensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function is called. If not specified, the callback is called at
                every step.

        Examples:

        ```py
        >>> from ppdiffusers import VersatileDiffusionTextToImagePipeline
        >>> import paddle

        >>> pipe = VersatileDiffusionTextToImagePipeline.from_pretrained(
        ...     "shi-labs/versatile-diffusion", paddle_dtype=paddle.float16
        ... )
        >>> pipe.remove_unused_weights()

        >>> generator = paddle.Generator().manual_seed(0)
        >>> image = pipe("an astronaut riding on a horse on mars", generator=generator).images[0]
        >>> image.save("./astronaut.png")
        ```

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] is returned,
                otherwise a `tuple` is returned where the first element is a list with the generated images.
        """
        height = (height or
                  self.image_unet.config.sample_size * self.vae_scale_factor)
        width = (width or
                 self.image_unet.config.sample_size * self.vae_scale_factor)
        self.check_inputs(prompt, height, width, callback_steps)
        batch_size = 1 if isinstance(prompt, str) else len(prompt)
        do_classifier_free_guidance = guidance_scale > 1.0
        prompt_embeds = self._encode_prompt(prompt, num_images_per_prompt,
                                            do_classifier_free_guidance,
                                            negative_prompt)
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps = self.scheduler.timesteps
        num_channels_latents = self.image_unet.config.in_channels
        latents = self.prepare_latents(batch_size * num_images_per_prompt,
                                       num_channels_latents, height, width,
                                       prompt_embeds.dtype, generator, latents)
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        for i, t in enumerate(self.progress_bar(timesteps)):
            latent_model_input = paddle.concat(
                x=[latents] * 2) if do_classifier_free_guidance else latents
            latent_model_input = self.scheduler.scale_model_input(
                latent_model_input, t)
            noise_pred = self.image_unet(
                latent_model_input, t,
                encoder_hidden_states=prompt_embeds).sample
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(chunks=2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                    noise_pred_text - noise_pred_uncond)
            latents = self.scheduler.step(noise_pred, t, latents,
                                          **extra_step_kwargs).prev_sample
            if callback is not None and i % callback_steps == 0:
                callback(i, t, latents)
        if not output_type == 'latent':
            image = self.vae.decode(
                latents / self.vae.config.scaling_factor, return_dict=False)[0]
        else:
            image = latents
        image = self.image_processor.postprocess(image, output_type=output_type)
        if not return_dict:
            return image,
        return ImagePipelineOutput(images=image)
