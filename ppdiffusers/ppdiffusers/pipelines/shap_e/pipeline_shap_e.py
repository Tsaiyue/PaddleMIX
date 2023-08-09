import paddle
import math
from dataclasses import dataclass
from typing import List, Optional, Union
import numpy as np
import PIL
from ...models import PriorTransformer
from ...schedulers import HeunDiscreteScheduler
from ...utils import BaseOutput, logging, randn_tensor, replace_example_docstring
from ..pipeline_utils import DiffusionPipeline
from .renderer import ShapERenderer
import paddlenlp

logger = logging.get_logger(__name__)
EXAMPLE_DOC_STRING = """
    Examples:
        ```py
        >>> import torch
        >>> from diffusers import DiffusionPipeline
        >>> from diffusers.utils import export_to_gif

        >>> device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        >>> repo = "openai/shap-e"
        >>> pipe = DiffusionPipeline.from_pretrained(repo, torch_dtype=torch.float16)
        >>> pipe = pipe.to(device)

        >>> guidance_scale = 15.0
        >>> prompt = "a shark"

        >>> images = pipe(
        ...     prompt,
        ...     guidance_scale=guidance_scale,
        ...     num_inference_steps=64,
        ...     frame_size=256,
        ... ).images

        >>> gif_path = export_to_gif(images[0], "shark_3d.gif")
        ```
"""


@dataclass
class ShapEPipelineOutput(BaseOutput):
    """
    Output class for [`ShapEPipeline`] and [`ShapEImg2ImgPipeline`].

    Args:
        images (`torch.FloatTensor`)
            A list of images for 3D rendering.
    """
    images: Union[List[List[PIL.Image.Image]], List[List[np.ndarray]]]


