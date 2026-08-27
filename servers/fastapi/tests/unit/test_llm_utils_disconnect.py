"""Disconnect / streaming retry tests for ``generate_structured_with_schema_retries``.

After dropping ``llmai``, the structured generator is event-driven via
``stream_generate_events`` (no ``client.generate`` method). These tests
patch that entry point directly so we can simulate streaming, content,
and completion payloads without needing a real provider.
"""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from utils.llm_utils import generate_structured_with_schema_retries


async def _collect(generator):
    result = None
    async for event in generator:
        if getattr(event, "type", None) == "completion":
            result = getattr(event, "content", None)
            break
    return result


def test_regular_generation_keeps_existing_retry_behavior(monkeypatch):
    monkeypatch.setenv("LLM", "openai")

    responses = [None, {"result": "ok"}]
    call_count = 0

    async def fake_stream_generate_events(_client, **_kwargs):
        nonlocal call_count
        call_count += 1
        yield SimpleNamespace(type="completion", content=responses.pop(0))

    monkeypatch.setattr(
        "utils.llm_utils.stream_generate_events",
        fake_stream_generate_events,
    )

    with patch("utils.llm_utils.asyncio.sleep", new=AsyncMock()):
        result = asyncio.run(
            generate_structured_with_schema_retries(
                object(),
                "test-model",
                messages=[],
                response_format=object(),
                json_schema={},
            )
        )

    assert result == {"result": "ok"}
    assert call_count == 2


def test_explicit_stream_false_skips_sse_even_with_disconnect_checker(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    responses = [{"result": "ok"}]
    call_count = 0

    async def fake_stream_generate_events(_client, **_kwargs):
        nonlocal call_count
        call_count += 1
        yield SimpleNamespace(type="completion", content=responses.pop(0))

    monkeypatch.setattr(
        "utils.llm_utils.stream_generate_events",
        fake_stream_generate_events,
    )

    async def is_disconnected():
        return False

    result = asyncio.run(
        generate_structured_with_schema_retries(
            object(),
            "test-model",
            messages=[],
            response_format=object(),
            json_schema={},
            disconnect_checker=is_disconnected,
            stream=False,
        )
    )

    assert result == {"result": "ok"}
    assert call_count == 1


def test_disconnect_cancels_generation_without_retrying(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    started = threading.Event()
    closed = threading.Event()

    async def fake_stream_generate_events(_client, **_kwargs):
        try:
            while True:
                started.set()
                yield SimpleNamespace(type="content", chunk='{"result":"pending"}')
                await asyncio.sleep(0)
                time.sleep(0.001)
        finally:
            closed.set()

    monkeypatch.setattr(
        "utils.llm_utils.stream_generate_events",
        fake_stream_generate_events,
    )

    async def run():
        async def is_disconnected():
            return started.is_set()

        with pytest.raises(asyncio.CancelledError):
            await generate_structured_with_schema_retries(
                object(),
                "test-model",
                messages=[],
                response_format=object(),
                json_schema={},
                disconnect_checker=is_disconnected,
            )

        while not closed.is_set():
            await asyncio.sleep(0.001)

    asyncio.run(run())


def test_connected_request_uses_stream_completion_content(monkeypatch):
    monkeypatch.setenv("LLM", "openai")

    async def fake_stream_generate_events(_client, **_kwargs):
        yield SimpleNamespace(type="completion", content={"result": "complete"})

    monkeypatch.setattr(
        "utils.llm_utils.stream_generate_events",
        fake_stream_generate_events,
    )

    async def is_disconnected():
        return False

    result = asyncio.run(
        generate_structured_with_schema_retries(
            object(),
            "test-model",
            messages=[],
            response_format=object(),
            json_schema={},
            disconnect_checker=is_disconnected,
        )
    )

    assert result == {"result": "complete"}


def test_connected_request_keeps_schema_validation_retries(monkeypatch):
    monkeypatch.setenv("LLM", "openai")

    responses = [{"wrong": "value"}, {"result": "fixed"}]
    call_count = 0

    async def fake_stream_generate_events(_client, **_kwargs):
        nonlocal call_count
        call_count += 1
        yield SimpleNamespace(type="completion", content=responses.pop(0))

    monkeypatch.setattr(
        "utils.llm_utils.stream_generate_events",
        fake_stream_generate_events,
    )

    async def is_disconnected():
        return False

    result = asyncio.run(
        generate_structured_with_schema_retries(
            object(),
            "test-model",
            messages=[],
            response_format=object(),
            json_schema={
                "type": "object",
                "properties": {"result": {"type": "string"}},
                "required": ["result"],
            },
            validate_schema=True,
            disconnect_checker=is_disconnected,
        )
    )

    assert result == {"result": "fixed"}
    assert call_count == 2


def test_stream_generate_events_polls_disconnect_during_iteration(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    checks = {"count": 0}

    class SlowStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.sleep(0.05)
            raise StopAsyncIteration

    async def fake_dispatch(*_args, **_kwargs):
        return SlowStream()

    monkeypatch.setattr("utils.llm_utils._dispatch_chat_completion", fake_dispatch)
    monkeypatch.setattr(
        "utils.llm_utils.get_llm_provider",
        lambda: __import__("enums.llm_provider", fromlist=["LLMProvider"]).LLMProvider.OPENAI,
    )
    monkeypatch.setattr("utils.llm_utils.use_responses_api", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("utils.llm_utils.CLIENT_DISCONNECT_POLL_SECONDS", 0.01)

    async def is_disconnected():
        checks["count"] += 1
        return checks["count"] > 2

    async def run():
        with pytest.raises(asyncio.CancelledError):
            async for _ in __import__(
                "utils.llm_utils", fromlist=["stream_generate_events"]
            ).stream_generate_events(
                object(),
                disconnect_checker=is_disconnected,
                model="test",
                messages=[],
                stream=True,
            ):
                pass

    asyncio.run(run())
    assert checks["count"] >= 2
