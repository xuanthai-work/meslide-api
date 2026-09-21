from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.auth.assets import is_app_data_path_authorized
from api.v1.auth.config import (
    LEGACY_SESSION_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    session_cookie_kwargs,
)
from api.v1.auth.principal import resolve_request_principal
from api.v1.auth.schemas import LoginCredentialsRequest
from api.v1.auth.users import (
    PASSWORD_HELPER,
    UsernameUserDatabase,
    get_jwt_strategy,
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
        configured = await _account_count(session) > 0
        return {
            "configured": configured,
            "authenticated": False,
            "username": None,
            "user_id": None,
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


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    response.delete_cookie(LEGACY_SESSION_COOKIE_NAME, path="/")


@API_V1_AUTH_ROUTER.post("/login")
async def login(
    credentials: LoginCredentialsRequest,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
):
    user_db = UsernameUserDatabase(session)
    user = await user_db.get_by_email(credentials.username)
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    verified, updated_hash = PASSWORD_HELPER.verify_and_update(
        credentials.password,
        user.hashed_password,
    )
    if not verified:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    if updated_hash:
        user.hashed_password = updated_hash
        session.add(user)
        await session.commit()
        await session.refresh(user)
    token = await get_jwt_strategy().write_token(user)
    response = JSONResponse(serialize_user(user))
    response.set_cookie(value=token, **session_cookie_kwargs(request))
    return response


@API_V1_AUTH_ROUTER.post("/register", status_code=410)
async def register():
    raise HTTPException(
        status_code=410,
        detail="Registration is no longer supported; gslide now ships as a miniweb inside the parent Gapo app.",
    )


@API_V1_AUTH_ROUTER.post("/logout")
async def logout():
    response = Response(status_code=204)
    _clear_session_cookies(response)
    return response
