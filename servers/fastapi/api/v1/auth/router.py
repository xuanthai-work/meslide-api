from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.auth.assets import is_app_data_path_authorized
from api.v1.auth.demo_user import resolve_demo_user
from api.v1.auth.principal import resolve_request_principal
from api.v1.auth.users import (
    read_user_from_cookie,
    serialize_user,
)
from models.sql.user import User
from services.database import get_async_session
from utils.get_env import is_disable_auth_enabled
from api.v1.auth.token import TOKEN_ROUTER


API_V1_AUTH_ROUTER = APIRouter(prefix="/api/v1/auth", tags=["Auth"])
API_V1_AUTH_ROUTER.include_router(TOKEN_ROUTER)


async def _account_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(User)) or 0)


@API_V1_AUTH_ROUTER.get("/status")
async def get_status(
    session: AsyncSession = Depends(get_async_session),
    user: User | None = Depends(read_user_from_cookie),
):
    if is_disable_auth_enabled():
        return {
            "configured": True,
            "authenticated": True,
            "username": "local",
            "user_id": None,
        }
    if user is None:
        # No cookie/bearer session. In the default-on demo mode there is no
        # login flow (login returns 410), so a cookieless browser can never
        # obtain a gslide_session cookie. Fall back to the seeded demo user so
        # proxy.ts /status checks see `authenticated: true` and API-driven
        # interactions (settings, uploads, exports) work out of the box.
        # A real cookie/Bearer session still wins via the `user` branch above.
        demo = await resolve_demo_user(session)
        return {
            "configured": True,
            "authenticated": True,
            "username": demo.username,
            "user_id": str(demo.id),
        }
    configured = await _account_count(session) > 0
    return {
        "configured": configured,
        "authenticated": user is not None,
        "username": user.username if user else None,
        "user_id": str(user.id) if user else None,
    }


@API_V1_AUTH_ROUTER.get("/verify")
async def verify_session(
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    if is_disable_auth_enabled():
        return {
            "authenticated": True,
            "username": "local",
            "method": "local",
        }
    principal, user = await resolve_request_principal(request, session)
    if principal is None or user is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    original_uri = request.headers.get("x-original-uri")
    if original_uri and not is_app_data_path_authorized(
        original_uri,
        user_id=principal.user_id,
    ):
        raise HTTPException(status_code=403, detail="Asset access denied")
    return {
        "authenticated": True,
        **serialize_user(user),
        "method": principal.method,
    }


@API_V1_AUTH_ROUTER.post("/login", status_code=410)
async def login():
    raise HTTPException(
        status_code=410,
        detail="Login is no longer supported; gslide now ships as a miniweb inside the parent Gapo app.",
    )


@API_V1_AUTH_ROUTER.post("/register", status_code=410)
async def register():
    raise HTTPException(
        status_code=410,
        detail="Registration is no longer supported; gslide now ships as a miniweb inside the parent Gapo app.",
    )


@API_V1_AUTH_ROUTER.post("/logout", status_code=410)
async def logout():
    raise HTTPException(
        status_code=410,
        detail="Logout is no longer supported; gslide now ships as a miniweb inside the parent Gapo app.",
    )
