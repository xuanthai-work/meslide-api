import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from typing import Any, Optional

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from models.sse_response import SSEErrorResponse, SSEHeartbeatResponse
from utils.coded_exception import CodedHTTPException

SSE_HEARTBEAT_SECONDS = 15.0
SSE_RESPONSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
}


def sse_streaming_response(stream: AsyncIterator[Any]) -> StreamingResponse:
    """Return an SSE response that proxies must forward without buffering."""
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers=SSE_RESPONSE_HEADERS,
    )


async def safe_sse_stream(
    stream: AsyncIterator[str],
    *,
    logger: logging.Logger,
    error_detail: str,
    error_code: Optional[str] = None,
    error_params: Optional[dict] = None,
    on_error: Callable[[], Awaitable[None]] | None = None,
    heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
) -> AsyncGenerator[str, None]:
    queue: asyncio.Queue = asyncio.Queue()

    async def produce() -> None:
        try:
            async for chunk in stream:
                await queue.put(("data", chunk))
        except asyncio.CancelledError:
            await queue.put(("cancelled", None))
            raise
        except Exception as exc:
            await queue.put(("error", exc))
        else:
            await queue.put(("end", None))

    producer = asyncio.create_task(produce())
    try:
        while True:
            try:
                kind, value = await asyncio.wait_for(
                    queue.get(), timeout=heartbeat_seconds
                )
            except asyncio.TimeoutError:
                yield SSEHeartbeatResponse().to_string()
                continue
            if kind == "data":
                yield value
            elif kind == "end":
                break
            elif kind == "cancelled":
                logger.info("SSE stream cancelled by client")
                return
            else:
                logger.exception("SSE stream failed after response started")
                if on_error:
                    try:
                        await on_error()
                    except Exception:
                        logger.exception("SSE stream error cleanup failed")
                detail = (
                    value.detail
                    if isinstance(value, HTTPException)
                    else error_detail
                )
                # Coded exceptions forward their own machine code; plain
                # HTTPExceptions keep the legacy (uncoded) error frame; other
                # exceptions fall back to the caller's catch-all code.
                if isinstance(value, CodedHTTPException):
                    frame_error_code = value.error_code
                    frame_error_params = value.error_params
                elif isinstance(value, HTTPException):
                    frame_error_code = None
                    frame_error_params = None
                else:
                    frame_error_code = error_code
                    frame_error_params = error_params
                yield SSEErrorResponse(
                    detail=str(detail),
                    error_code=frame_error_code,
                    error_params=frame_error_params,
                ).to_string()
                break
    finally:
        if not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
