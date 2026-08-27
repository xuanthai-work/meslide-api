"""
Local replacement for `llmai.shared` message/tool/response dataclasses.

After dropping the `llmai` dependency, call sites that still use
``SystemMessage(content=...)`` / ``UserMessage(content=...)`` syntax need a
local carrier. This module provides thin dataclasses that serialize to the
shape expected by native ``openai.OpenAI`` and ``google.genai`` clients, plus
helpers that convert a ``list[Message]`` into provider-ready payload.

Call sites use the dataclasses directly (no behavioral change); the
``to_openai`` / ``to_google`` methods and the module-level helpers below are
the boundary that turns them into native SDK kwargs.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Sequence

from enums.llm_provider import LLMProvider


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """Base marker class — concrete message types below."""

    role: str


@dataclass
class SystemMessage(Message):
    role: Literal["system"] = "system"
    content: str = ""


@dataclass
class UserMessage(Message):
    role: Literal["user"] = "user"
    content: Any = ""  # str | list[str | TextContentPart | ImageContentPart]


@dataclass
class AssistantMessage(Message):
    role: Literal["assistant"] = "assistant"
    content: list[str] | None = None
    tool_calls: list["AssistantToolCall"] = field(default_factory=list)
    id: str | None = None
    thinking: str | None = None


@dataclass
class ToolResponseMessage(Message):
    """Tool result message threaded back to the model.

    ``content`` accepts either ``list[str]`` (templates v2 preview-loop) or
    ``list[TextContentPart]`` (chat service tool-call loop). We accept both
    shapes so call sites stay untouched.
    """

    role: Literal["tool"] = "tool"
    id: str = ""
    content: list[Any] = field(default_factory=list)
    name: str | None = None

    def text_payload(self) -> str:
        """Flatten content into a single string for native SDKs."""
        parts: list[str] = []
        for part in self.content or []:
            if isinstance(part, str):
                parts.append(part)
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
        return "\n".join(parts)


@dataclass
class AssistantToolCall:
    """Tool call request from the assistant."""

    id: str
    name: str
    arguments: str  # JSON-serialized dict


# ---------------------------------------------------------------------------
# Content parts
# ---------------------------------------------------------------------------


@dataclass
class TextContentPart:
    text: str


@dataclass
class ImageContentPart:
    """Image content for multimodal messages.

    Either ``data`` (raw bytes) + ``mime_type`` or ``url`` is supplied.
    """

    data: bytes | None = None
    mime_type: str | None = None
    url: str | None = None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@dataclass
class Tool:
    """Function-tool definition.

    Accepts either a Pydantic model class (preferred) or a pre-built JSON
    schema dict (``input_schema``). The ``to_openai`` / ``to_google`` methods
    serialize both shapes.
    """

    name: str
    description: str
    schema: Any | None = None  # Pydantic model class
    input_schema: dict[str, Any] | None = None  # pre-built JSON schema
    strict: bool = False

    def resolved_schema(self) -> dict[str, Any]:
        if isinstance(self.input_schema, dict):
            return self.input_schema
        if self.schema is not None and hasattr(self.schema, "model_json_schema"):
            return self.schema.model_json_schema()
        return {"type": "object", "properties": {}}

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.resolved_schema(),
                "strict": self.strict,
            },
        }

    def to_google(self) -> dict[str, Any]:
        """Return a Google ``function_declarations`` entry as a plain dict.

        google-genai accepts either ``types.Tool`` objects or dicts; we use
        dicts to keep this module dependency-free for the SDK types.
        """
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._clean_schema_for_google(self.resolved_schema()),
        }

    @staticmethod
    def _clean_schema_for_google(schema: dict[str, Any]) -> dict[str, Any]:
        """Strip JSON-Schema keywords that google-genai rejects.

        Google rejects ``additionalProperties`` at the root and ``$schema``
        / ``$ref`` / ``$defs`` keys. We drop them defensively.
        """
        if not isinstance(schema, dict):
            return schema
        blocked_top_level = {"additionalProperties", "$schema"}
        cleaned = {
            key: value
            for key, value in schema.items()
            if key not in blocked_top_level
        }
        # Recursively strip $ref / $defs from nested objects.
        def visit(node: Any) -> Any:
            if isinstance(node, dict):
                return {
                    k: visit(v)
                    for k, v in node.items()
                    if k not in {"$ref", "$defs"}
                }
            if isinstance(node, list):
                return [visit(item) for item in node]
            return node

        return visit(cleaned)


@dataclass
class WebSearchTool:
    """Marker that triggers provider-native web search.

    Single production site: ``utils/llm_calls/generate_presentation_outlines.py``.
    The test at ``tests/unit/test_generate_presentation_outlines.py:151`` relies
    on ``isinstance(..., WebSearchTool)``, so we keep the class name.
    """

    def to_openai(self) -> dict[str, Any]:
        return {"type": "web_search"}

    def to_google(self) -> dict[str, Any]:
        return {"google_search": {}}


# ---------------------------------------------------------------------------
# Response format / reasoning
# ---------------------------------------------------------------------------


@dataclass
class JSONSchemaResponse:
    """Structured-output response-format descriptor."""

    name: str
    json_schema: dict[str, Any] | type | None = None
    strict: bool = False


def _json_schema_dict(schema: Any) -> dict[str, Any]:
    """Return a JSON-serializable schema dict from a dict or Pydantic model."""
    if isinstance(schema, dict):
        return schema
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        dumped = model_json_schema()
        if isinstance(dumped, dict):
            return dumped
    return {}


@dataclass
class ReasoningConfig:
    enabled: bool = False
    effort: str | None = None  # "low" | "medium" | "high"


class ReasoningEffortValue:
    LOW = "low"


def build_response_format(
    payload: Any,
    *,
    provider: LLMProvider,
) -> dict[str, Any] | None:
    """Translate a ``JSONSchemaResponse`` (or None) into provider kwargs."""
    if payload is None:
        return None
    if not isinstance(payload, JSONSchemaResponse):
        # Fallback: treat as a dict (legacy passthrough).
        return payload  # type: ignore[return-value]
    schema = _json_schema_dict(payload.json_schema)
    if provider == LLMProvider.OPENAI:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": payload.name,
                "schema": schema,
                "strict": payload.strict,
            },
        }
    if provider == LLMProvider.GOOGLE:
        return {
            "response_schema": schema,
            "response_mime_type": "application/json",
        }
    # CUSTOM provider uses OpenAI chat-completions API.
    return {
        "type": "json_schema",
        "json_schema": {
            "name": payload.name,
            "schema": schema,
            "strict": payload.strict,
        },
    }


def build_reasoning_kwargs(
    config: ReasoningConfig | None,
    *,
    provider: LLMProvider,
    use_responses_api: bool,
) -> dict[str, Any]:
    """Translate a ``ReasoningConfig`` into provider kwargs.

    OpenAI Chat Completions does not support reasoning; we only attach it
    when using the Responses API. Google supports it via ``thinking_config``.
    """
    if config is None or not config.enabled:
        return {}
    if provider == LLMProvider.OPENAI and use_responses_api:
        payload: dict[str, Any] = {}
        if config.effort:
            payload["effort"] = config.effort
        if payload:
            return {"reasoning": payload}
    if provider == LLMProvider.GOOGLE:
        budget = 2048
        return {
            "thinking_config": {
                "include_thoughts": True,
                "thinking_budget": budget,
            }
        }
    return {}


# ---------------------------------------------------------------------------
# Message list -> provider payload
# ---------------------------------------------------------------------------


def _content_to_openai(content: Any) -> Any:
    """Translate ``Message.content`` into an OpenAI content part list."""
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                parts.append({"type": "text", "text": part})
                continue
            if isinstance(part, ImageContentPart):
                if part.data is not None:
                    import base64

                    encoded = base64.b64encode(part.data).decode("ascii")
                    mime = part.mime_type or "image/png"
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{encoded}",
                            },
                        }
                    )
                elif part.url:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": part.url},
                        }
                    )
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append({"type": "text", "text": text})
        return parts if parts else ""
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return str(content)


def _content_to_google(content: Any) -> list[dict[str, Any]]:
    """Translate ``Message.content`` into Google ``parts``."""
    if content is None:
        return [{"text": ""}]
    if isinstance(content, str):
        return [{"text": content}]
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[dict[str, Any]] = []
        for part in content:
            if isinstance(part, str):
                parts.append({"text": part})
                continue
            if isinstance(part, ImageContentPart):
                if part.data is not None:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": part.mime_type or "image/png",
                                "data": _b64(part.data),
                            }
                        }
                    )
                elif part.url:
                    parts.append(
                        {
                            "file_data": {
                                "file_uri": part.url,
                            }
                        }
                    )
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append({"text": text})
        return parts or [{"text": ""}]
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return [{"text": text}]
    return [{"text": str(content)}]


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


def messages_to_openai(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """Translate local Message dataclasses to OpenAI Chat Completions payload."""
    payload: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            payload.append(
                {"role": "system", "content": _content_to_openai(message.content)}
            )
            continue
        if isinstance(message, UserMessage):
            payload.append(
                {"role": "user", "content": _content_to_openai(message.content)}
            )
            continue
        if isinstance(message, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant"}
            text = message.content[0] if message.content else None
            if text is not None:
                entry["content"] = text
            else:
                entry["content"] = None
            if message.tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for call in message.tool_calls
                ]
            payload.append(entry)
            continue
        if isinstance(message, ToolResponseMessage):
            payload.append(
                {
                    "role": "tool",
                    "tool_call_id": message.id,
                    "content": message.text_payload(),
                }
            )
            continue
        # Unknown role — pass through.
        payload.append({"role": message.role, "content": _content_to_openai(getattr(message, "content", ""))})
    return payload


def messages_to_google(messages: Iterable[Message]) -> list[dict[str, Any]]:
    """Translate local Message dataclasses to google-genai ``contents`` payload.

    System prompts are not valid Gemini content roles; peel them off with
    ``google_system_instruction`` and pass via ``config.system_instruction``.
    """
    payload: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            continue
        if isinstance(message, UserMessage):
            payload.append(
                {
                    "role": "user",
                    "parts": _content_to_google(message.content),
                }
            )
            continue
        if isinstance(message, AssistantMessage):
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"text": "\n".join(message.content)})
            for call in message.tool_calls:
                args: Any
                if call.arguments:
                    try:
                        args = json.loads(call.arguments)
                    except Exception:
                        args = call.arguments
                else:
                    args = {}
                parts.append(
                    {
                        "function_call": {
                            "name": call.name,
                            "args": args,
                        }
                    }
                )
            payload.append({"role": "model", "parts": parts or [{"text": ""}]})
            continue
        if isinstance(message, ToolResponseMessage):
            # Google wants one tool result per function-call part. If multiple
            # tool responses are batched, emit multiple parts with the same id.
            parts: list[dict[str, Any]] = []
            text = message.text_payload()
            name = message.name or ""
            if not message.content:
                parts.append(
                    {
                        "function_response": {
                            "name": name,
                            "response": {"result": text},
                        }
                    }
                )
            else:
                for index, _part in enumerate(message.content):
                    parts.append(
                        {
                            "function_response": {
                                "name": name,
                                "response": {"result": text},
                            }
                        }
                        if index == 0
                        else {"text": ""}
                    )
            payload.append({"role": "user", "parts": parts})
            continue
        payload.append(
            {
                "role": message.role,
                "parts": _content_to_google(getattr(message, "content", "")),
            }
        )
    return payload


def google_system_instruction(messages: Iterable[Message]) -> str | None:
    """Join SystemMessage contents for Gemini ``config.system_instruction``."""
    parts = [
        message.content
        for message in messages
        if isinstance(message, SystemMessage) and message.content
    ]
    return "\n\n".join(parts) or None


# ---------------------------------------------------------------------------
# Tool-call parsing (native response -> AssistantToolCall list)
# ---------------------------------------------------------------------------


def tool_calls_from_openai_response(response: Any) -> list[AssistantToolCall]:
    """Extract tool calls from a Chat Completions response or streamed final."""
    calls: list[AssistantToolCall] = []
    if response is None:
        return calls
    # Chat Completions: response.choices[0].message.tool_calls
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        for tool_call in getattr(message, "tool_calls", None) or []:
            function = getattr(tool_call, "function", None)
            name = getattr(function, "name", "") if function else ""
            arguments = getattr(function, "arguments", "") if function else ""
            calls.append(
                AssistantToolCall(
                    id=getattr(tool_call, "id", "") or "",
                    name=name or "",
                    arguments=arguments or "{}",
                )
            )
        return calls
    # OpenAI Responses API: response.output[*] items of type="function_call"
    output = getattr(response, "output", None)
    if output:
        for item in output:
            if getattr(item, "type", None) == "function_call":
                calls.append(
                    AssistantToolCall(
                        id=getattr(item, "id", "") or getattr(item, "call_id", "") or "",
                        name=getattr(item, "name", "") or "",
                        arguments=getattr(item, "arguments", "") or "{}",
                    )
                )
    return calls


def tool_calls_from_google_response(response: Any) -> list[AssistantToolCall]:
    """Extract tool calls from a google-genai response or streamed final."""
    calls: list[AssistantToolCall] = []
    if response is None:
        return calls
    candidates = getattr(response, "candidates", None) or []
    for index, candidate in enumerate(candidates):
        content = getattr(candidate, "content", None)
        parts = getattr(content, "parts", None) if content else None
        if not parts:
            continue
        for part_index, part in enumerate(parts):
            function_call = getattr(part, "function_call", None)
            if function_call is None:
                continue
            name = getattr(function_call, "name", "") or ""
            args = getattr(function_call, "args", None)
            if isinstance(args, dict):
                arguments = json.dumps(args, ensure_ascii=False)
            elif isinstance(args, str):
                arguments = args
            else:
                arguments = "{}"
            synthetic_id = (
                getattr(function_call, "id", None)
                or f"google-{index}-{part_index}-{name}"
            )
            calls.append(
                AssistantToolCall(
                    id=synthetic_id,
                    name=name,
                    arguments=arguments,
                )
            )
    return calls


__all__ = [
    "AssistantMessage",
    "AssistantToolCall",
    "ImageContentPart",
    "JSONSchemaResponse",
    "Message",
    "ReasoningConfig",
    "ReasoningEffortValue",
    "SystemMessage",
    "TextContentPart",
    "Tool",
    "ToolResponseMessage",
    "UserMessage",
    "WebSearchTool",
    "build_reasoning_kwargs",
    "build_response_format",
    "messages_to_google",
    "google_system_instruction",
    "messages_to_openai",
    "tool_calls_from_google_response",
    "tool_calls_from_openai_response",
]
