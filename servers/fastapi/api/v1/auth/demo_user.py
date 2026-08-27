import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.v1.auth.users import PASSWORD_HELPER
from models.sql.user import User


logger = logging.getLogger(__name__)


DEMO_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
DEMO_USERNAME = "demo"


@dataclass(frozen=True)
class DemoPrincipal:
    user_id: uuid.UUID
    username: str
    method: Literal["default"]


_DEMO_PRINCIPAL = DemoPrincipal(
    user_id=DEMO_USER_ID,
    username=DEMO_USERNAME,
    method="default",
)


def get_demo_principal() -> DemoPrincipal:
    return _DEMO_PRINCIPAL


def _new_random_password() -> str:
    # 32-byte URL-safe token; logged once on first create. Operators can paste it
    # into their curl scripts if they need Bearer-over-password later. For the
    # default-on demo path this is never used by the UI.
    return secrets.token_urlsafe(32)


async def resolve_demo_user(session: AsyncSession) -> User:
    """Return the demo user, creating it on first call.

    Idempotent: a second call returns the same row with the same hashed_password.
    The fixed UUID guarantees no duplicate inserts across restarts.
    """
    existing = await session.get(User, DEMO_USER_ID)
    if existing is not None:
        return existing

    user = User(
        id=DEMO_USER_ID,
        username=DEMO_USERNAME,
        hashed_password=PASSWORD_HELPER.hash(_new_random_password()),
        is_active=True,
        is_verified=True,
        auth_version=1,
    )
    session.add(user)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        # Race: another worker inserted between get() and commit. Re-fetch.
        existing = await session.get(User, DEMO_USER_ID)
        if existing is None:
            raise
        return existing
    await session.refresh(user)
    logger.info(
        "[demo-user] auto-created (id=%s, username=%s)",
        DEMO_USER_ID,
        DEMO_USERNAME,
    )
    return user
