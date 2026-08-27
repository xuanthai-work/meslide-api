import logging
import os

from sqlalchemy import delete, func, select, update

from api.v1.auth.users import PASSWORD_HELPER
from models.sql.access_token import AccessToken
from models.sql.user import User
from models.sql.async_task import AsyncTaskModel
from models.sql.async_presentation_generation_status import (
    AsyncPresentationGenerationTaskModel,
)
from models.sql.chat_history_message import ChatHistoryMessageModel
from models.sql.image_asset import ImageAsset
from models.sql.key_value import KeyValueSqlModel
from models.sql.presentation import PresentationModel
from models.sql.presentation_layout_code import PresentationLayoutCodeModel
from models.sql.slide import SlideModel
from models.sql.template import TemplateModel
from models.sql.template_create_info import TemplateCreateInfoModel
from models.sql.template_v2 import TemplateV2
from models.sql.webhook_subscription import WebhookSubscription
from services.database import async_session_maker
from api.v1.auth.config import (
    get_legacy_admin_credentials,
    persist_auth_credentials,
)


logger = logging.getLogger(__name__)


def _truthy(value: str | None) -> bool:
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def _validate_new_environment_password(password: str | None) -> None:
    if password is not None and len(password) < 8:
        raise RuntimeError("AUTH_PASSWORD must be at least 8 characters")


def _validate_new_environment_username(username: str) -> None:
    if username and len(username) < 3:
        raise RuntimeError("AUTH_USERNAME must be at least 3 characters")


async def _find_bootstrap_user(session) -> User | None:
    """Find the user that the legacy/admin bootstrap should recover.

    The migration looks up by ``AUTH_USERNAME`` from the recovery file/env,
    falling back to the first user the database has if any exist.
    """
    env_username = (os.getenv("AUTH_USERNAME") or "").strip()
    if env_username:
        existing = await session.scalar(
            select(User).where(func.lower(User.username) == env_username.casefold())
        )
        if existing is not None:
            return existing
    # No env hint, but if the instance already has an account, take the first
    # one so the bootstrap always operates on a real user row.
    return await session.scalar(select(User).order_by(User.created_at.asc()).limit(1))


async def bootstrap_database_user() -> None:
    """Upsert the demo user. Idempotent across restarts.

    Honors RESET_AUTH only when DEMO_AUTH_FROM_ENV=true (escape hatch for
    operators who want a non-default username/password sourced from env).
    """
    from api.v1.auth.demo_user import (
        DEMO_USER_ID,
        DEMO_USERNAME,
        resolve_demo_user,
    )

    env_from_file = _truthy(os.getenv("DEMO_AUTH_FROM_ENV"))
    env_username = (os.getenv("DEMO_USERNAME") or "").strip() or DEMO_USERNAME
    env_password = os.getenv("DEMO_PASSWORD")
    reset_requested = _truthy(os.getenv("RESET_AUTH"))
    if env_from_file:
        _validate_new_environment_username(env_username)
        if env_password is not None:
            _validate_new_environment_password(env_password)
        if (reset_requested or env_password is None) and not env_password:
            raise RuntimeError(
                "DEMO_AUTH_FROM_ENV with RESET_AUTH requires DEMO_PASSWORD"
            )

    async with async_session_maker() as session:
        existing = await session.get(User, DEMO_USER_ID)

        if existing is None:
            user = await resolve_demo_user(session)
            password_hash = user.hashed_password
        else:
            user = existing
            password_hash = (
                PASSWORD_HELPER.hash(env_password)
                if env_from_file and env_password is not None
                else user.hashed_password
            )
            if env_from_file and env_password is not None:
                user.hashed_password = password_hash
                user.username = env_username
                await session.commit()
                await session.refresh(user)

        if existing is None:
            await _backfill_legacy_ownership(session, user)
            persist_auth_credentials(user.username, password_hash)
            logger.info(
                "Migrated the demo user into the user database (backfill complete)."
            )


async def _backfill_legacy_ownership(session, user: User) -> None:
    owned_models = (
        PresentationModel,
        SlideModel,
        PresentationLayoutCodeModel,
        TemplateModel,
        AsyncTaskModel,
        AsyncPresentationGenerationTaskModel,
        ChatHistoryMessageModel,
        ImageAsset,
        TemplateCreateInfoModel,
        WebhookSubscription,
    )
    for model in owned_models:
        await session.execute(
            update(model)
            .where(model.owner_id.is_(None))
            .values(owner_id=user.id)
        )
    # Built-in templates intentionally remain shared; only custom templates
    # migrate into the bootstrap user's private workspace.
    await session.execute(
        update(TemplateV2)
        .where(TemplateV2.owner_id.is_(None), TemplateV2.is_default.is_(False))
        .values(owner_id=user.id)
    )
    await session.execute(
        update(KeyValueSqlModel)
        .where(KeyValueSqlModel.key == "presentation_custom_themes")
        .values(key=f"presentation_custom_themes:{user.id}")
    )
    await session.commit()
