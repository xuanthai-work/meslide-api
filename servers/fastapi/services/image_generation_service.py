import asyncio
import base64
import os
import uuid
from weakref import WeakKeyDictionary

import aiohttp
from fastapi import HTTPException
from google import genai
from google.genai import types
from openai import NOT_GIVEN, AsyncOpenAI
from models.image_prompt import ImagePrompt
from models.sql.image_asset import ImageAsset
from utils.get_env import (
    get_gpt_image_1_5_quality_env,
    get_openai_compat_image_base_url_env,
    get_openai_compat_image_api_key_env,
    get_openai_compat_image_model_env,
    is_parallel_image_generation_enabled,
)
from utils.image_provider import (
    is_gpt_image_1_5_selected,
    is_image_generation_disabled,
    is_gemini_flash_selected,
    is_nanobanana_pro_selected,
    is_openai_compatible_selected,
)
from utils.asset_directory_utils import absolute_fastapi_asset_url
from utils.image_generation_error import normalize_image_generation_error


_IMAGE_GENERATION_LOCKS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, asyncio.Lock
] = WeakKeyDictionary()


def _get_image_generation_lock() -> asyncio.Lock:
    """Share one image request lock across presentation, editor, and chat services."""
    loop = asyncio.get_running_loop()
    lock = _IMAGE_GENERATION_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _IMAGE_GENERATION_LOCKS[loop] = lock
    return lock


