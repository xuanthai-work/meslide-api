import asyncio
import json
import logging
import math
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Optional

import dirtyjson
from fastapi import HTTPException

from enums.llm_provider import LLMProvider
from utils.llm_config import get_extra_body
from utils.llm_messages import (
    AssistantMessage,
    AssistantToolCall,
    JSONSchemaResponse,
    Message,
    ReasoningConfig,
    SystemMessage,
    TextContentPart,
    Tool,
    ToolResponseMessage,
    UserMessage,
    WebSearchTool,
    build_reasoning_kwargs,
    build_response_format,
    messages_to_google,
    messages_to_openai,
    google_system_instruction as extract_google_system_instruction,
    tool_calls_from_google_response,
    tool_calls_from_openai_response,
)
from utils.llm_provider import get_llm_provider, use_responses_api
from utils.dict_utils import to_plain_data
from utils.schema_utils import get_schema_validation_errors

LOGGER = logging.getLogger(__name__)
CLIENT_DISCONNECT_POLL_SECONDS = 0.1
DisconnectChecker = Callable[[], Awaitable[bool]]
_STREAM_END = object()


async def _yield_stream_items(
    stream: Any,
    *,
    disconnect_checker: Optional[DisconnectChecker] = None,
) -> AsyncGenerator[Any, None]:
    """Yield items from either an async stream or a sync OpenAI ``Stream``.

    Polls ``disconnect_checker`` (when provided) at least every
    ``CLIENT_DISCONNECT_POLL_SECONDS`` while waiting for the next item so a
    client disconnect during a hung/stalled LLM stream raises
    ``asyncio.CancelledError`` promptly.
    """
    if hasattr(stream, "__aiter__"):
        iterator = stream.__aiter__()
        while True:
            await _raise_if_client_disconnected(disconnect_checker)
            next_item = asyncio.create_task(iterator.__anext__())
            try:
                while True:
                    await _raise_if_client_disconnected(disconnect_checker)
                    done, _ = await asyncio.wait(
                        {next_item}, timeout=CLIENT_DISCONNECT_POLL_SECONDS
                    )
                    if done:
                        break
                item = next_item.result()
            except StopAsyncIteration:
                break
            yield item
        return

    iterator = iter(stream)

    def _next_item() -> Any:
        return next(iterator, _STREAM_END)

    while True:
        await _raise_if_client_disconnected(disconnect_checker)
        next_item = asyncio.ensure_future(asyncio.to_thread(_next_item))
        while True:
            await _raise_if_client_disconnected(disconnect_checker)
            done, _ = await asyncio.wait(
                {next_item}, timeout=CLIENT_DISCONNECT_POLL_SECONDS
            )
            if done:
                break
        item = next_item.result()
        if item is _STREAM_END:
            break
        yield item
TextChunkCallback = Callable[[str], Awaitable[None]]


# ---------------------------------------------------------------------------
# Stream event dataclasses (preserved public shape)
# ---------------------------------------------------------------------------


@dataclass
class _StreamContentChunk:
    type: str = "content"
    chunk: str = ""


@dataclass
class _StreamThinkingChunk:
    type: str = "thinking"
    chunk: str = ""


@dataclass
class _StreamCompletionChunk:
    type: str = "completion"
    content: Any = None
    tool_calls: list[AssistantToolCall] = None  # type: ignore[assignment]
    usage: Any = None
    raw: Any = None

    def __post_init__(self) -> None:
        if self.tool_calls is None:
            self.tool_calls = []


@dataclass(frozen=True)
class TextGenerationMetrics:
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    tokens_per_second: float
    duration_seconds: float
    estimated: bool
    thinking_tokens: Optional[int] = None
    thinking_tokens_estimated: bool = False
    supports_thinking: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "tokens_per_second": round(self.tokens_per_second, 2),
            "duration_seconds": round(self.duration_seconds, 3),
            "estimated": self.estimated,
            "thinking_tokens": self.thinking_tokens,
            "thinking_tokens_estimated": self.thinking_tokens_estimated,
            "supports_thinking": self.supports_thinking,
        }


# ---------------------------------------------------------------------------
# Message / tool helpers — operate on local Message dataclasses
# ---------------------------------------------------------------------------


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


