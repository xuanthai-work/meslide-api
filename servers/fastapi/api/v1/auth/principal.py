from dataclasses import dataclass
from typing import Literal
import uuid

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.auth.demo_user import resolve_demo_user
from api.v1.auth.users import UsernameUserDatabase, UserManager, get_jwt_strategy
from models.sql.access_token import AccessToken
from models.sql.user import User
from api.v1.auth.config import is_accepted_api_key, read_session_token


@dataclass(frozen=True)
class AuthPrincipal:
    user_id: uuid.UUID
    username: str
    method: Literal["jwt", "api_key", "default"]


async def resolve_request_principal(
    request: Request, session: AsyncSession
) -> tuple[AuthPrincipal | None, User | None]:
    cookie_token = read_session_token(request.cookies)
    if cookie_token:
        user_db = UsernameUserDatabase(session)
        user = await get_jwt_strategy().read_token(cookie_token, UserManager(user_db))
        if user:
            return (
                AuthPrincipal(
                    user_id=user.id,
                    username=user.username,
                    method="jwt",
                ),
                user,
            )

    authorization = request.headers.get("Authorization", "")
    if authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        if not is_accepted_api_key(token):
            return None, None
        access_token = await session.get(AccessToken, token)
        if access_token is None:
            return None, None
        user = await session.get(User, access_token.user_id)
        if user is None or not user.is_active:
            return None, None
        return (
            AuthPrincipal(
                user_id=user.id,
                username=user.username,
                method="api_key",
            ),
            user,
        )

    # Future parent Gapo app handshake inserts ABOVE this point.

    # Default-on fallback: the demo user is auto-seeded by bootstrap_database_user
    # in lifespan. resolve_demo_user is idempotent so this is safe to call per request.
    demo_user = await resolve_demo_user(session)
    return (
        AuthPrincipal(
            user_id=demo_user.id,
            username=demo_user.username,
            method="default",
        ),
        demo_user,
    )


def principal_from_request(request: Request) -> AuthPrincipal:
    principal = getattr(request.state, "auth_principal", None)
    if principal is None:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return principal
