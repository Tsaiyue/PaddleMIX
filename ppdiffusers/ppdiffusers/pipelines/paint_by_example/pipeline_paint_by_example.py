import paddle
import inspect
import warnings
from typing import Callable, List, Optional, Union
import numpy as np
import PIL
from ...image_processor import VaeImageProcessor
from ...models import AutoencoderKL, UNet2DConditionModel
from ...schedulers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler
from ...utils import logging, randn_tensor
from ..pipeline_utils import DiffusionPipeline
from ..stable_diffusion import StableDiffusionPipelineOutput
from ..stable_diffusion.safety_checker import StableDiffusionSafetyChecker
from .image_encoder import PaintByExampleImageEncoder
from paddlenlp.transformers import CLIPImageProcessor
logger = logging.get_logger(__name__)


def prepare_mask_and_masked_image(image, mask):
    """
    Prepares a pair (image, mask) to be consumed by the Paint by Example pipeline. This means that those inputs will be
    converted to ``paddle.Tensor`` with shapes ``batch x channels x height x width`` where ``channels`` is ``3`` for the
    ``image`` and ``1`` for the ``mask``.

    The ``image`` will be converted to ``paddle.float32`` and normalized to be in ``[-1, 1]``. The ``mask`` will be
    binarized (``mask > 0.5``) and cast to ``paddle.float32`` too.

    Args:
        image (Union[np.array, PIL.Image, paddle.Tensor]): The image to inpaint.
            It can be a ``PIL.Image``, or a ``height x width x 3`` ``np.array`` or a ``channels x height x width``
            ``paddle.Tensor`` or a ``batch x channels x height x width`` ``paddle.Tensor``.
        mask (_type_): The mask to apply to the image, i.e. regions to inpaint.
            It can be a ``PIL.Image``, or a ``height x width`` ``np.array`` or a ``1 x height x width``
            ``paddle.Tensor`` or a ``batch x 1 x height x width`` ``paddle.Tensor``.


    Raises:
        ValueError: ``paddle.Tensor`` images should be in the ``[-1, 1]`` range. ValueError: ``paddle.Tensor`` mask
        should be in the ``[0, 1]`` range. ValueError: ``mask`` and ``image`` should have the same spatial dimensions.
        TypeError: ``mask`` is a ``paddle.Tensor`` but ``image`` is not
            (ot the other way around).

    Returns:
        tuple[paddle.Tensor]: The pair (mask, masked_image) as ``paddle.Tensor`` with 4
            dimensions: ``batch x channels x height x width``.
    """
    if isinstance(image, paddle.Tensor):
        if not isinstance(mask, paddle.Tensor):
            raise TypeError(
                f'`image` is a paddle.Tensor but `mask` (type: {type(mask)} is not'
            )
        if image.ndim == 3:
            assert image.shape[
                0] == 3, 'Image outside a batch should be of shape (3, H, W)'
            image = image.unsqueeze(axis=0)
        if mask.ndim == 2:
            mask = mask.unsqueeze(axis=0).unsqueeze(axis=0)
        if mask.ndim == 3:
            if mask.shape[0] == image.shape[0]:
                mask = mask.unsqueeze(axis=1)
            else:
                mask = mask.unsqueeze(axis=0)
        assert image.ndim == 4 and mask.ndim == 4, 'Image and Mask must have 4 dimensions'
        assert image.shape[-2:] == mask.shape[
            -2:], 'Image and Mask must have the same spatial dimensions'
        assert image.shape[0] == mask.shape[
            0], 'Image and Mask must have the same batch size'
        assert mask.shape[1] == 1, 'Mask image must have a single channel'
        if image.min() < -1 or image.max() > 1:
            raise ValueError('Image should be in [-1, 1] range')
        if mask.min() < 0 or mask.max() > 1:
            raise ValueError('Mask should be in [0, 1] range')
        mask = 1 - mask
        mask[mask < 0.5] = 0
        mask[mask >= 0.5] = 1
        image = image.cast(dtype='float32')
    elif isinstance(mask, paddle.Tensor):
        raise TypeError(
            f'`mask` is a paddle.Tensor but `image` (type: {type(image)} is not')
    else:
        if isinstance(image, PIL.Image.Image):
            image = [image]
        image = np.concatenate(
            [np.array(i.convert('RGB'))[None, :] for i in image], axis=0)
        image = image.transpose(0, 3, 1, 2)
        image = paddle.to_tensor(data=image).cast(dtype='float32') / 127.5 - 1.0
        if isinstance(mask, PIL.Image.Image):
            mask = [mask]
        mask = np.concatenate(
            [np.array(m.convert('L'))[None, None, :] for m in mask], axis=0)
        mask = mask.astype(np.float32) / 255.0
        mask = 1 - mask
        mask[mask < 0.5] = 0
        mask[mask >= 0.5] = 1
        mask = paddle.to_tensor(data=mask)
    masked_image = image * mask
    return mask, masked_image


