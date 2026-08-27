from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from api.v1.auth.assets import is_app_data_path_authorized
from api.v1.auth.context import (
    reset_current_owner_id,
    set_current_owner_id,
)
from api.v1.auth.principal import resolve_request_principal
from api.v1.auth.users import get_jwt_strategy
from models.sql.provider_settings import ProviderSettings
from models.sql.user_provider_settings import UserProviderSettings
from models.sql.user import User
from services.provider_settings import PROVIDER_SETTINGS_ID, sanitize_user_overlay
from services.database import async_session_maker
from utils.get_env import is_disable_auth_enabled
from utils.provider_overlay import (
    reset_provider_overlay,
    set_provider_overlay,
)


class UserConfigEnvUpdateMiddleware(BaseHTTPMiddleware):
    """Pass-through placeholder kept for backward compatibility.

    The previous behavior copied ``userConfig.json`` into ``os.environ`` on
    every request. That is unsafe now that two users can hold different
    provider keys at the same time, so the middleware no longer mutates env.
    Settings reads go through the per-user overlay + session middleware.
    """

    async def dispatch(self, request: Request, call_next):
        return await call_next(request)


class SessionAuthMiddleware(BaseHTTPMiddleware):
    _PUBLIC_AUTH_PATHS = {
        "/api/v1/auth/status",
        "/api/v1/auth/verify",
        "/api/v1/auth/login",
        "/api/v1/auth/logout",
        "/api/v1/auth/register",
    }
    _PUBLIC_AUTH_PREFIXES: tuple[str, ...] = ()
    _PUBLIC_APP_DATA_PREFIXES = (
        "/app_data/fonts/",
        "/app_data/templates/",
    )
    _PROTECTED_NON_API_PATHS = {"/docs", "/openapi.json", "/redoc"}

    def _requires_auth(self, path: str) -> bool:
        if any(path.startswith(prefix) for prefix in self._PUBLIC_AUTH_PREFIXES):
            return False
        if path.startswith("/api/"):
            return True
        if any(path.startswith(prefix) for prefix in self._PUBLIC_APP_DATA_PREFIXES):
            return False
        if path.startswith("/app_data/"):
            return True
        return path in self._PROTECTED_NON_API_PATHS

    async def dispatch(self, request: Request, call_next):
        if is_disable_auth_enabled():
            overlay_token = None
            try:
                async with async_session_maker() as session:
                    overlay_token = await _set_disable_auth_overlay(session)
                    return await call_next(request)
            finally:
                if overlay_token is not None:
                    reset_provider_overlay(overlay_token)

        path = request.url.path
        if (
            request.method == "OPTIONS"
            or not self._requires_auth(path)
            or path in self._PUBLIC_AUTH_PATHS
        ):
            return await call_next(request)

        async with async_session_maker() as session:
            configured = bool(
                await session.scalar(select(func.count()).select_from(User))
            )
            if not configured:
                return JSONResponse(
                    status_code=428,
                    content={"detail": "No accounts yet"},
                )
            principal, user = await resolve_request_principal(request, session)
            if principal is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Unauthorized"},
                )
            request.state.auth_principal = principal
            request.state.current_user = user
            request.state.auth_username = principal.username
            if principal.method == "api_key" and user is not None:
                request.state.internal_session_token = (
                    await get_jwt_strategy().write_token(user)
                )
            context_token = set_current_owner_id(principal.user_id)
            overlay_token = await _set_request_overlay(session, user)
            try:
                if path.startswith(
                    "/app_data/"
                ) and not is_app_data_path_authorized(
                    path,
                    user_id=principal.user_id,
                ):
                    return JSONResponse(
                        status_code=404,
                        content={"detail": "Asset not found"},
                    )
                return await call_next(request)
            finally:
                reset_provider_overlay(overlay_token)
                reset_current_owner_id(context_token)


async def _set_disable_auth_overlay(session: AsyncSession):
    """Load the singleton provider row so DISABLE_AUTH requests see saved keys."""
    row = await session.get(ProviderSettings, PROVIDER_SETTINGS_ID)
    config = dict(row.config) if row is not None and row.config else {}
    return set_provider_overlay(sanitize_user_overlay(config))


async def _set_request_overlay(
    session: AsyncSession, user: User | None
):
    """Load the current user's provider overlay into the request ContextVar."""
    if user is None:
        return set_provider_overlay({})
    row = await session.get(UserProviderSettings, user.id)
    config = dict(row.config) if row is not None and row.config else {}
    return set_provider_overlay(config)
