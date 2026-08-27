import asyncio
from types import SimpleNamespace

from utils.llm_utils import _iterate_openai_chat_stream


def _chunk(text: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=None),
            )
        ],
        usage=None,
    )


async def _collect(stream):
    events = []
    async for event in _iterate_openai_chat_stream(stream):
        events.append(event)
    return events


def test_iterate_openai_chat_stream_accepts_sync_stream():
    events = asyncio.run(_collect([_chunk("hello")]))

    assert [event.type for event in events] == ["content", "completion"]
    assert events[0].chunk == "hello"
    assert events[1].content == "hello"


def test_iterate_openai_chat_stream_accepts_async_stream():
    async def async_chunks():
        yield _chunk("world")

    events = asyncio.run(_collect(async_chunks()))

    assert [event.type for event in events] == ["content", "completion"]
    assert events[0].chunk == "world"
    assert events[1].content == "world"
