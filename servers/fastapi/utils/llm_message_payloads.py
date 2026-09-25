"""Convert local message content and messages into provider payloads."""

from collections.abc import Sequence
from typing import Any

from fastapi import HTTPException

from enums.llm_provider import LLMProvider
from utils.llm_messages import Message, TextContentPart, messages_to_google, messages_to_openai


def normalize_content_parts(content: Any) -> list[Any]:
    """Flatten ``str | list[str | TextContentPart | ImageContentPart]`` to parts."""
    if content is None:
        return []
    if isinstance(content, str):
        return [TextContentPart(text=content)]
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        return list(content)
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return [TextContentPart(text=text)]
    return [content]


def _convert_messages_for_provider(
    messages: Sequence[Message], provider: LLMProvider
) -> list[dict[str, Any]]:
    """Translate local ``Message`` list into provider-native payload."""
    if provider in (LLMProvider.OPENAI, LLMProvider.CUSTOM):
        return messages_to_openai(messages)
    if provider == LLMProvider.GOOGLE:
        return messages_to_google(messages)
    raise HTTPException(
        status_code=500,
        detail=(
            "Invalid LLM provider. Please select one of: "
            "openai, google, custom"
        ),
    )
