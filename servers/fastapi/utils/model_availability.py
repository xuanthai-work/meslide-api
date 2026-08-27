from constants.llm import OPENAI_URL
from enums.image_provider import ImageProvider
from enums.llm_provider import LLMProvider
from utils.available_models import (
    list_available_google_models,
    list_available_openai_compatible_models,
    normalize_openai_compatible_base_url,
)
from utils.get_env import (
    get_can_change_keys_env,
    get_google_model_env,
    get_openai_api_key_env,
    get_openai_model_env,
)
from utils.get_env import get_custom_llm_api_key_env
from utils.get_env import get_custom_llm_url_env
from utils.get_env import get_custom_model_env
from utils.get_env import get_google_api_key_env
from utils.llm_provider import (
    get_llm_provider,
    is_custom_llm_selected,
)
from utils.image_provider import (
    get_selected_image_provider,
    is_image_generation_disabled,
)


def _check_image_provider_configuration() -> None:
    selected_image_provider = get_selected_image_provider()
    if not selected_image_provider:
        raise Exception("IMAGE_PROVIDER must be provided")

    if (
        selected_image_provider == ImageProvider.GEMINI_FLASH
        or selected_image_provider == ImageProvider.NANOBANANA_PRO
    ):
        google_api_key = get_google_api_key_env()
        if not google_api_key:
            raise Exception("GOOGLE_API_KEY must be provided")

    elif selected_image_provider == ImageProvider.GPT_IMAGE_1_5:
        openai_api_key = get_openai_api_key_env()
        if not openai_api_key:
            raise Exception("OPENAI_API_KEY must be provided")


async def check_llm_and_image_provider_api_or_model_availability():
    can_change_keys = get_can_change_keys_env() != "false"
    skip_image_validation = is_image_generation_disabled()
    if not can_change_keys:
        if get_llm_provider() == LLMProvider.OPENAI:
            openai_api_key = get_openai_api_key_env()
            if not openai_api_key:
                raise Exception("OPENAI_API_KEY must be provided")
            openai_model = get_openai_model_env()
            if openai_model:
                available_models = await list_available_openai_compatible_models(
                    OPENAI_URL, openai_api_key
                )
                if openai_model not in available_models:
                    print("-" * 50)
                    print("Available models: ", available_models)
                    raise Exception(f"Model {openai_model} is not available")

        elif get_llm_provider() == LLMProvider.GOOGLE:
            google_api_key = get_google_api_key_env()
            if not google_api_key:
                raise Exception("GOOGLE_API_KEY must be provided")
            google_model = get_google_model_env()
            if google_model:
                available_models = await list_available_google_models(google_api_key)
                if google_model not in available_models:
                    print("-" * 50)
                    print("Available models: ", available_models)
                    raise Exception(f"Model {google_model} is not available")

        elif is_custom_llm_selected():
            custom_model = get_custom_model_env()
            custom_llm_url = get_custom_llm_url_env()
            if not custom_model:
                raise Exception("CUSTOM_MODEL must be provided")
            if not custom_llm_url:
                raise Exception("CUSTOM_LLM_URL must be provided")
            available_models = await list_available_openai_compatible_models(
                custom_llm_url, get_custom_llm_api_key_env() or "null"
            )
            print("-" * 50)
            print("Available models: ", available_models)
            if custom_model not in available_models:
                raise Exception(f"Model {custom_model} is not available")

        if not skip_image_validation:
            _check_image_provider_configuration()