class ImageGenerationService:
    def __init__(self, output_directory: str):
        self.output_directory = output_directory
        self.is_image_generation_disabled = is_image_generation_disabled()
        self.image_gen_func = self.get_image_gen_func()

    def get_image_gen_func(self):
        if self.is_image_generation_disabled:
            return None

        if is_gemini_flash_selected():
            return self.generate_image_gemini_flash
        elif is_nanobanana_pro_selected():
            return self.generate_image_nanobanana_pro
        elif is_gpt_image_1_5_selected():
            return self.generate_image_openai_gpt_image_1_5
        elif is_openai_compatible_selected():
            return self.generate_image_openai_compatible
        return None

    async def generate_image(self, prompt: ImagePrompt) -> str | ImageAsset:
        """
        Generates an image based on the provided prompt.
        - If no image generation function is available, returns a placeholder image.
        - Output Directory is used for saving the generated image.
        """
        if self.is_image_generation_disabled:
            print("Image generation is disabled. Using placeholder image.")
            return absolute_fastapi_asset_url("/static/images/placeholder.jpg")

        if not self.image_gen_func:
            print("No image generation function found. Using placeholder image.")
            return absolute_fastapi_asset_url("/static/images/placeholder.jpg")

        image_prompt = prompt.get_image_prompt(with_theme=True)
        print(f"Request - Generating Image for {image_prompt}")

        try:
            if is_parallel_image_generation_enabled():
                image_path = await self._call_image_provider(image_prompt)
            else:
                async with _get_image_generation_lock():
                    image_path = await self._call_image_provider(image_prompt)
            if image_path:
                if image_path.startswith("http"):
                    return image_path
                elif os.path.exists(image_path):
                    return ImageAsset(
                        path=image_path,
                        is_uploaded=False,
                        extras={
                            "prompt": prompt.prompt,
                            "theme_prompt": prompt.theme_prompt,
                        },
                    )
                elif image_path.startswith("/app_data/") or image_path.startswith(
                    "/static/"
                ):
                    return absolute_fastapi_asset_url(image_path)
            raise Exception(f"Image not found at {image_path}")

        except Exception as e:
            print(f"Error generating image: {e}")
            normalized_error = normalize_image_generation_error(e)
            if normalized_error is e:
                raise
            raise normalized_error from e

    async def _call_image_provider(self, image_prompt: str) -> str:
        return await self.image_gen_func(image_prompt, self.output_directory)

    async def generate_image_openai(
        self, prompt: str, output_directory: str, model: str, quality: str
    ) -> str:
        client = AsyncOpenAI()
        result = await client.images.generate(
            model=model,
            prompt=prompt,
            n=1,
            quality=quality,
            response_format="b64_json" if model == "dall-e-3" else NOT_GIVEN,
            size="1024x1024",
        )
        image_path = os.path.join(output_directory, f"{uuid.uuid4()}.png")
        with open(image_path, "wb") as f:
            f.write(base64.b64decode(result.data[0].b64_json))
        return image_path

    async def generate_image_openai_gpt_image_1_5(
        self, prompt: str, output_directory: str
    ) -> str:
        return await self.generate_image_openai(
            prompt,
            output_directory,
            "gpt-image-1.5",
            get_gpt_image_1_5_quality_env() or "medium",
        )

    async def _generate_image_google(
        self, prompt: str, output_directory: str, model: str
    ) -> str:
        """Base method for Google image generation models."""
        client = genai.Client()
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"],
            ),
        )

        # Latest SDK docs expose images in response.parts.
        response_parts = getattr(response, "parts", None)
        if not response_parts and getattr(response, "candidates", None):
            first_candidate = response.candidates[0] if response.candidates else None
            content = (
                getattr(first_candidate, "content", None) if first_candidate else None
            )
            response_parts = getattr(content, "parts", None) if content else None

        image_path = None
        for part in response_parts or []:
            if part.inline_data is not None:
                mime_type = getattr(part.inline_data, "mime_type", "") or ""
                ext = (
                    mime_type.split("/")[-1]
                    if mime_type.startswith("image/")
                    else "png"
                )
                image_path = os.path.join(output_directory, f"{uuid.uuid4()}.{ext}")
                if hasattr(part, "as_image"):
                    part.as_image().save(image_path)
                else:
                    # Backward-compatible fallback if helper method is unavailable.
                    image_data = getattr(part.inline_data, "data", None)
                    if image_data is None:
                        continue
                    image_bytes = (
                        base64.b64decode(image_data)
                        if isinstance(image_data, str)
                        else image_data
                    )
                    with open(image_path, "wb") as image_file:
                        image_file.write(image_bytes)

        if not image_path:
            raise HTTPException(
                status_code=500, detail=f"No image generated by google {model}"
            )

        return image_path

    async def generate_image_gemini_flash(
        self, prompt: str, output_directory: str
    ) -> str:
        """Generate image using Gemini Flash (gemini-2.5-flash-image)."""
        return await self._generate_image_google(
            prompt, output_directory, "gemini-2.5-flash-image"
        )

    async def generate_image_nanobanana_pro(
        self, prompt: str, output_directory: str
    ) -> str:
        """Generate image using NanoBanana Pro (gemini-3-pro-image-preview)."""
        return await self._generate_image_google(
            prompt, output_directory, "gemini-3-pro-image-preview"
        )

    async def generate_image_openai_compatible(
        self, prompt: str, output_directory: str
    ) -> str:
        base_url = get_openai_compat_image_base_url_env()
        api_key = get_openai_compat_image_api_key_env()
        model = get_openai_compat_image_model_env()

        if not base_url or not api_key or not model:
            raise ValueError(
                "OPENAI_COMPAT_IMAGE_BASE_URL, OPENAI_COMPAT_IMAGE_API_KEY and OPENAI_COMPAT_IMAGE_MODEL must be set."
            )

        from urllib.parse import urlparse

        parsed = urlparse(base_url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        client = AsyncOpenAI(base_url=base_url, api_key=api_key)

        response = await client.images.generate(
            model=model,
            prompt=prompt,
            n=1,
            size="1024x1024",
        )

        item = response.data[0]
        image_path = os.path.join(output_directory, f"{uuid.uuid4()}.png")

        if item.b64_json:
            with open(image_path, "wb") as f:
                f.write(base64.b64decode(item.b64_json))
        elif item.url:
            image_url = item.url
            is_relative_url = image_url.startswith("/")
            if is_relative_url:
                image_url = origin + image_url
            image_origin = urlparse(image_url)
            headers = {}
            if (
                is_relative_url
                or (
                    image_origin.scheme == parsed.scheme
                    and image_origin.netloc == parsed.netloc
                )
            ):
                headers["Authorization"] = f"Bearer {api_key}"
            async with aiohttp.ClientSession(trust_env=True) as session:
                dl_resp = await session.get(
                    image_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                )
                if dl_resp.status != 200:
                    raise Exception(
                        f"Failed to download image from OpenAI-compatible provider: {dl_resp.status}"
                    )
                with open(image_path, "wb") as f:
                    f.write(await dl_resp.read())
        else:
            raise Exception("OpenAI-compatible provider returned no image data")

        return image_path