def messages_to_provider_payload(
    messages: Sequence[Message],
) -> list[dict[str, Any]]:
    """Translate local ``Message`` list into provider-native payload."""
    provider = get_llm_provider()
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


def _build_tools_kwarg(tools: Sequence[Any]) -> dict[str, Any]:
    """Translate the local ``tools`` list into provider kwargs.

    Returns a dict with one or both keys:
    - ``"tools"``: function-declaration tools (OpenAI / Google function_declarations).
    - ``"google_search_tool"``: Google native web-search tool, if present.
    """
    provider = get_llm_provider()
    function_tools: list[dict[str, Any]] = []
    google_search_payload: dict[str, Any] | None = None

    for tool in tools:
        if isinstance(tool, WebSearchTool):
            if provider == LLMProvider.GOOGLE:
                google_search_payload = tool.to_google()
            # OpenAI web-search goes into the same tools list.
            else:
                function_tools.append(tool.to_openai())
            continue
        if isinstance(tool, Tool):
            if provider == LLMProvider.GOOGLE:
                function_tools.append(tool.to_google())
            else:
                function_tools.append(tool.to_openai())
            continue
        # Unknown type — pass through (legacy/fallback).
        function_tools.append(tool)

    kwarg: dict[str, Any] = {}
    if function_tools:
        kwarg["tools"] = function_tools
    if google_search_payload is not None:
        kwarg["google_search_tool"] = google_search_payload
    return kwarg