class ShapEPipeline(DiffusionPipeline):
    """
    Pipeline for generating latent representation of a 3D asset and rendering with NeRF method with Shap-E.

    This model inherits from [`DiffusionPipeline`]. Check the superclass documentation for the generic methods
    implemented for all pipelines (downloading, saving, running on a particular device, etc.).

    Args:
        prior ([`PriorTransformer`]):
            The canonincal unCLIP prior to approximate the image embedding from the text embedding.
        text_encoder ([`CLIPTextModelWithProjection`]):
            Frozen text-encoder.
        tokenizer (`CLIPTokenizer`):
             A [`~transformers.CLIPTokenizer`] to tokenize text.
        scheduler ([`HeunDiscreteScheduler`]):
            A scheduler to be used in combination with `prior` to generate image embedding.
        shap_e_renderer ([`ShapERenderer`]):
            Shap-E renderer projects the generated latents into parameters of a MLP that's used to create 3D objects
            with the NeRF rendering method.
    """

    def __init__(
            self,
            prior: PriorTransformer,
            text_encoder: paddlenlp.transformers.CLIPTextModelWithProjection,
            tokenizer: paddlenlp.transformers.CLIPTokenizer,
            scheduler: HeunDiscreteScheduler,
            shap_e_renderer: ShapERenderer):
        super().__init__()
        self.register_modules(
            prior=prior,
            text_encoder=text_encoder,
            tokenizer=tokenizer,
            scheduler=scheduler,
            shap_e_renderer=shap_e_renderer)

    def prepare_latents(self, shape, dtype, device, generator, latents,
                        scheduler):
        if latents is None:
            latents = randn_tensor(
                shape, generator=generator, device=device, dtype=dtype)
        else:
            if latents.shape != shape:
                raise ValueError(
                    f'Unexpected latents shape, got {latents.shape}, expected {shape}'
                )
            latents = latents.to(device)
        latents = latents * scheduler.init_noise_sigma
        return latents

    def _encode_prompt(self, prompt, device, num_images_per_prompt,
                       do_classifier_free_guidance):
        len(prompt) if isinstance(prompt, list) else 1
        self.tokenizer.pad_token_id = 0
        text_inputs = self.tokenizer(
            prompt,
            padding='max_length',
            max_length=self.tokenizer.model_max_length,
            truncation=True,
            return_tensors='pt')
        text_input_ids = text_inputs.input_ids
        untruncated_ids = self.tokenizer(
            prompt, padding='longest', return_tensors='pt').input_ids
        if untruncated_ids.shape[-1] >= text_input_ids.shape[
                -1] and not paddle.equal_all(
                    x=text_input_ids, y=untruncated_ids).item():
            removed_text = self.tokenizer.batch_decode(
                untruncated_ids[:, self.tokenizer.model_max_length - 1:-1])
            logger.warning(
                f'The following part of your input was truncated because CLIP can only handle sequences up to {self.tokenizer.model_max_length} tokens: {removed_text}'
            )
        text_encoder_output = self.text_encoder(text_input_ids.to(device))
        prompt_embeds = text_encoder_output.text_embeds
        prompt_embeds = prompt_embeds.repeat_interleave(
            repeats=num_images_per_prompt, axis=0)
        prompt_embeds = prompt_embeds / paddle.linalg.norm(
            x=prompt_embeds, axis=-1, keepdim=True)
        if do_classifier_free_guidance:
            negative_prompt_embeds = paddle.zeros_like(x=prompt_embeds)
            prompt_embeds = paddle.concat(
                x=[negative_prompt_embeds, prompt_embeds])
        prompt_embeds = math.sqrt(prompt_embeds.shape[1]) * prompt_embeds
        return prompt_embeds

    @paddle.no_grad()
    @replace_example_docstring(EXAMPLE_DOC_STRING)
    def __call__(self,
                 prompt: str,
                 num_images_per_prompt: int=1,
                 num_inference_steps: int=25,
                 generator: Optional[Union[torch.Generator, List[
                     torch.Generator]]]=None,
                 latents: Optional[paddle.Tensor]=None,
                 guidance_scale: float=4.0,
                 frame_size: int=64,
                 output_type: Optional[str]='pil',
                 return_dict: bool=True):
        """
        The call function to the pipeline for generation.

        Args:
            prompt (`str` or `List[str]`):
                The prompt or prompts to guide the image generation.
            num_images_per_prompt (`int`, *optional*, defaults to 1):
                The number of images to generate per prompt.
            num_inference_steps (`int`, *optional*, defaults to 25):
                The number of denoising steps. More denoising steps usually lead to a higher quality image at the
                expense of slower inference.
            generator (`torch.Generator` or `List[torch.Generator]`, *optional*):
                A [`torch.Generator`](https://pytorch.org/docs/stable/generated/torch.Generator.html) to make
                generation deterministic.
            latents (`torch.FloatTensor`, *optional*):
                Pre-generated noisy latents sampled from a Gaussian distribution, to be used as inputs for image
                generation. Can be used to tweak the same generation with different prompts. If not provided, a latents
                tensor is generated by sampling using the supplied random `generator`.
            guidance_scale (`float`, *optional*, defaults to 4.0):
                A higher guidance scale value encourages the model to generate images closely linked to the text
                `prompt` at the expense of lower image quality. Guidance scale is enabled when `guidance_scale > 1`.
                usually at the expense of lower image quality.
            frame_size (`int`, *optional*, default to 64):
                The width and height of each image frame of the generated 3D output.
            output_type (`str`, *optional*, defaults to `"pt"`):
                The output format of the generate image. Choose between: `"pil"` (`PIL.Image.Image`), `"np"`
                (`np.array`),`"latent"` (`torch.Tensor`), mesh ([`MeshDecoderOutput`]).
            return_dict (`bool`, *optional*, defaults to `True`):
                Whether or not to return a [`~pipelines.shap_e.pipeline_shap_e.ShapEPipelineOutput`] instead of a plain
                tuple.

        Examples:

        Returns:
            [`~pipelines.shap_e.pipeline_shap_e.ShapEPipelineOutput`] or `tuple`:
                If `return_dict` is `True`, [`~pipelines.shap_e.pipeline_shap_e.ShapEPipelineOutput`] is returned,
                otherwise a `tuple` is returned where the first element is a list with the generated images.
        """
        if isinstance(prompt, str):
            batch_size = 1
        elif isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            raise ValueError(
                f'`prompt` has to be of type `str` or `list` but is {type(prompt)}'
            )
        device = self._execution_device
        batch_size = batch_size * num_images_per_prompt
        do_classifier_free_guidance = guidance_scale > 1.0
        prompt_embeds = self._encode_prompt(
            prompt, device, num_images_per_prompt, do_classifier_free_guidance)
        self.scheduler.set_timesteps(num_inference_steps, device=device)
        timesteps = self.scheduler.timesteps
        num_embeddings = self.prior.config.num_embeddings
        embedding_dim = self.prior.config.embedding_dim
        latents = self.prepare_latents(
            (batch_size, num_embeddings * embedding_dim), prompt_embeds.dtype,
            device, generator, latents, self.scheduler)
        latents = latents.reshape(latents.shape[0], num_embeddings,
                                  embedding_dim)
        for i, t in enumerate(self.progress_bar(timesteps)):
            latent_model_input = paddle.concat(
                x=[latents] * 2) if do_classifier_free_guidance else latents
            scaled_model_input = self.scheduler.scale_model_input(
                latent_model_input, t)
            noise_pred = self.prior(
                scaled_model_input, timestep=t,
                proj_embedding=prompt_embeds).predicted_image_embedding
            noise_pred, _ = noise_pred.split(scaled_model_input.shape[2], dim=2)
            if do_classifier_free_guidance:
                noise_pred_uncond, noise_pred = noise_pred.chunk(chunks=2)
                noise_pred = noise_pred_uncond + guidance_scale * (
                    noise_pred - noise_pred_uncond)
            latents = self.scheduler.step(
                noise_pred, timestep=t, sample=latents).prev_sample
        if output_type not in ['np', 'pil', 'latent', 'mesh']:
            raise ValueError(
                f'Only the output types `pil`, `np`, `latent` and `mesh` are supported not output_type={output_type}'
            )
        if output_type == 'latent':
            return ShapEPipelineOutput(images=latents)
        images = []
        if output_type == 'mesh':
            for i, latent in enumerate(latents):
                mesh = self.shap_e_renderer.decode_to_mesh(latent[(None), :],
                                                           device)
                images.append(mesh)
        else:
            for i, latent in enumerate(latents):
                image = self.shap_e_renderer.decode_to_image(
                    latent[(None), :], device, size=frame_size)
                images.append(image)
            images = paddle.stack(x=images)
            images = images.cpu().numpy()
            if output_type == 'pil':
                images = [self.numpy_to_pil(image) for image in images]
        if hasattr(
                self,
                'final_offload_hook') and self.final_offload_hook is not None:
            self.final_offload_hook.offload()
        if not return_dict:
            return images,
        return ShapEPipelineOutput(images=images)