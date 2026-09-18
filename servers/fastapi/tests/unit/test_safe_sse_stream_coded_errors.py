"""Phase 5: ``safe_sse_stream`` forwards machine error codes.

- ``CodedHTTPException`` forwarded with ``error_code``/``error_params``.
- Plain ``HTTPException`` keeps the legacy error frame byte-for-byte.
- Other exceptions use the caller's catch-all code when provided.
"""
import asyncio
import json
from unittest.mock import MagicMock

from fastapi import HTTPException

from utils.coded_exception import CodedHTTPException
from utils.sse import safe_sse_stream


def _error_payload(frame: str) -> dict:
    lines = frame.strip().split("\n")
    assert lines[0] == "event: response"
    assert lines[1].startswith("data: ")
    return json.loads(lines[1][len("data: ") :])


def _collect(stream, **kwargs) -> list[str]:
    async def runner():
        frames = []
        async for frame in safe_sse_stream(
            stream, logger=MagicMock(), **kwargs
        ):
            frames.append(frame)
        return frames

    return asyncio.run(runner())


def test_coded_http_exception_forwards_code_and_params():
    async def coded_stream():
        yield "data"
        raise CodedHTTPException(
            502,
            "Provider failed",
            error_code="error.provider.generic",
            error_params={"prefix": "Generation"},
        )

    frames = _collect(
        coded_stream(),
        error_detail="Catch-all",
        error_code="error.catch_all",
    )
    assert len(frames) == 2
    assert _error_payload(frames[-1]) == {
        "type": "error",
        "detail": "Provider failed",
        "error_code": "error.provider.generic",
        "error_params": {"prefix": "Generation"},
    }


def test_plain_http_exception_keeps_legacy_frame_exactly():
    async def plain_stream():
        raise HTTPException(status_code=418, detail="teapot")
        yield  # pragma: no cover — makes this an async generator

    frames = _collect(
        plain_stream(),
        error_detail="Catch-all",
        error_code="error.catch_all",
    )
    assert frames == [
        'event: response\ndata: {"type": "error", "detail": "teapot"}\n\n'
    ]


def test_generic_exception_uses_caller_catch_all_code():
    async def broken_stream():
        raise RuntimeError("provider failed")
        yield  # pragma: no cover

    frames = _collect(
        broken_stream(),
        error_detail="Failed to generate presentation outlines. Please try again.",
        error_code="error.outline_generation_failed",
    )
    assert _error_payload(frames[-1]) == {
        "type": "error",
        "detail": "Failed to generate presentation outlines. Please try again.",
        "error_code": "error.outline_generation_failed",
    }


def test_generic_exception_without_caller_code_keeps_legacy_frame():
    async def broken_stream():
        raise RuntimeError("provider failed")
        yield  # pragma: no cover

    frames = _collect(broken_stream(), error_detail="Chat stream failed")
    assert frames == [
        'event: response\ndata: {"type": "error", "detail": "Chat stream failed"}\n\n'
    ]