def get_generate_kwargs(
    model: str,
    messages: Sequence[Message],
    max_tokens: Optional[int] = None,
    tools: Optional[Sequence[Any]] = None,
    response_format: Optional[JSONSchemaResponse] = None,
    reasoning: Optional[ReasoningConfig] = None,
    stream: bool = False,
) -> dict[str, Any]:
    """Build native SDK kwargs for the currently configured provider.

    Replaces the legacy ``llmai.get_client.generate(...)`` kwarg shape. Callers
    must unpack the dict into the provider's API method, e.g.
    ``client.chat.completions.create(**get_generate_kwargs(...))`` for OpenAI.
    """
    provider = get_llm_provider()
    reasoning_enabled = bool(reasoning is not None and reasoning.enabled)
    responses_api = use_responses_api(tools, reasoning=reasoning_enabled)

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages_to_provider_payload(messages),
        "stream": stream,
    }

    if max_tokens is not None:
        if provider == LLMProvider.GOOGLE:
            kwargs["max_output_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

    if tools:
        tool_kwargs = _build_tools_kwarg(tools)
        if "tools" in tool_kwargs:
            kwargs["tools"] = tool_kwargs["tools"]
        if "google_search_tool" in tool_kwargs:
            # Google's native web search is attached as a top-level config
            # option, not inside the function-declaration tools list.
            kwargs["google_search_tool"] = tool_kwargs["google_search_tool"]

    if response_format is not None:
        built = build_response_format(response_format, provider=provider)
        if provider == LLMProvider.GOOGLE and isinstance(built, dict):
            # Google's structured output goes into a generation_config-shaped
            # dict; the dispatcher in stream_generate_events unpacks it.
            kwargs["generation_config"] = built
        elif built is not None:
            kwargs["response_format"] = built

    if reasoning is not None:
        kwargs.update(
            build_reasoning_kwargs(
                reasoning,
                provider=provider,
                use_responses_api=responses_api,
            )
        )

    extra_body = get_extra_body(uses_tool_choice=bool(tools or response_format))
    if extra_body:
        kwargs["extra_body"] = extra_body

    if provider == LLMProvider.GOOGLE:
        instruction = extract_google_system_instruction(messages)
        if instruction:
            kwargs["system_instruction"] = instruction

    return kwargs


# ---------------------------------------------------------------------------
# Provider-specific chat.completion / generate_content dispatch
# ---------------------------------------------------------------------------


def _to_responses_text_format(response_format: Any) -> Any:
    """Unwrap Chat Completions ``response_format`` into Responses ``text.format``."""
    if not isinstance(response_format, dict):
        return response_format
    inner = response_format.get("json_schema")
    if response_format.get("type") == "json_schema" and isinstance(inner, dict):
        return {
            "type": "json_schema",
            "name": inner.get("name"),
            "schema": inner.get("schema"),
            "strict": inner.get("strict", False),
        }
    return response_format


def _tools_for_responses_api(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Chat Completions function-tool wrappers for Responses."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            converted.append(tool)
            continue
        nested = tool.get("function")
        if tool.get("type") == "function" and isinstance(nested, dict):
            converted.append(
                {
                    "type": "function",
                    "name": nested.get("name"),
                    "description": nested.get("description"),
                    "parameters": nested.get("parameters"),
                    "strict": nested.get("strict", False),
                }
            )
            continue
        converted.append(tool)
    return converted


async def _dispatch_chat_completion(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    tools: list[dict[str, Any]] | None = None,
    response_format: Any = None,
    max_tokens: int | None = None,
    reasoning: dict[str, Any] | None = None,
    extra_body: dict[str, Any] | None = None,
    google_search_tool: dict[str, Any] | None = None,
    google_generation_config: dict[str, Any] | None = None,
    google_system_instruction: str | None = None,
    google_thinking_config: dict[str, Any] | None = None,
    google_max_output_tokens: int | None = None,
):
    """Call the provider's native SDK with the prepared kwargs.

    Returns either a single response (stream=False) or an iterator of events
    (stream=True).
    """
    provider = get_llm_provider()
    if provider == LLMProvider.OPENAI:
        # Use Responses API for native web search; otherwise Chat Completions.
        use_responses = use_responses_api(tools, reasoning=reasoning)
        if use_responses:
            responses_kwargs: dict[str, Any] = {
                "model": model,
                "input": messages,
                "stream": stream,
            }
            if tools:
                responses_kwargs["tools"] = _tools_for_responses_api(tools)
            if response_format is not None:
                responses_kwargs["text"] = {
                    "format": _to_responses_text_format(response_format),
                }
            if max_tokens is not None:
                responses_kwargs["max_output_tokens"] = max_tokens
            if reasoning:
                responses_kwargs["reasoning"] = reasoning
            if extra_body:
                responses_kwargs["extra_body"] = extra_body
            return client.responses.create(**responses_kwargs)
        chat_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if tools:
            chat_kwargs["tools"] = tools
        if response_format is not None:
            chat_kwargs["response_format"] = response_format
        if max_tokens is not None:
            chat_kwargs["max_tokens"] = max_tokens
        if extra_body:
            chat_kwargs["extra_body"] = extra_body
        return client.chat.completions.create(**chat_kwargs)

    if provider == LLMProvider.GOOGLE:
        gen_kwargs: dict[str, Any] = {
            "model": model,
            "contents": messages,
        }
        config = gen_kwargs.setdefault("config", {})
        if tools:
            config.setdefault("tools", []).append(
                {"function_declarations": tools}
            )
        if google_search_tool is not None:
            config.setdefault("tools", []).append(google_search_tool)
        if google_generation_config is not None:
            for key, value in google_generation_config.items():
                config[key] = value
        token_limit = (
            google_max_output_tokens
            if google_max_output_tokens is not None
            else max_tokens
        )
        if token_limit is not None:
            config["max_output_tokens"] = token_limit
        if google_thinking_config is not None:
            config["thinking_config"] = google_thinking_config
        if google_system_instruction:
            config["system_instruction"] = google_system_instruction
        if not config:
            gen_kwargs.pop("config", None)
        if stream:
            return await client.aio.models.generate_content_stream(**gen_kwargs)
        return await asyncio.to_thread(
            client.models.generate_content, **gen_kwargs
        )

    # CUSTOM provider — OpenAI-compatible chat completions.
    chat_kwargs = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        chat_kwargs["tools"] = tools
    if response_format is not None:
        chat_kwargs["response_format"] = response_format
    if max_tokens is not None:
        chat_kwargs["max_tokens"] = max_tokens
    if extra_body:
        chat_kwargs["extra_body"] = extra_body
    return client.chat.completions.create(**chat_kwargs)


# ---------------------------------------------------------------------------
# Native stream iteration -> unified event shape
# ---------------------------------------------------------------------------


async def _iterate_openai_responses_stream(
    stream: Any,
    *,
    disconnect_checker: Optional[DisconnectChecker] = None,
):
    """Iterate OpenAI Responses API streaming events.

    Maps to the same event shape the chat service / smart deck generator expect:
    - ``_StreamContentChunk(type="content", chunk=text)``
    - ``_StreamThinkingChunk(type="thinking", chunk=text)``
    - ``_StreamCompletionChunk(type="completion", content=..., tool_calls=..., usage=..., raw=...)``
    """
    accumulated_text: list[str] = []
    accumulated_reasoning: list[str] = []
    final_response: Any = None
    async for event in _yield_stream_items(stream, disconnect_checker=disconnect_checker):
        event_type = getattr(event, "type", None)
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            delta = getattr(event, "delta", None)
            if isinstance(delta, str) and delta:
                accumulated_text.append(delta)
                yield _StreamContentChunk(chunk=delta)
        elif event_type == "response.reasoning_summary_text.delta":
            delta = getattr(event, "delta", None)
            if isinstance(delta, str) and delta:
                accumulated_reasoning.append(delta)
                yield _StreamThinkingChunk(chunk=delta)
        elif event_type == "response.completed":
            final_response = getattr(event, "response", None) or event
            break
        else:
            # Final response may arrive without a "completed" event in some
            # SDK versions; capture any object with a ``output`` attribute.
            if hasattr(event, "output") and event_type is None:
                final_response = event
    if final_response is None:
        final_response = _FakeResponse(
            text="".join(accumulated_text),
            reasoning="".join(accumulated_reasoning),
        )
    yield _StreamCompletionChunk(
        content="".join(accumulated_text) or None,
        tool_calls=tool_calls_from_openai_response(final_response),
        usage=getattr(final_response, "usage", None),
        raw=final_response,
    )


async def _iterate_openai_chat_stream(
    stream: Any,
    *,
    disconnect_checker: Optional[DisconnectChecker] = None,
):
    """Iterate OpenAI Chat Completions streaming chunks."""
    accumulated_text: list[str] = []
    accumulated_tool_calls: dict[int, dict[str, str]] = {}
    final_chunk: Any = None
    async for chunk in _yield_stream_items(stream, disconnect_checker=disconnect_checker):
        final_chunk = chunk
        choices = getattr(chunk, "choices", None) or []
        if not choices:
            continue
        delta = getattr(choices[0], "delta", None)
        if delta is None:
            continue
        content = getattr(delta, "content", None)
        if isinstance(content, str) and content:
            accumulated_text.append(content)
            yield _StreamContentChunk(chunk=content)
        for tool_call in getattr(delta, "tool_calls", None) or []:
            index = getattr(tool_call, "index", 0) or 0
            slot = accumulated_tool_calls.setdefault(
                index,
                {"id": "", "name": "", "arguments": ""},
            )
            new_id = getattr(tool_call, "id", None)
            if new_id:
                slot["id"] = new_id
            function = getattr(tool_call, "function", None)
            if function is not None:
                new_name = getattr(function, "name", None)
                if new_name:
                    slot["name"] = new_name
                new_args = getattr(function, "arguments", None)
                if new_args:
                    slot["arguments"] += new_args
    tool_calls = [
        AssistantToolCall(
            id=slot["id"] or f"chatcmpl-tool-{index}",
            name=slot["name"],
            arguments=slot["arguments"] or "{}",
        )
        for index, slot in sorted(accumulated_tool_calls.items())
        if slot["name"]
    ]
    yield _StreamCompletionChunk(
        content="".join(accumulated_text) or None,
        tool_calls=tool_calls,
        usage=getattr(final_chunk, "usage", None) if final_chunk else None,
        raw=final_chunk,
    )


async def _iterate_google_stream(
    stream: Any,
    *,
    disconnect_checker: Optional[DisconnectChecker] = None,
):
    """Iterate google-genai ``generate_content_stream`` events."""
    accumulated_text: list[str] = []
    accumulated_reasoning: list[str] = []
    final_response: Any = None
    async for chunk in _yield_stream_items(stream, disconnect_checker=disconnect_checker):
        final_response = chunk
        candidates = getattr(chunk, "candidates", None) or []
        if not candidates:
            continue
        parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
        for part in parts:
            thought = getattr(part, "thought", None)
            text = getattr(part, "text", None)
            if thought and isinstance(text, str) and text:
                accumulated_reasoning.append(text)
                yield _StreamThinkingChunk(chunk=text)
            elif isinstance(text, str) and text:
                accumulated_text.append(text)
                yield _StreamContentChunk(chunk=text)
    yield _StreamCompletionChunk(
        content="".join(accumulated_text) or None,
        tool_calls=tool_calls_from_google_response(final_response),
        usage=getattr(final_response, "usage_metadata", None) if final_response else None,
        raw=final_response,
    )


@dataclass
class _FakeResponse:
    """Lightweight stand-in when a provider doesn't emit a final event."""

    text: str = ""
    reasoning: str = ""

    @property
    def output(self):  # noqa: D401 — OpenAI Responses API shape
        return [
            _FakeOutputItem(text=self.text, reasoning=self.reasoning),
        ]

    @property
    def usage(self):
        return None


@dataclass
class _FakeOutputItem:
    type: str = "message"
    text: str = ""
    reasoning: str = ""

    def __post_init__(self) -> None:
        if self.reasoning:
            self.type = "reasoning"


# ---------------------------------------------------------------------------
# Public async streaming API
# ---------------------------------------------------------------------------


async def _raise_if_client_disconnected(
    disconnect_checker: Optional[DisconnectChecker],
) -> None:
    if disconnect_checker and await disconnect_checker():
        raise asyncio.CancelledError


async def stream_generate_events(
    client: Any,
    *,
    disconnect_checker: Optional[DisconnectChecker] = None,
    **kwargs: Any,
) -> AsyncGenerator[Any, None]:
    """Stream events from the native SDK in the unified ``llmai``-style shape.

    Yields ``_StreamContentChunk`` / ``_StreamThinkingChunk`` / ``_StreamCompletionChunk``
    instances whose ``.type`` attribute is one of ``"content"``, ``"thinking"``,
    or ``"completion"``. The completion chunk carries ``.content``, ``.tool_calls``,
    ``.usage``, and ``.raw`` so callers can extract metrics or run a tool-call loop.
    """
    await _raise_if_client_disconnected(disconnect_checker)

    provider = get_llm_provider()
    stream = kwargs.pop("stream", True)
    messages = kwargs.pop("messages", [])
    model = kwargs.pop("model", None)
    tools = kwargs.pop("tools", None)
    response_format = kwargs.pop("response_format", None)
    max_tokens = kwargs.pop("max_tokens", None)
    reasoning = kwargs.pop("reasoning", None)
    extra_body = kwargs.pop("extra_body", None)
    google_search_tool = kwargs.pop("google_search_tool", None)
    generation_config = kwargs.pop("generation_config", None)
    system_instruction = kwargs.pop("system_instruction", None)
    thinking_config = kwargs.pop("thinking_config", None)
    max_output_tokens = kwargs.pop("max_output_tokens", None)
    token_limit = max_tokens if max_tokens is not None else max_output_tokens

    dispatch_kwargs = dict(
        tools=tools,
        response_format=response_format,
        max_tokens=token_limit,
        reasoning=reasoning,
        extra_body=extra_body,
        google_search_tool=google_search_tool,
        google_generation_config=generation_config,
        google_system_instruction=system_instruction,
        google_thinking_config=thinking_config,
        google_max_output_tokens=max_output_tokens,
    )

    if not stream:
        # Non-streaming path: synthesize a single completion event.
        response = await _dispatch_chat_completion(
            client,
            model=model,
            messages=messages,
            stream=False,
            **dispatch_kwargs,
        )
        if provider == LLMProvider.GOOGLE:
            tool_calls = tool_calls_from_google_response(response)
        else:
            tool_calls = tool_calls_from_openai_response(response)
        content = _completion_content(response)
        yield _StreamCompletionChunk(
            content=content,
            tool_calls=tool_calls,
            usage=getattr(response, "usage", None)
            or getattr(response, "usage_metadata", None),
            raw=response,
        )
        return

    native_stream = await _dispatch_chat_completion(
        client,
        model=model,
        messages=messages,
        stream=True,
        **dispatch_kwargs,
    )

    await _raise_if_client_disconnected(disconnect_checker)

    if provider == LLMProvider.OPENAI:
        if use_responses_api(tools, reasoning=reasoning):
            async for event in _iterate_openai_responses_stream(
                native_stream, disconnect_checker=disconnect_checker
            ):
                yield event
            return
        async for event in _iterate_openai_chat_stream(
            native_stream, disconnect_checker=disconnect_checker
        ):
            yield event
        return

    if provider == LLMProvider.GOOGLE:
        async for event in _iterate_google_stream(
            native_stream, disconnect_checker=disconnect_checker
        ):
            yield event
        return

    # CUSTOM — OpenAI-compatible chat completions.
    async for event in _iterate_openai_chat_stream(
        native_stream, disconnect_checker=disconnect_checker
    ):
        yield event


# ---------------------------------------------------------------------------
# Content extraction (preserved from llmai-era code)
# ---------------------------------------------------------------------------


def extract_text(content: Any) -> Optional[str]:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence) and not isinstance(content, (bytes, bytearray)):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str):
                parts.append(text)
        joined = "".join(parts)
        return joined or None
    text = getattr(content, "text", None)
    if isinstance(text, str):
        return text
    return None


