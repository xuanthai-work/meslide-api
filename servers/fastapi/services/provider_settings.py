import logging
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import AsyncSession

from models.sql.provider_settings import ProviderSettings
from models.sql.user_provider_settings import UserProviderSettings
from utils.datetime_utils import get_current_utc_datetime
from utils.db_utils import get_database_url_and_connect_args, to_sync_sqlalchemy_url
from utils.get_env import get_user_config_path_env
from utils.user_config_store import read_user_config_file, update_user_config_file


logger = logging.getLogger(__name__)

PROVIDER_SETTINGS_ID = 1
_CLOUD_STATUS_FIELDS = {
    "PRESENTON_CONNECTED",
    "PRESENTON_EMAIL",
}

# Fields that are stored at the instance level, not per user. Saving these in
# an overlay would leak operator-only state into a user row.
_INSTANCE_LEVEL_FIELDS = {"DISABLE_ANONYMOUS_TRACKING"}


def sanitize_provider_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Keep provider/runtime settings and exclude legacy auth and Cloud status."""
    cleaned = {
        key: value
        for key, value in config.items()
        if not key.upper().startswith("AUTH_") and key not in _CLOUD_STATUS_FIELDS
    }
    if cleaned.get("LLM") == "presenton":
        cleaned.pop("LLM", None)
    return cleaned


def sanitize_user_overlay(config: dict[str, Any]) -> dict[str, Any]:
    """Per-user overlay cannot include AUTH_, Cloud status, or instance-level fields."""
    cleaned = sanitize_provider_settings(config)
    for key in _INSTANCE_LEVEL_FIELDS:
        cleaned.pop(key, None)
    return cleaned


def persist_instance_level_fields(incoming: dict[str, Any]) -> None:
    """Write Analytics/telemetry flags to instance userConfig.json, not the overlay."""
    path = get_user_config_path_env()
    if not path:
        return
    updates = {
        key: incoming[key]
        for key in _INSTANCE_LEVEL_FIELDS
        if key in incoming
    }
    if not updates:
        return

    def patch(existing: dict[str, Any]) -> dict[str, Any]:
        next_config = dict(existing)
        next_config.update(updates)
        return next_config

    update_user_config_file(path, patch)


def merge_provider_settings(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
    """Preserve the previous settings API's patch behavior."""
    return {
        **sanitize_provider_settings(existing),
        **sanitize_provider_settings(incoming),
    }


def fill_unset_from_runtime(config: dict[str, Any]) -> dict[str, Any]:
    """Fill blank DB/file provider fields from env (+ userConfig.json fallbacks)."""
    from utils.user_config import get_user_config

    filled = dict(sanitize_provider_settings(config))
    effective = sanitize_provider_settings(
        get_user_config().model_dump(exclude_none=True)
    )
    for key, value in effective.items():
        if filled.get(key) in (None, ""):
            filled[key] = value
    return filled


def _mirror_to_legacy_file(config: dict[str, Any]) -> None:
    """Mirror DB settings for code paths that still consume userConfig.json."""
    path = get_user_config_path_env()
    if not path:
        return

    db_config = sanitize_provider_settings(config)

    def replace_provider_config(existing: dict[str, Any]) -> dict[str, Any]:
        mirrored = dict(db_config)

        for key, value in fill_unset_from_runtime(mirrored).items():
            if mirrored.get(key) in (None, ""):
                mirrored[key] = value

        # Authentication is database-backed now, but the compatibility file is
        # also the rollback/recovery copy. Never discard its credential fields.
        for key, value in existing.items():
            if key.upper().startswith("AUTH_"):
                mirrored[key] = value
        return mirrored

    update_user_config_file(path, replace_provider_config)


