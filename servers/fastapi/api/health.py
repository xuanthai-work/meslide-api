"""Process liveness and dependency readiness probes.

``GET /health/live`` is cheap: the process accepted the request.
``GET /health/ready`` checks the database, Alembic head, and required object
storage. Orchestrators should not restart a live process just because a
dependency blipped.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from services.database import async_session_maker
from services.object_storage.readiness import check_object_storage_readiness


logger = logging.getLogger(__name__)

HEALTH_ROUTER = APIRouter(tags=["Health"])


async def check_database() -> str:
    try:
        async with async_session_maker() as session:
            await session.execute(text("SELECT 1"))
        return "ok"
    except Exception:
        logger.warning("Readiness database check failed", exc_info=True)
        return "unavailable"


async def check_migration() -> str:
    try:
        from migrations import check_migrations_at_head

        return await asyncio.to_thread(check_migrations_at_head)
    except Exception:
        logger.warning("Readiness migration check failed", exc_info=True)
        return "unavailable"


async def check_storage() -> str:
    try:
        result = await check_object_storage_readiness()
    except Exception:
        logger.warning("Readiness storage check failed", exc_info=True)
        return "unavailable"
    if result.get("available"):
        return "ok"
    return str(result.get("reason") or "unavailable")


@HEALTH_ROUTER.get("/health/live")
async def get_live():
    return {"status": "ok"}


@HEALTH_ROUTER.get("/health/ready")
async def get_ready():
    checks = {
        "database": await check_database(),
        "migration": await check_migration(),
        "storage": await check_storage(),
    }
    ready = all(value == "ok" for value in checks.values())
    body = {
        "status": "ok" if ready else "not_ready",
        "checks": checks,
    }
    if ready:
        return body
    return JSONResponse(status_code=503, content=body)