def extract_structured_content(content: Any) -> Optional[dict]:
    if content is None:
        return None
    if isinstance(content, dict):
        plain = to_plain_data(content)
        return plain if isinstance(plain, dict) else None
    if hasattr(content, "model_dump"):
        dumped = content.model_dump(mode="json")
        if isinstance(dumped, dict):
            return to_plain_data(dumped)

    raw_text = extract_text(content)
    if not raw_text:
        return None

    try:
        parsed = dirtyjson.loads(raw_text)
    except Exception:
        return None

    if isinstance(parsed, dict):
        plain = to_plain_data(parsed)
        return plain if isinstance(plain, dict) else None
    return None


def serialize_structured_content(content: Any) -> Optional[str]:
    parsed = extract_structured_content(content)
    if parsed is not None:
        return json.dumps(parsed, ensure_ascii=False)

    raw_text = extract_text(content)
    if raw_text:
        return raw_text
    return None


def message_content_to_text(content: Sequence[Any] | str | None) -> Optional[str]:
    joined = "".join(
        part.text
        for part in normalize_content_parts(content)
        if isinstance(getattr(part, "text", None), str)
    )
    return joined or None


# ---------------------------------------------------------------------------
# Structured-with-schema retries (preserved; reads local dataclasses now)
# ---------------------------------------------------------------------------


