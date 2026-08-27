from typing import Optional

from fastapi import HTTPException

from utils.get_env import (
    get_disable_thinking_env,
    get_web_grounding_env,
)
from utils.llm_provider import get_llm_provider
from utils.parsers import parse_bool_or_none


def enable_web_grounding() -> bool:
    return parse_bool_or_none(get_web_grounding_env()) or False


def disable_thinking() -> bool:
    return parse_bool_or_none(get_disable_thinking_env()) or False


def get_extra_body(*, uses_tool_choice: bool = False) -> Optional[dict]:
    """Return OpenAI-style ``extra_body`` payload for ``chat.completions.create``.

    CUSTOM (OpenAI-compatible) provider with ``DISABLE_THINKING`` is the only
    path that needs an extra body — Ollama and similar servers accept a
    ``{"enable_thinking": False}`` flag.
    """
    llm_provider = get_llm_provider()
    if llm_provider.value == "custom" and disable_thinking():
        return {"enable_thinking": False}
    return None


__all__ = [
    "disable_thinking",
    "enable_web_grounding",
    "get_extra_body",
]
