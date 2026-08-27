import asyncio
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

from fastapi import HTTPException

from models.sse_response import SSEErrorResponse, SSEHeartbeatResponse

SSE_HEARTBEAT_SECONDS = 15.0


async def safe_sse_stream(
    stream: AsyncIterator[str],
    *,
    logger: logging.Logger,
    error_detail: str,
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
                yield SSEErrorResponse(detail=str(detail)).to_string()
                break
    finally:
        if not producer.done():
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)