async def _generate_structured_content(
    client: Any,
    *,
    disconnect_checker: Optional[DisconnectChecker],
    text_chunk_callback: Optional[TextChunkCallback] = None,
    force_stream: Optional[bool] = None,
    **kwargs: Any,
) -> Optional[dict]:
    use_stream = (
        True
        if force_stream is True
        else False
        if force_stream is False
        else disconnect_checker is not None or text_chunk_callback is not None
    )
    completion_content: Any = None
    streamed_text: list[str] = []
    stream_kwargs = dict(kwargs)
    stream_kwargs["stream"] = use_stream
    async for event in stream_generate_events(
        client,
        disconnect_checker=disconnect_checker,
        **stream_kwargs,
    ):
        # Poll the client disconnect flag on every event so a hung/stalled LLM
        # stream is aborted promptly even when ``stream_generate_events`` is
        # patched out by callers (no-op when ``disconnect_checker`` is None).
        await _raise_if_client_disconnected(disconnect_checker)
        if isinstance(event, _StreamCompletionChunk) or getattr(event, "type", None) == "completion":
            completion_content = event.content
        elif getattr(event, "type", None) == "content":
            chunk = getattr(event, "chunk", None)
            if isinstance(chunk, str):
                streamed_text.append(chunk)
                if text_chunk_callback is not None:
                    await text_chunk_callback(chunk)

    content = extract_structured_content(completion_content)
    if content is not None:
        if text_chunk_callback is not None and not streamed_text:
            serialized = serialize_structured_content(completion_content)
            if serialized:
                await text_chunk_callback(serialized)
        return content
    return extract_structured_content("".join(streamed_text))


