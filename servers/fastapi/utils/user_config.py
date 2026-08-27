from models.user_config import UserConfig
from utils.get_env import (
    get_custom_llm_api_key_env,
    get_custom_llm_url_env,
    get_custom_model_env,
    get_disable_image_generation_env,
    get_google_api_key_env,
    get_google_model_env,
    get_gpt_image_1_5_quality_env,
    get_llm_provider_env,
    get_openai_api_key_env,
    get_openai_model_env,
    get_user_config_path_env,
    get_image_provider_env,
    get_extended_reasoning_env,
    get_web_grounding_env,
    get_web_search_provider_env,
    get_web_search_max_results_env,
    get_searxng_base_url_env,
    get_disable_thinking_env,
    get_openai_compat_image_base_url_env,
    get_openai_compat_image_api_key_env,
    get_openai_compat_image_model_env,
)
from utils.parsers import parse_bool_or_none
from utils.user_config_store import read_user_config_file
from utils.set_env import (
    set_custom_llm_api_key_env,
    set_custom_llm_url_env,
    set_custom_model_env,
    set_disable_image_generation_env,
    set_google_api_key_env,
    set_google_model_env,
    set_gpt_image_1_5_quality_env,
    set_llm_provider_env,
    set_openai_api_key_env,
    set_openai_model_env,
    set_image_provider_env,
    set_extended_reasoning_env,
    set_web_grounding_env,
    set_web_search_provider_env,
    set_web_search_max_results_env,
    set_searxng_base_url_env,
    set_disable_thinking_env,
    set_openai_compat_image_base_url_env,
    set_openai_compat_image_api_key_env,
    set_openai_compat_image_model_env,
)


def get_user_config():
    user_config_path = get_user_config_path_env()

    existing_config = UserConfig()
    existing_config_data = {}
    try:
        if user_config_path:
            existing_config_data = read_user_config_file(user_config_path)
            existing_config = UserConfig(**existing_config_data)
    except Exception:
        print("Error while loading user config")
        pass

    return UserConfig(
        LLM=existing_config.LLM or get_llm_provider_env(),
        OPENAI_API_KEY=existing_config.OPENAI_API_KEY or get_openai_api_key_env(),
        OPENAI_MODEL=existing_config.OPENAI_MODEL or get_openai_model_env(),
        GOOGLE_API_KEY=existing_config.GOOGLE_API_KEY or get_google_api_key_env(),
        GOOGLE_MODEL=existing_config.GOOGLE_MODEL or get_google_model_env(),
        CUSTOM_LLM_URL=existing_config.CUSTOM_LLM_URL or get_custom_llm_url_env(),
        CUSTOM_LLM_API_KEY=existing_config.CUSTOM_LLM_API_KEY
        or get_custom_llm_api_key_env(),
        CUSTOM_MODEL=existing_config.CUSTOM_MODEL or get_custom_model_env(),
        IMAGE_PROVIDER=existing_config.IMAGE_PROVIDER or get_image_provider_env(),
        DISABLE_IMAGE_GENERATION=(
            existing_config.DISABLE_IMAGE_GENERATION
            if existing_config.DISABLE_IMAGE_GENERATION is not None
            else (parse_bool_or_none(get_disable_image_generation_env()) or False)
        ),
        GPT_IMAGE_1_5_QUALITY=existing_config.GPT_IMAGE_1_5_QUALITY
        or get_gpt_image_1_5_quality_env(),
        DISABLE_THINKING=(
            existing_config.DISABLE_THINKING
            if existing_config.DISABLE_THINKING is not None
            else (parse_bool_or_none(get_disable_thinking_env()) or False)
        ),
        EXTENDED_REASONING=(
            existing_config.EXTENDED_REASONING
            if existing_config.EXTENDED_REASONING is not None
            else (parse_bool_or_none(get_extended_reasoning_env()) or False)
        ),
        WEB_GROUNDING=(
            existing_config.WEB_GROUNDING
            if existing_config.WEB_GROUNDING is not None
            else (parse_bool_or_none(get_web_grounding_env()) or False)
        ),
        WEB_SEARCH_PROVIDER=existing_config.WEB_SEARCH_PROVIDER
        or get_web_search_provider_env(),
        WEB_SEARCH_MAX_RESULTS=existing_config.WEB_SEARCH_MAX_RESULTS
        or get_web_search_max_results_env(),
        SEARXNG_BASE_URL=existing_config.SEARXNG_BASE_URL
        or get_searxng_base_url_env(),
        OPENAI_COMPAT_IMAGE_BASE_URL=existing_config.OPENAI_COMPAT_IMAGE_BASE_URL
        or get_openai_compat_image_base_url_env(),
        OPENAI_COMPAT_IMAGE_API_KEY=existing_config.OPENAI_COMPAT_IMAGE_API_KEY
        or get_openai_compat_image_api_key_env(),
        OPENAI_COMPAT_IMAGE_MODEL=existing_config.OPENAI_COMPAT_IMAGE_MODEL
        or get_openai_compat_image_model_env(),
    )