async def migrate_provider_settings_from_file(session: AsyncSession) -> dict[str, Any]:
    """One-time startup import, followed by DB-to-file compatibility syncing."""
    row = await session.get(ProviderSettings, PROVIDER_SETTINGS_ID)
    path = get_user_config_path_env()

    if row is None:
        legacy_config = sanitize_provider_settings(
            read_user_config_file(path) if path else {}
        )
        row = ProviderSettings(
            id=PROVIDER_SETTINGS_ID,
            config=legacy_config,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        logger.info("Migrated provider settings from userConfig.json into the database.")
    else:
        sanitized = sanitize_provider_settings(dict(row.config or {}))
        if sanitized != row.config:
            row.config = sanitized
            row.updated_at = get_current_utc_datetime()
            await session.commit()

    config = fill_unset_from_runtime(dict(row.config or {}))
    _mirror_to_legacy_file(config)
    return config


async def get_provider_settings(session: AsyncSession) -> dict[str, Any]:
    row = await session.get(ProviderSettings, PROVIDER_SETTINGS_ID)
    if row is None:
        return await migrate_provider_settings_from_file(session)
    return fill_unset_from_runtime(dict(row.config or {}))


async def save_provider_settings(
    session: AsyncSession, incoming: dict[str, Any]
) -> dict[str, Any]:
    row = await session.get(ProviderSettings, PROVIDER_SETTINGS_ID)
    if row is None:
        existing: dict[str, Any] = {}
        row = ProviderSettings(id=PROVIDER_SETTINGS_ID, config={})
        session.add(row)
    else:
        existing = dict(row.config or {})

    row.config = merge_provider_settings(existing, incoming)
    row.updated_at = get_current_utc_datetime()
    await session.commit()
    await session.refresh(row)

    config = dict(row.config or {})
    _mirror_to_legacy_file(config)
    return config


def sync_legacy_file_to_provider_settings() -> None:
    """Persist changes made by remaining synchronous userConfig writers."""
    path = get_user_config_path_env()
    if not path:
        return
    config = sanitize_provider_settings(read_user_config_file(path))
    database_url, _ = get_database_url_and_connect_args()
    engine = create_engine(to_sync_sqlalchemy_url(database_url))
    try:
        with engine.begin() as connection:
            table = ProviderSettings.__table__
            exists = connection.execute(
                select(table.c.id).where(table.c.id == PROVIDER_SETTINGS_ID)
            ).scalar_one_or_none()
            values = {
                "config": config,
                "updated_at": get_current_utc_datetime(),
            }
            if exists is None:
                connection.execute(
                    table.insert().values(id=PROVIDER_SETTINGS_ID, **values)
                )
            else:
                connection.execute(
                    table.update()
                    .where(table.c.id == PROVIDER_SETTINGS_ID)
                    .values(**values)
                )
    finally:
        engine.dispose()


async def get_user_provider_overlay(
    session: AsyncSession, user_id: Any
) -> dict[str, Any]:
    """Effective provider config for a user: overlay merged with process env.

    The result is sanitized so callers never see another user's keys or
    instance-level fields the user cannot edit.
    """
    row = await session.get(UserProviderSettings, user_id)
    overlay = dict(row.config) if row is not None and row.config else {}
    return fill_unset_from_runtime(sanitize_user_overlay(overlay))


async def save_user_provider_overlay(
    session: AsyncSession, user_id: Any, incoming: dict[str, Any]
) -> dict[str, Any]:
    """Merge a sanitized overlay for one user and return the new effective config.

    Partial bodies (Analytics-only POSTs) must not wipe keys already saved in
    the overlay. Incoming overlay fields overwrite matching keys; omitted keys
    stay. Instance-level fields are stripped before persist.
    """
    sanitized = sanitize_user_overlay(incoming or {})
    row = await session.get(UserProviderSettings, user_id)
    existing = sanitize_user_overlay(dict(row.config or {})) if row is not None else {}
    merged = {**existing, **sanitized}
    if row is None:
        row = UserProviderSettings(user_id=user_id, config=merged)
        session.add(row)
    else:
        row.config = merged
    row.updated_at = get_current_utc_datetime()
    await session.commit()
    await session.refresh(row)
    return fill_unset_from_runtime(dict(row.config or {}))