class PaintByExamplePipeline(DiffusionPipeline):
    """
    <Tip warning={true}>

    🧪 This is an experimental feature!

    </Tip>

    Pipeline for image-guided image inpainting using Stable Diffusion.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    Args:
        vae ([`AutoencoderKL`]):
            Variational Auto-Encoder (VAE) model to encode and decode images to and from latent representations.
        image_encoder ([`PaintByExampleImageEncoder`]):
            Encodes the example input image. The `unet` is conditioned on the example image instead of a text prompt.
        tokenizer ([`~transformers.CLIPTokenizer`]):
            A `CLIPTokenizer` to tokenize text.
        unet ([`UNet2DConditionModel`]):
            A `UNet2DConditionModel` to denoise the encoded image latents.
        scheduler ([`SchedulerMixin`]):
            A scheduler to be used in combination with `unet` to denoise the encoded image latents. Can be one of
            [`DDIMScheduler`], [`LMSDiscreteScheduler`], or [`PNDMScheduler`].
        safety_checker ([`StableDiffusionSafetyChecker`]):
            Classification module that estimates whether generated images could be considered offensive or harmful.
            Please refer to the [model card](https://huggingface.co/runwayml/stable-diffusion-v1-5) for more details
            about a model's potential harms.
        feature_extractor ([`~transformers.CLIPImageProcessor`]):
            A `CLIPImageProcessor` to extract features from generated images; used as inputs to the `safety_checker`.

    """
    _optional_components = ['safety_checker']

    def __init__(self,
                 vae: AutoencoderKL,
                 image_encoder: PaintByExampleImageEncoder,
                 unet: UNet2DConditionModel,
                 scheduler: Union[DDIMScheduler, PNDMScheduler,
                                  LMSDiscreteScheduler],
                 safety_checker: StableDiffusionSafetyChecker,
                 feature_extractor: CLIPImageProcessor,
                 requires_safety_checker: bool=False):
        super().__init__()
        self.register_modules(
            vae=vae,
            image_encoder=image_encoder,
            unet=unet,
            scheduler=scheduler,
            safety_checker=safety_checker,
            feature_extractor=feature_extractor)
        self.vae_scale_factor = 2**(len(self.vae.config.block_out_channels) - 1)
        self.image_processor = VaeImageProcessor(
            vae_scale_factor=self.vae_scale_factor)
        self.register_to_config(requires_safety_checker=requires_safety_checker)

    def run_safety_checker(self, image, dtype):
        if self.safety_checker is None:
            has_nsfw_concept = None
        else:
            if paddle.is_tensor(x=image):
                feature_extractor_input = self.image_processor.postprocess(
                    image, output_type='pil')
            else:
                feature_extractor_input = self.image_processor.numpy_to_pil(
                    image)
            safety_checker_input = self.feature_extractor(
                feature_extractor_input, return_tensors='pd')
            image, has_nsfw_concept = self.safety_checker(
                images=image,
                clip_input=safety_checker_input.pixel_values.cast(dtype))
        return image, has_nsfw_concept

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

    def check_inputs(self, image, height, width, callback_steps):
        if not isinstance(image, paddle.Tensor) and not isinstance(
                image, PIL.Image.Image) and not isinstance(image, list):
            raise ValueError(
                f'`image` has to be of type `paddle.Tensor` or `PIL.Image.Image` or `List[PIL.Image.Image]` but is {type(image)}'
            )
        if height % 8 != 0 or width % 8 != 0:
            raise ValueError(
                f'`height` and `width` have to be divisible by 8 but are {height} and {width}.'
            )
        if callback_steps is None or callback_steps is not None and (
                not isinstance(callback_steps, int) or callback_steps <= 0):
            raise ValueError(
                f'`callback_steps` has to be a positive integer but is {callback_steps} of type {type(callback_steps)}.'
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

    def prepare_mask_latents(self, mask, masked_image, batch_size, height,
                             width, dtype, generator,
                             do_classifier_free_guidance):
        mask = paddle.nn.functional.interpolate(
            x=mask,
            size=(height // self.vae_scale_factor,
                  width // self.vae_scale_factor))
        mask = mask.cast(dtype=dtype)
        masked_image = masked_image.cast(dtype=dtype)
        masked_image_latents = self._encode_vae_image(
            masked_image, generator=generator)
        if mask.shape[0] < batch_size:
            if not batch_size % mask.shape[0] == 0:
                raise ValueError(
                    f"The passed mask and the required batch size don't match. Masks are supposed to be duplicated to a total batch size of {batch_size}, but {mask.shape[0]} masks were passed. Make sure the number of masks that you pass is divisible by the total requested batch size."
                )
            mask = mask.tile(
                repeat_times=[batch_size // mask.shape[0], 1, 1, 1])
        if masked_image_latents.shape[0] < batch_size:
            if not batch_size % masked_image_latents.shape[0] == 0:
                raise ValueError(
                    f"The passed images and the required batch size don't match. Images are supposed to be duplicated to a total batch size of {batch_size}, but {masked_image_latents.shape[0]} images were passed. Make sure the number of images that you pass is divisible by the total requested batch size."
                )
            masked_image_latents = masked_image_latents.tile(repeat_times=[
                batch_size // masked_image_latents.shape[0], 1, 1, 1
            ])
        mask = paddle.concat(x=[mask] *
                             2) if do_classifier_free_guidance else mask
        masked_image_latents = paddle.concat(
            x=[masked_image_latents] *
            2) if do_classifier_free_guidance else masked_image_latents
        masked_image_latents = masked_image_latents.cast(dtype=dtype)
        return mask, masked_image_latents

    def _encode_vae_image(self,
                          image: paddle.Tensor,
                          generator: paddle.Generator):
        if isinstance(generator, list):
            image_latents = [
                self.vae.encode(image[i:i + 1]).latent_dist.sample(
                    generator=generator[i]) for i in range(image.shape[0])
            ]
            image_latents = paddle.concat(x=image_latents, axis=0)
        else:
            image_latents = self.vae.encode(image).latent_dist.sample(
                generator=generator)
        image_latents = self.vae.config.scaling_factor * image_latents
        return image_latents

    def _encode_image(self, image, num_images_per_prompt,
                      do_classifier_free_guidance):
        dtype = next(self.image_encoder.parameters()).dtype
        if not isinstance(image, paddle.Tensor):
            image = self.feature_extractor(
                images=image, return_tensors='pd').pixel_values
        image = image.cast(dtype=dtype)
        image_embeddings, negative_prompt_embeds = self.image_encoder(
            image, return_uncond_vector=True)
        bs_embed, seq_len, _ = image_embeddings.shape
        image_embeddings = image_embeddings.tile(
            repeat_times=[1, num_images_per_prompt, 1])
        image_embeddings = image_embeddings.reshape(
            [bs_embed * num_images_per_prompt, seq_len, -1])
        if do_classifier_free_guidance:
            negative_prompt_embeds = negative_prompt_embeds.tile(
                repeat_times=[1, image_embeddings.shape[0], 1])
            negative_prompt_embeds = negative_prompt_embeds.reshape(
                [bs_embed * num_images_per_prompt, 1, -1])
            image_embeddings = paddle.concat(
                x=[negative_prompt_embeds, image_embeddings])
        return image_embeddings

    @paddle.no_grad()
    def __call__(
            self,
            example_image: Union[paddle.Tensor, PIL.Image.Image],
            image: Union[paddle.Tensor, PIL.Image.Image],
            mask_image: Union[paddle.Tensor, PIL.Image.Image],
            height: Optional[int]=None,
            width: Optional[int]=None,
            num_inference_steps: int=50,
            guidance_scale: float=5.0,
            negative_prompt: Optional[Union[str, List[str]]]=None,
            num_images_per_prompt: Optional[int]=1,
            eta: float=0.0,
            generator: Optional[Union[paddle.Generator, List[
                paddle.Generator]]]=None,
            latents: Optional[paddle.Tensor]=None,
            output_type: Optional[str]='pil',
            return_dict: bool=True,
            callback: Optional[Callable[[int, int, paddle.Tensor], None]]=None,
            callback_steps: int=1):
        """
        The call function to the pipeline for generation.

        Args:
            example_image (`paddle.Tensor` or `PIL.Image.Image` or `List[PIL.Image.Image]`):
                An example image to guide image generation.
            image (`paddle.Tensor` or `PIL.Image.Image` or `List[PIL.Image.Image]`):
                `Image` or tensor representing an image batch to be inpainted (parts of the image are masked out with
                `mask_image` and repainted according to `prompt`).
            mask_image (`paddle.Tensor` or `PIL.Image.Image` or `List[PIL.Image.Image]`):
                `Image` or tensor representing an image batch to mask `image`. White pixels in the mask are repainted,
                while black pixels are preserved. If `mask_image` is a PIL image, it is converted to a single channel
                (luminance) before use. If it's a tensor, it should contain one color channel (L) instead of 3, so the
                expected shape would be `(B, H, W, 1)`.
            height (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to `self.unet.config.sample_size * self.vae_scale_factor`):
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
            generator (`paddle.Generator` or `List[paddle.Generator]`, *optional*):
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

        Example:

        ```py
        >>> import PIL
        >>> import requests
        >>> import paddle
        >>> from io import BytesIO
        >>> from ppdiffusers import PaintByExamplePipeline


        >>> def download_image(url):
        ...     response = requests.get(url)
        ...     return PIL.Image.open(BytesIO(response.content)).convert("RGB")


        >>> img_url = (
        ...     "https://raw.githubusercontent.com/Fantasy-Studio/Paint-by-Example/main/examples/image/example_1.png"
        ... )
        >>> mask_url = (
        ...     "https://raw.githubusercontent.com/Fantasy-Studio/Paint-by-Example/main/examples/mask/example_1.png"
        ... )
        >>> example_url = "https://raw.githubusercontent.com/Fantasy-Studio/Paint-by-Example/main/examples/reference/example_1.jpg"

        >>> init_image = download_image(img_url).resize((512, 512))
        >>> mask_image = download_image(mask_url).resize((512, 512))
        >>> example_image = download_image(example_url).resize((512, 512))

        >>> pipe = PaintByExamplePipeline.from_pretrained(
        ...     "Fantasy-Studio/Paint-by-Example",
        ...     paddle_dtype=paddle.float16,
        ... )

        >>> image = pipe(image=init_image, mask_image=mask_image, example_image=example_image).images[0]
        >>> image
        ```

        Returns:
            [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.stable_diffusion.StableDiffusionPipelineOutput`] is returned,
                otherwise a `tuple` is returned where the first element is a list with the generated images and the
                second element is a list of `bool`s indicating whether the corresponding generated image contains
                "not-safe-for-work" (nsfw) content.
        """
        if isinstance(image, PIL.Image.Image):
            batch_size = 1
        elif isinstance(image, list):
            batch_size = len(image)
        else:
            batch_size = image.shape[0]
        do_classifier_free_guidance = guidance_scale > 1.0
        mask, masked_image = prepare_mask_and_masked_image(image, mask_image)
        height, width = masked_image.shape[-2:]
        self.check_inputs(example_image, height, width, callback_steps)
        image_embeddings = self._encode_image(
            example_image, num_images_per_prompt, do_classifier_free_guidance)
        self.scheduler.set_timesteps(num_inference_steps)
        timesteps = self.scheduler.timesteps
        num_channels_latents = self.vae.config.latent_channels
        latents = self.prepare_latents(
            batch_size * num_images_per_prompt, num_channels_latents, height,
            width, image_embeddings.dtype, generator, latents)
        mask, masked_image_latents = self.prepare_mask_latents(
            mask, masked_image, batch_size * num_images_per_prompt, height,
            width, image_embeddings.dtype, generator,
            do_classifier_free_guidance)
        num_channels_mask = mask.shape[1]
        num_channels_masked_image = masked_image_latents.shape[1]
        if (num_channels_latents + num_channels_mask + num_channels_masked_image
                != self.unet.config.in_channels):
            raise ValueError(
                f'Incorrect configuration settings! The config of `pipeline.unet`: {self.unet.config} expects {self.unet.config.in_channels} but received `num_channels_latents`: {num_channels_latents} + `num_channels_mask`: {num_channels_mask} + `num_channels_masked_image`: {num_channels_masked_image} = {num_channels_latents + num_channels_masked_image + num_channels_mask}. Please verify the config of `pipeline.unet` or your `mask_image` or `image` input.'
            )
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)
        num_warmup_steps = len(
            timesteps) - num_inference_steps * self.scheduler.order
        with self.progress_bar(total=num_inference_steps) as progress_bar:
            for i, t in enumerate(timesteps):
                latent_model_input = paddle.concat(
                    x=[latents] * 2) if do_classifier_free_guidance else latents
                latent_model_input = self.scheduler.scale_model_input(
                    latent_model_input, t)
                latent_model_input = paddle.concat(
                    x=[latent_model_input, masked_image_latents, mask], axis=1)
                noise_pred = self.unet(
                    latent_model_input,
                    t,
                    encoder_hidden_states=image_embeddings).sample
                if do_classifier_free_guidance:
                    noise_pred_uncond, noise_pred_text = noise_pred.chunk(
                        chunks=2)
                    noise_pred = noise_pred_uncond + guidance_scale * (
                        noise_pred_text - noise_pred_uncond)
                latents = self.scheduler.step(noise_pred, t, latents,
                                              **extra_step_kwargs).prev_sample
                if i == len(timesteps) - 1 or i + 1 > num_warmup_steps and (
                        i + 1) % self.scheduler.order == 0:
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        callback(i, t, latents)
        if not output_type == 'latent':
            image = self.vae.decode(
                latents / self.vae.config.scaling_factor, return_dict=False)[0]
            image, has_nsfw_concept = self.run_safety_checker(
                image, image_embeddings.dtype)
        else:
            image = latents
            has_nsfw_concept = None
        if has_nsfw_concept is None:
            do_denormalize = [True] * image.shape[0]
        else:
            do_denormalize = [(not has_nsfw) for has_nsfw in has_nsfw_concept]
        image = self.image_processor.postprocess(
            image, output_type=output_type, do_denormalize=do_denormalize)
        if not return_dict:
            return image, has_nsfw_concept
        return StableDiffusionPipelineOutput(
            images=image, nsfw_content_detected=has_nsfw_concept)