def update_env_with_user_config():
    user_config = get_user_config()
    if user_config.LLM:
        set_llm_provider_env(user_config.LLM)
    if user_config.OPENAI_API_KEY:
        set_openai_api_key_env(user_config.OPENAI_API_KEY)
    if user_config.OPENAI_MODEL:
        set_openai_model_env(user_config.OPENAI_MODEL)
    if user_config.GOOGLE_API_KEY:
        set_google_api_key_env(user_config.GOOGLE_API_KEY)
    if user_config.GOOGLE_MODEL:
        set_google_model_env(user_config.GOOGLE_MODEL)
    if user_config.CUSTOM_LLM_URL:
        set_custom_llm_url_env(user_config.CUSTOM_LLM_URL)
    if user_config.CUSTOM_LLM_API_KEY:
        set_custom_llm_api_key_env(user_config.CUSTOM_LLM_API_KEY)
    if user_config.CUSTOM_MODEL:
        set_custom_model_env(user_config.CUSTOM_MODEL)
    if user_config.DISABLE_IMAGE_GENERATION is not None:
        set_disable_image_generation_env(str(user_config.DISABLE_IMAGE_GENERATION))
    if user_config.IMAGE_PROVIDER:
        set_image_provider_env(user_config.IMAGE_PROVIDER)
    if user_config.GPT_IMAGE_1_5_QUALITY:
        set_gpt_image_1_5_quality_env(user_config.GPT_IMAGE_1_5_QUALITY)
    if user_config.DISABLE_THINKING is not None:
        set_disable_thinking_env(str(user_config.DISABLE_THINKING))
    if user_config.EXTENDED_REASONING is not None:
        set_extended_reasoning_env(str(user_config.EXTENDED_REASONING))
    if user_config.WEB_GROUNDING is not None:
        set_web_grounding_env(str(user_config.WEB_GROUNDING))
    if user_config.WEB_SEARCH_PROVIDER:
        set_web_search_provider_env(user_config.WEB_SEARCH_PROVIDER)
    if user_config.WEB_SEARCH_MAX_RESULTS:
        set_web_search_max_results_env(user_config.WEB_SEARCH_MAX_RESULTS)
    if user_config.SEARXNG_BASE_URL:
        set_searxng_base_url_env(user_config.SEARXNG_BASE_URL)
    if user_config.OPENAI_COMPAT_IMAGE_BASE_URL:
        set_openai_compat_image_base_url_env(user_config.OPENAI_COMPAT_IMAGE_BASE_URL)
    if user_config.OPENAI_COMPAT_IMAGE_API_KEY:
        set_openai_compat_image_api_key_env(user_config.OPENAI_COMPAT_IMAGE_API_KEY)
    if user_config.OPENAI_COMPAT_IMAGE_MODEL:
        set_openai_compat_image_model_env(user_config.OPENAI_COMPAT_IMAGE_MODEL)