def _completion_content(response: Any) -> Any:
    if response is None:
        return None
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text
    content = getattr(response, "content", None)
    if content is not None:
        return content
    text = getattr(response, "text", None)
    if text is not None:
        return text
    # OpenAI Chat Completions: content lives in choices[0].message.content
    choices = getattr(response, "choices", None)
    if choices:
        message = getattr(choices[0], "message", None)
        if message is not None:
            return getattr(message, "content", None)
    # google-genai: text lives in candidates[0].content.parts[*].text
    candidates = getattr(response, "candidates", None)
    if candidates:
        parts = getattr(getattr(candidates[0], "content", None), "parts", None)
        if parts:
            return getattr(parts[0], "text", None)
    return None


async def generate_structured_with_schema_retries(
    client: Any,
    model: str,
    *,
    messages: Sequence[Message],
    response_format: JSONSchemaResponse,
    json_schema: dict,
    strict: bool = False,
    validate_schema: bool = False,
    validate_schema_max_loop_count: int = 4,
    disconnect_checker: Optional[DisconnectChecker] = None,
    text_chunk_callback: Optional[TextChunkCallback] = None,
    stream: Optional[bool] = None,
) -> dict:
    """Parse retries (inner) plus JSON Schema validation feedback loops (outer)."""
    del strict  # strict is encoded inside ``response_format``
    max_validation_loops = max(1, validate_schema_max_loop_count)
    working_messages: list[Message] = list(messages)

    for validation_attempt in range(max_validation_loops):
        content: Optional[dict] = None
        for attempt in range(3):
            await _raise_if_client_disconnected(disconnect_checker)
            content = await _generate_structured_content(
                client,
                disconnect_checker=disconnect_checker,
                text_chunk_callback=(
                    text_chunk_callback
                    if validation_attempt == 0 and attempt == 0
                    else None
                ),
                force_stream=stream,
                **get_generate_kwargs(
                    model=model,
                    messages=working_messages,
                    response_format=response_format,
                ),
            )
            if content is not None:
                break
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))

        if content is None:
            raise HTTPException(
                status_code=400,
                detail="LLM did not return any content",
            )

        if not validate_schema:
            return content

        validation_errors = get_schema_validation_errors(
            json_schema,
            content,
            strict=False,
        )

        if not validation_errors:
            return content

        formatted_validation_errors = " | ".join(validation_errors)
        if validation_attempt == max_validation_loops - 1:
            LOGGER.warning(
                "Validation error after max fixes, returning last response: %s",
                formatted_validation_errors,
            )
            return content

        LOGGER.warning(
            "Validation error, attempting fix %s/%s: %s",
            validation_attempt + 1,
            max_validation_loops - 1,
            formatted_validation_errors,
        )
        working_messages.append(
            structured_validation_feedback_user_message(content, validation_errors)
        )

    raise HTTPException(status_code=400, detail="LLM did not return any content")


