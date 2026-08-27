from typing import Any

import httpx
from fastapi import HTTPException
from google import genai
from openai import OpenAI

from constants.llm import (
    DEFAULT_GOOGLE_MODEL,
    DEFAULT_OPENAI_MODEL,
    OPENAI_URL,
)
from enums.llm_provider import LLMProvider
from utils.get_env import (
    get_custom_llm_api_key_env,
    get_custom_llm_url_env,
    get_custom_model_env,
    get_google_api_key_env,
    get_google_model_env,
    get_llm_provider_env,
    get_openai_api_key_env,
    get_openai_model_env,
)

# 180s total request timeout, 10s connect. Applied to OpenAI and OpenAI-compatible
# (CUSTOM) clients so a stalled LLM stream does not hang the request indefinitely.
LLM_HTTP_TIMEOUT = httpx.Timeout(180.0, connect=10.0)


def get_llm_provider():
    try:
        return LLMProvider(get_llm_provider_env())
    except Exception:
        raise HTTPException(
            status_code=500,
            detail=(
                "Invalid LLM provider. Please select one of: "
                "openai, google, custom"
            ),
        )


def is_openai_selected():
    return get_llm_provider() == LLMProvider.OPENAI


def is_google_selected():
    return get_llm_provider() == LLMProvider.GOOGLE


def is_custom_llm_selected():
    return get_llm_provider() == LLMProvider.CUSTOM


def get_model():
    selected_llm = get_llm_provider()
    if selected_llm == LLMProvider.OPENAI:
        return get_openai_model_env() or DEFAULT_OPENAI_MODEL
    elif selected_llm == LLMProvider.GOOGLE:
        return get_google_model_env() or DEFAULT_GOOGLE_MODEL
    elif selected_llm == LLMProvider.CUSTOM:
        return get_custom_model_env()
    else:
        raise HTTPException(
            status_code=500,
            detail=(
                "Invalid LLM provider. Please select one of: "
                "openai, google, custom"
            ),
        )


def get_google_llm_client() -> genai.Client:
    """Google GenAI client for tests and direct API use (uses GOOGLE_API_KEY from env)."""
    if not get_google_api_key_env():
        raise HTTPException(status_code=400, detail="Google API Key is not set")
    return genai.Client()


def get_llm_client() -> OpenAI | genai.Client:
    """Return a native SDK client for the currently configured LLM provider.

    Replaces the removed ``llmai.get_client(config=...)`` factory.
    """
    provider = get_llm_provider()
    if provider == LLMProvider.OPENAI:
        api_key = get_openai_api_key_env()
        if not api_key:
            raise HTTPException(status_code=400, detail="OpenAI API Key is not set")
        return OpenAI(api_key=api_key, base_url=OPENAI_URL, timeout=LLM_HTTP_TIMEOUT)
    if provider == LLMProvider.GOOGLE:
        return get_google_llm_client()
    if provider == LLMProvider.CUSTOM:
        base_url = get_custom_llm_url_env()
        if not base_url:
            raise HTTPException(
                status_code=400,
                detail="Custom LLM URL is not set",
            )
        return OpenAI(
            api_key=get_custom_llm_api_key_env() or "null",
            base_url=base_url,
            timeout=LLM_HTTP_TIMEOUT,
        )
    raise HTTPException(
        status_code=500,
        detail=(
            "Invalid LLM provider. Please select one of: "
            "openai, google, custom"
        ),
    )


def get_large_model() -> str:
    """Resolved model name for the configured LLM provider (same as runtime `get_model`)."""
    return get_model()


def _supports_thinking(model: str) -> bool:
    """Return True when the model supports provider-native reasoning.

    Replaces ``llmai.supports_thinking(model, provider=...)``. Test patches
    this symbol at ``utils.llm_provider._supports_thinking``.
    """
    if not model:
        return False
    normalized = model.lower().lstrip("/")
    prefixes = (
        "gpt-5",
        "o1",
        "o3",
        "o4",
        "gemini-2.5",
        "gemini-3",
    )
    return any(normalized.startswith(prefix) for prefix in prefixes)


def use_responses_api(tools: Any = None, *, reasoning: Any = None) -> bool:
    """Return True when this OpenAI call should use the Responses API.

    Native web search and OpenAI reasoning both require Responses. All other
    OpenAI traffic uses Chat Completions.
    """
    if get_llm_provider() != LLMProvider.OPENAI:
        return False
    if reasoning:
        return True
    for tool in tools or ():
        if isinstance(tool, dict) and tool.get("type") == "web_search":
            return True
        to_openai = getattr(tool, "to_openai", None)
        if callable(to_openai):
            payload = to_openai()
            if isinstance(payload, dict) and payload.get("type") == "web_search":
                return True
    return False
