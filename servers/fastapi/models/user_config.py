from typing import Optional
from pydantic import BaseModel


class UserConfig(BaseModel):
    LLM: Optional[str] = None

    # OpenAI
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_MODEL: Optional[str] = None

    # Google
    GOOGLE_API_KEY: Optional[str] = None
    GOOGLE_MODEL: Optional[str] = None

    # Custom LLM (OpenAI-compatible self-host)
    CUSTOM_LLM_URL: Optional[str] = None
    CUSTOM_LLM_API_KEY: Optional[str] = None
    CUSTOM_MODEL: Optional[str] = None

    # Image Provider
    DISABLE_IMAGE_GENERATION: Optional[bool] = None
    IMAGE_PROVIDER: Optional[str] = None

    # OpenAI Compatible Image Provider
    OPENAI_COMPAT_IMAGE_BASE_URL: Optional[str] = None
    OPENAI_COMPAT_IMAGE_API_KEY: Optional[str] = None
    OPENAI_COMPAT_IMAGE_MODEL: Optional[str] = None

    # Gpt Image 1.5 Quality
    GPT_IMAGE_1_5_QUALITY: Optional[str] = None

    # Reasoning
    DISABLE_THINKING: Optional[bool] = None
    EXTENDED_REASONING: Optional[bool] = None

    # Web Search
    WEB_GROUNDING: Optional[bool] = None
    WEB_SEARCH_PROVIDER: Optional[str] = None
    WEB_SEARCH_MAX_RESULTS: Optional[str] = None
    SEARXNG_BASE_URL: Optional[str] = None