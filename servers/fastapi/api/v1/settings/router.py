from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.auth.users import read_user_from_cookie
from models.sql.user import User
from services.database import get_async_session
from services.provider_settings import (
    get_provider_settings,
    get_user_provider_overlay,
    persist_instance_level_fields,
    save_provider_settings,
    save_user_provider_overlay,
    sanitize_user_overlay,
)
from utils.get_env import get_can_change_keys_env, is_disable_auth_enabled
from utils.user_config import update_env_with_user_config


API_V1_SETTINGS_ROUTER = APIRouter(prefix="/api/v1/settings", tags=["Settings"])


def _require_settings_user(user: User | None) -> None:
    if is_disable_auth_enabled():
        return
    if user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")


@API_V1_SETTINGS_ROUTER.get("/provider")
async def read_provider_settings(
    user: User | None = Depends(read_user_from_cookie),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    _require_settings_user(user)
    if is_disable_auth_enabled():
        return await get_provider_settings(session)
    assert user is not None
    return await get_user_provider_overlay(session, user.id)


@API_V1_SETTINGS_ROUTER.put("/provider")
async def update_provider_settings(
    config: dict[str, Any] = Body(...),
    user: User | None = Depends(read_user_from_cookie),
    session: AsyncSession = Depends(get_async_session),
) -> dict[str, Any]:
    _require_settings_user(user)
    overlay_writes = sanitize_user_overlay(config or {})
    if get_can_change_keys_env() == "false" and overlay_writes:
        raise HTTPException(
            status_code=403,
            detail="You are not allowed to access this resource",
        )
    persist_instance_level_fields(config or {})
    if is_disable_auth_enabled():
        saved = await save_provider_settings(session, config)
        update_env_with_user_config()
        return saved
    assert user is not None
    return await save_user_provider_overlay(session, user.id, config)
