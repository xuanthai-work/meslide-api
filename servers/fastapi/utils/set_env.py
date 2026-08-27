import os


def set_temp_directory_env(value):
    os.environ["TEMP_DIRECTORY"] = value


def set_user_config_path_env(value):
    os.environ["USER_CONFIG_PATH"] = value


def set_llm_provider_env(value):
    os.environ["LLM"] = value


def set_custom_llm_url_env(value):
    os.environ["CUSTOM_LLM_URL"] = value


def set_openai_api_key_env(value):
    os.environ["OPENAI_API_KEY"] = value


def set_openai_model_env(value):
    os.environ["OPENAI_MODEL"] = value


def set_google_api_key_env(value):
    os.environ["GOOGLE_API_KEY"] = value


def set_google_model_env(value):
    os.environ["GOOGLE_MODEL"] = value


def set_custom_llm_api_key_env(value):
    os.environ["CUSTOM_LLM_API_KEY"] = value


def set_custom_model_env(value):
    os.environ["CUSTOM_MODEL"] = value


def set_image_provider_env(value):
    os.environ["IMAGE_PROVIDER"] = value


def set_disable_image_generation_env(value):
    os.environ["DISABLE_IMAGE_GENERATION"] = value


def set_disable_thinking_env(value):
    os.environ["DISABLE_THINKING"] = value


def set_extended_reasoning_env(value):
    os.environ["EXTENDED_REASONING"] = value


def set_web_grounding_env(value):
    os.environ["WEB_GROUNDING"] = value


def set_web_search_provider_env(value):
    os.environ["WEB_SEARCH_PROVIDER"] = value


def set_web_search_max_results_env(value):
    os.environ["WEB_SEARCH_MAX_RESULTS"] = value


def set_searxng_base_url_env(value):
    os.environ["SEARXNG_BASE_URL"] = value


def set_gpt_image_1_5_quality_env(value):
    os.environ["GPT_IMAGE_1_5_QUALITY"] = value


# OpenAI-compatible Image Provider
def set_openai_compat_image_base_url_env(value: str):
    os.environ["OPENAI_COMPAT_IMAGE_BASE_URL"] = value


def set_openai_compat_image_api_key_env(value: str):
    os.environ["OPENAI_COMPAT_IMAGE_API_KEY"] = value


def set_openai_compat_image_model_env(value: str):
    os.environ["OPENAI_COMPAT_IMAGE_MODEL"] = value