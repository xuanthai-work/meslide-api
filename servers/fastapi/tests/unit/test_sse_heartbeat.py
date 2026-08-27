import asyncio
import json

from models.sse_response import SSEHeartbeatResponse
from utils.sse import safe_sse_stream


def _payload(frame: str) -> dict:
    data_line = next(
        line for line in frame.splitlines() if line.startswith("data:")
    )
    return json.loads(data_line[len("data:") :].strip())


def test_heartbeat_response_type():
    payload = _payload(SSEHeartbeatResponse().to_string())
    assert payload == {"type": "heartbeat"}


def test_safe_sse_stream_emits_heartbeat_while_inner_blocked():
    async def inner():
        await asyncio.sleep(0.05)
        yield "chunk-one\n\n"
        await asyncio.sleep(0.05)
        yield "chunk-two\n\n"

    async def collect():
        frames = []
        async for frame in safe_sse_stream(
            inner(),
            logger=__import__("logging").getLogger("test"),
            error_detail="boom",
            heartbeat_seconds=0.02,
        ):
            frames.append(frame)
        return frames

    frames = asyncio.run(collect())
    types = []
    for frame in frames:
        if frame == "chunk-one\n\n" or frame == "chunk-two\n\n":
            types.append(frame.strip())
            continue
        types.append(_payload(frame)["type"])
    assert "heartbeat" in types
    assert types[-1] == "chunk-two"


def test_safe_sse_stream_cancelled_does_not_emit_error():
    async def inner():
        raise asyncio.CancelledError
        yield  # make this an async generator so CancelledError surfaces on iteration

    async def collect():
        return [
            frame
            async for frame in safe_sse_stream(
                inner(),
                logger=__import__("logging").getLogger("test"),
                error_detail="boom",
            )
        ]

    assert asyncio.run(collect()) == []