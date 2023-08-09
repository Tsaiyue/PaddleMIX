import paddle
from typing import Callable, List, Optional, Union
import numpy as np
import PIL
from PIL import Image
from ...models import UNet2DConditionModel, VQModel
from ...schedulers import DDPMScheduler
from ...utils import logging, randn_tensor, replace_example_docstring
from ..pipeline_utils import DiffusionPipeline, ImagePipelineOutput
logger = logging.get_logger(__name__)
EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> import torch
        >>> import numpy as np

        >>> from diffusers import KandinskyV22PriorEmb2EmbPipeline, KandinskyV22ControlnetImg2ImgPipeline
        >>> from transformers import pipeline
        >>> from diffusers.utils import load_image


        >>> def make_hint(image, depth_estimator):
        ...     image = depth_estimator(image)["depth"]
        ...     image = np.array(image)
        ...     image = image[:, :, None]
        ...     image = np.concatenate([image, image, image], axis=2)
        ...     detected_map = torch.from_numpy(image).float() / 255.0
        ...     hint = detected_map.permute(2, 0, 1)
        ...     return hint


        >>> depth_estimator = pipeline("depth-estimation")

        >>> pipe_prior = KandinskyV22PriorEmb2EmbPipeline.from_pretrained(
        ...     "kandinsky-community/kandinsky-2-2-prior", torch_dtype=torch.float16
        ... )
        >>> pipe_prior = pipe_prior.to("cuda")

        >>> pipe = KandinskyV22ControlnetImg2ImgPipeline.from_pretrained(
        ...     "kandinsky-community/kandinsky-2-2-controlnet-depth", torch_dtype=torch.float16
        ... )
        >>> pipe = pipe.to("cuda")

        >>> img = load_image(
        ...     "https://huggingface.co/datasets/hf-internal-testing/diffusers-images/resolve/main"
        ...     "/kandinsky/cat.png"
        ... ).resize((768, 768))


        >>> hint = make_hint(img, depth_estimator).unsqueeze(0).half().to("cuda")

        >>> prompt = "A robot, 4k photo"
        >>> negative_prior_prompt = "lowres, text, error, cropped, worst quality, low quality, jpeg artifacts, ugly, duplicate, morbid, mutilated, out of frame, extra fingers, mutated hands, poorly drawn hands, poorly drawn face, mutation, deformed, blurry, dehydrated, bad anatomy, bad proportions, extra limbs, cloned face, disfigured, gross proportions, malformed limbs, missing arms, missing legs, extra arms, extra legs, fused fingers, too many fingers, long neck, username, watermark, signature"

        >>> generator = torch.Generator(device="cuda").manual_seed(43)

        >>> img_emb = pipe_prior(prompt=prompt, image=img, strength=0.85, generator=generator)
        >>> negative_emb = pipe_prior(prompt=negative_prior_prompt, image=img, strength=1, generator=generator)

        >>> images = pipe(
        ...     image=img,
        ...     strength=0.5,
        ...     image_embeds=img_emb.image_embeds,
        ...     negative_image_embeds=negative_emb.image_embeds,
        ...     hint=hint,
        ...     num_inference_steps=50,
        ...     generator=generator,
        ...     height=768,
        ...     width=768,
        ... ).images

        >>> images[0].save("robot_cat.png")
        ```
"""


def downscale_height_and_width(height, width, scale_factor=8):
    new_height = height // scale_factor**2
    if height % scale_factor**2 != 0:
        new_height += 1
    new_width = width // scale_factor**2
    if width % scale_factor**2 != 0:
        new_width += 1
    return new_height * scale_factor, new_width * scale_factor


def prepare_image(pil_image, w=512, h=512):
    pil_image = pil_image.resize((w, h), resample=Image.BICUBIC, reducing_gap=1)
    arr = np.array(pil_image.convert('RGB'))
    arr = arr.astype(np.float32) / 127.5 - 1
    arr = np.transpose(arr, [2, 0, 1])
    image = paddle.to_tensor(data=arr).unsqueeze(axis=0)
    return image


class KandinskyV22ControlnetImg2ImgPipeline(DiffusionPipeline):
    """
    Pipeline for image-to-image generation using Kandinsky

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods the
    library implements for all the pipelines (such as downloading or saving, running on a particular device, etc.)

    Args:
        scheduler ([`DDIMScheduler`]):
            A scheduler to be used in combination with `unet` to generate image latents.
        unet ([`UNet2DConditionModel`]):
            Conditional U-Net architecture to denoise the image embedding.
        movq ([`VQModel`]):
            MoVQ Decoder to generate the image from the latents.
    """

    def __init__(self,
                 unet: UNet2DConditionModel,
                 scheduler: DDPMScheduler,
                 movq: VQModel):
        super().__init__()
        self.register_modules(unet=unet, scheduler=scheduler, movq=movq)
        self.movq_scale_factor = 2**(
            len(self.movq.config.block_out_channels) - 1)

    def get_timesteps(self, num_inference_steps, strength, device):
        init_timestep = min(
            int(num_inference_steps * strength), num_inference_steps)
        t_start = max(num_inference_steps - init_timestep, 0)
        timesteps = self.scheduler.timesteps[t_start:]
        return timesteps, num_inference_steps - t_start

    def prepare_latents(self,
                        image,
                        timestep,
                        batch_size,
                        num_images_per_prompt,
                        dtype,
                        device,
                        generator=None):
        if not isinstance(image, (paddle.Tensor, PIL.Image.Image, list)):
            raise ValueError(
                f'`image` has to be of type `torch.Tensor`, `PIL.Image.Image` or list but is {type(image)}'
            )
        image = image.to(device=device, dtype=dtype)
        batch_size = batch_size * num_images_per_prompt
        if image.shape[1] == 4:
            init_latents = image
        else:
            if isinstance(generator, list) and len(generator) != batch_size:
                raise ValueError(
                    f'You have passed a list of generators of length {len(generator)}, but requested an effective batch size of {batch_size}. Make sure the batch size matches the length of the generators.'
                )
            elif isinstance(generator, list):
                init_latents = [
                    self.movq.encode(image[i:i + 1]).latent_dist.sample(
                        generator[i]) for i in range(batch_size)
                ]
                init_latents = paddle.concat(x=init_latents, axis=0)
            else:
                init_latents = self.movq.encode(image).latent_dist.sample(
                    generator)
            init_latents = self.movq.config.scaling_factor * init_latents
        init_latents = paddle.concat(x=[init_latents], axis=0)
        shape = init_latents.shape
        noise = randn_tensor(
            shape, generator=generator, device=device, dtype=dtype)
        init_latents = self.scheduler.add_noise(init_latents, noise, timestep)
        latents = init_latents
        return latents

    @paddle.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(
            self,
            image_embeds: Union[paddle.Tensor, List[paddle.Tensor]],
            image: Union[paddle.Tensor, PIL.Image.Image, List[paddle.Tensor],
                         List[PIL.Image.Image]],
            negative_image_embeds: Union[paddle.Tensor, List[paddle.Tensor]],
            hint: paddle.Tensor,
            height: int=512,
            width: int=512,
            num_inference_steps: int=100,
            guidance_scale: float=4.0,
            strength: float=0.3,
            num_images_per_prompt: int=1,
            generator: Optional[Union[paddle.Generator, List[
                paddle.Generator]]]=None,
            output_type: Optional[str]='pil',
            callback: Optional[Callable[[int, int, paddle.Tensor], None]]=None,
            callback_steps: int=1,
            return_dict: bool=True):
        """
        Function invoked when calling the pipeline for generation.

        Args:
            image_embeds (`torch.FloatTensor` or `List[torch.FloatTensor]`):
                The clip image embeddings for text prompt, that will be used to condition the image generation.
            image (`torch.FloatTensor`, `PIL.Image.Image`, `np.ndarray`, `List[torch.FloatTensor]`, `List[PIL.Image.Image]`, or `List[np.ndarray]`):
                `Image`, or tensor representing an image batch, that will be used as the starting point for the
                process. Can also accpet image latents as `image`, if passing latents directly, it will not be encoded
                again.
            strength (`float`, *optional*, defaults to 0.8):
                Conceptually, indicates how much to transform the reference `image`. Must be between 0 and 1. `image`
                will be used as a starting point, adding more noise to it the larger the `strength`. The number of
                denoising steps depends on the amount of noise initially added. When `strength` is 1, added noise will
                be maximum and the denoising process will run for the full number of iterations specified in
                `num_inference_steps`. A value of 1, therefore, essentially ignores `image`.
            hint (`torch.FloatTensor`):
                The controlnet condition.
            negative_image_embeds (`torch.FloatTensor` or `List[torch.FloatTensor]`):
                The clip image embeddings for negative text prompt, will be used to condition the image generation.
            height (`int`, *optional*, defaults to 512):
                The height in pixels of the generated image.
            width (`int`, *optional*, defaults to 512):
                The width in pixels of the generated image.
            num_inference_steps (`int`, *optional*, defaults to 100):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            guidance_scale (`float`, *optional*, defaults to 4.0):
                Guidance scale as defined in [Classifier-Free Diffusion Guidance](https://arxiv.org/abs/2207.12598).
                `guidance_scale` is defined as `w` of equation 2. of [Imagen
                Paper](https://arxiv.org/pdf/2205.11487.pdf). Guidance scale is enabled by setting `guidance_scale >
                1`. Higher guidance scale encourages to generate images that are closely linked to the text `prompt`,
                usually at the expense of lower image quality.
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                One or a list of [torch generator(s)](https://pytorch.org/docs/stable/generated/torch.Generator.html)
                to make generation deterministic.
            output_type (`str`, *optional*, defaults to `"pil"`):
                The output format of the generate image. Choose between: `"pil"` (`PIL.Image.Image`), `"np"`
                (`np.array`) or `"pt"` (`torch.Tensor`).
            callback (`Callable`, *optional*):
                A function that calls every `callback_steps` steps during inference. The function is called with the
                following arguments: `callback(step: int, timestep: int, latents: torch.FloatTensor)`.
            callback_steps (`int`, *optional*, defaults to 1):
                The frequency at which the `callback` function is called. If not specified, the callback is called at
                every step.
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.ImagePipelineOutput`] instead of a plain tuple.

        Examples:

        Returns:
            [`~pipelines.ImagePipelineOutput`] or `tuple`
        """
        device = self._execution_device
        do_classifier_free_guidance = guidance_scale > 1.0
        if isinstance(image_embeds, list):
            image_embeds = paddle.concat(x=image_embeds, axis=0)
        if isinstance(negative_image_embeds, list):
            negative_image_embeds = paddle.concat(
                x=negative_image_embeds, axis=0)
        if isinstance(hint, list):
            hint = paddle.concat(x=hint, axis=0)
        batch_size = image_embeds.shape[0]
        if do_classifier_free_guidance:
            image_embeds = image_embeds.repeat_interleave(
                repeats=num_images_per_prompt, axis=0)
            negative_image_embeds = negative_image_embeds.repeat_interleave(
                repeats=num_images_per_prompt, axis=0)
            hint = hint.repeat_interleave(repeats=num_images_per_prompt, axis=0)
            image_embeds = paddle.concat(
                x=[negative_image_embeds, image_embeds],
                axis=0).to(dtype=self.unet.dtype, device=device)
            hint = paddle.concat(
                x=[hint, hint], axis=0).to(dtype=self.unet.dtype, device=device)
        if not isinstance(image, list):
            image = [image]
        if not all(
                isinstance(i, (PIL.Image.Image, paddle.Tensor)) for i in image):
            raise ValueError(
                f'Input is in incorrect format: {[type(i) for i in image]}. Currently, we only support  PIL image and pytorch tensor'
            )
        image = paddle.concat(
            x=[prepare_image(i, width, height) for i in image], axis=0)
        image = image.to(dtype=image_embeds.dtype, device=device)
        latents = self.movq.encode(image)['latents']
        latents = latents.repeat_interleave(
            repeats=num_images_per_prompt, axis=0)
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps, num_inference_steps = self.get_timesteps(num_inference_steps,
                                                            strength, device)
        latent_timestep = timesteps[:1].tile(
            repeat_times=[batch_size * num_images_per_prompt])
        height, width = downscale_height_and_width(height, width,
                                                   self.movq_scale_factor)
        latents = self.prepare_latents(latents, latent_timestep, batch_size,
                                       num_images_per_prompt,
                                       image_embeds.dtype, device, generator)
        for i, t in enumerate(self.progress_bar(timesteps)):
            latent_model_input = paddle.concat(
                x=[latents] * 2) if do_classifier_free_guidance else latents
            added_cond_kwargs = {'image_embeds': image_embeds, 'hint': hint}
            noise_pred = self.unet(
                sample=latent_model_input,
                timestep=t,
                encoder_hidden_states=None,
                added_cond_kwargs=added_cond_kwargs,
                return_dict=False)[0]
            if do_classifier_free_guidance:
                noise_pred, variance_pred = noise_pred.split(
                    latents.shape[1], dim=1)
                noise_pred_uncond, noise_pred_text = noise_pred.chunk(chunks=2)
                _, variance_pred_text = variance_pred.chunk(chunks=2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                    noise_pred_text - noise_pred_uncond)
                noise_pred = paddle.concat(
                    x=[noise_pred, variance_pred_text], axis=1)
            if not (hasattr(self.scheduler.config, 'variance_type') and
                    self.scheduler.config.variance_type in
                    ['learned', 'learned_range']):
                noise_pred, _ = noise_pred.split(latents.shape[1], dim=1)
            latents = self.scheduler.step(
                noise_pred, t, latents, generator=generator)[0]
            if callback is not None and i % callback_steps == 0:
                callback(i, t, latents)
        image = self.movq.decode(latents, force_not_quantize=True)['sample']
        if hasattr(
                self,
                'final_offload_hook') and self.final_offload_hook is not None:
            self.final_offload_hook.offload()
        if output_type not in ['pt', 'np', 'pil']:
            raise ValueError(
                f'Only the output types `pt`, `pil` and `np` are supported not output_type={output_type}'
            )
        if output_type in ['np', 'pil']:
            image = image * 0.5 + 0.5
            image = image.clip(min=0, max=1)
            image = image.cpu().transpose(perm=[0, 2, 3, 1]).astype(
                dtype='float32').numpy()
        if output_type == 'pil':
            image = self.numpy_to_pil(image)
        if not return_dict:
            return image,
        return ImagePipelineOutput(images=image)