def structured_validation_feedback_user_message(
    content: dict,
    validation_errors: list[str],
) -> UserMessage:
    max_error_count = 10
    max_json_chars = 6000

    formatted_errors = validation_errors[:max_error_count]
    if len(validation_errors) > max_error_count:
        formatted_errors.append(
            f"...and {len(validation_errors) - max_error_count} more validation errors."
        )

    previous_response = json.dumps(
        content,
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    if len(previous_response) > max_json_chars:
        previous_response = previous_response[:max_json_chars] + "\n... (truncated)"

    return UserMessage(
        content=(
            "The previous JSON response did not match the required response schema.\n\n"
            "Validation errors:\n"
            + "\n".join(f"- {error}" for error in formatted_errors)
            + "\n\nPrevious invalid JSON:\n"
            + f"```json\n{previous_response}\n```\n\n"
            + "Return corrected JSON only. Make sure it fully matches the required schema."
        )
    )


# ---------------------------------------------------------------------------
# Token estimation + metrics (preserved)
# ---------------------------------------------------------------------------


def estimate_text_tokens(value: str) -> int:
    """Return a stable approximation when a provider omits token usage."""
    return max(1, round(len(value) / 4)) if value else 0


def estimate_thinking_tokens(value: str) -> int:
    """Match llmai's visible-reasoning estimate for streamed thinking chunks."""
    return math.ceil(len(value.encode("utf-8")) / 4) if value else 0


def estimate_message_tokens(messages: Sequence[Message]) -> int:
    return sum(
        estimate_text_tokens(extract_text(getattr(message, "content", None)) or "")
        for message in messages
    )


def _usage_token_value(usage: Any, *names: str) -> Optional[int]:
    for name in names:
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        if isinstance(value, (int, float)):
            return int(value)
    return None


def _usage_thinking_tokens(usage: Any) -> tuple[Optional[int], bool]:
    if usage is None:
        return None, False

    reasoning = (
        usage.get("reasoning")
        if isinstance(usage, dict)
        else getattr(usage, "reasoning", None)
    )
    billed_tokens = _usage_token_value(reasoning, "billed_tokens")
    if billed_tokens is not None:
        billed_estimated = (
            reasoning.get("billed_estimated", False)
            if isinstance(reasoning, dict)
            else getattr(reasoning, "billed_estimated", False)
        )
        return billed_tokens, bool(billed_estimated)

    visible_tokens = _usage_token_value(reasoning, "visible_tokens")
    if visible_tokens is not None:
        return visible_tokens, True

    return _usage_token_value(usage, "thinking_tokens", "reasoning_tokens"), False


def build_text_generation_metrics(
    *,
    model: str,
    messages: Sequence[Message],
    content: str,
    streamed_thinking: str,
    completion: Any,
    started_at: float,
    model_supports_thinking: bool = False,
) -> TextGenerationMetrics:
    usage = getattr(completion, "usage", None) if completion is not None else None
    exact_input_tokens = _usage_token_value(usage, "input_tokens", "prompt_tokens")
    exact_output_tokens = _usage_token_value(
        usage, "output_tokens", "completion_tokens"
    )
    input_tokens = (
        exact_input_tokens
        if exact_input_tokens is not None
        else estimate_message_tokens(messages)
    )
    output_tokens = (
        exact_output_tokens
        if exact_output_tokens is not None
        else estimate_text_tokens(content)
    )
    thinking_tokens, thinking_tokens_estimated = _usage_thinking_tokens(usage)
    if thinking_tokens is None and streamed_thinking:
        thinking_tokens = estimate_thinking_tokens(streamed_thinking)
        thinking_tokens_estimated = True

    supports_thinking = bool(
        model_supports_thinking
        or thinking_tokens is not None
        or streamed_thinking
    )
    if (
        supports_thinking
        and exact_output_tokens is not None
        and (thinking_tokens is None or thinking_tokens_estimated)
    ):
        inferred_thinking_tokens = max(
            0,
            exact_output_tokens - estimate_text_tokens(content),
        )
        if thinking_tokens is None or inferred_thinking_tokens > thinking_tokens:
            thinking_tokens = inferred_thinking_tokens
            thinking_tokens_estimated = True
    if supports_thinking and thinking_tokens is None:
        thinking_tokens = 0
        thinking_tokens_estimated = True

    duration_seconds = (
        getattr(completion, "duration_seconds", None)
        if completion is not None
        else None
    )
    if not isinstance(duration_seconds, (int, float)) or duration_seconds <= 0:
        duration_seconds = max(time.perf_counter() - started_at, 1e-9)
    total_tokens = _usage_token_value(usage, "total_tokens")
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens

    return TextGenerationMetrics(
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        tokens_per_second=output_tokens / duration_seconds,
        duration_seconds=duration_seconds,
        estimated=exact_input_tokens is None or exact_output_tokens is None,
        thinking_tokens=thinking_tokens,
        thinking_tokens_estimated=thinking_tokens_estimated,
        supports_thinking=supports_thinking,
    )


__all__ = [
    "AssistantMessage",
    "AssistantToolCall",
    "DisconnectChecker",
    "JSONSchemaResponse",
    "Message",
    "ReasoningConfig",
    "SystemMessage",
    "TextContentPart",
    "TextChunkCallback",
    "TextGenerationMetrics",
    "Tool",
    "ToolResponseMessage",
    "UserMessage",
    "WebSearchTool",
    "build_text_generation_metrics",
    "estimate_message_tokens",
    "estimate_text_tokens",
    "estimate_thinking_tokens",
    "extract_structured_content",
    "extract_text",
    "generate_structured_with_schema_retries",
    "get_generate_kwargs",
    "message_content_to_text",
    "messages_to_provider_payload",
    "normalize_content_parts",
    "serialize_structured_content",
    "stream_generate_events",
    "structured_validation_feedback_user_message",
]
