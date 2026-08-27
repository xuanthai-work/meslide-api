import asyncio
import os
import tempfile

# Mirror the Task 1/2 test setup: importing `api.v1.auth.bootstrap` transitively
# imports `services.database`, which at import time calls `_ensure_sqlite_parent_dir`
# on a path derived from `get_app_data_directory_env()`. On Windows the default
# `/tmp/gslide` is not writable, so set APP_DATA_DIRECTORY BEFORE importing.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.bootstrap import bootstrap_database_user
from api.v1.auth.demo_user import DEMO_USER_ID, DEMO_USERNAME
from models.sql.access_token import AccessToken
from models.sql.key_value import KeyValueSqlModel
from models.sql.presentation import PresentationModel
from models.sql.user import User


def _make_engine(database_path):
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}")


def _bootstrap_schema_sync(engine):
    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(AccessToken.__table__.create)
            await conn.run_sync(PresentationModel.__table__.create)
            await conn.run_sync(KeyValueSqlModel.__table__.create)

    asyncio.run(_create())


def test_bootstrap_creates_demo_user_when_db_empty(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path / "demo.db")
    _bootstrap_schema_sync(engine)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    # NOTE: The brief patched `services.database.async_session_maker`, but
    # `bootstrap.py` binds `async_session_maker` into its own namespace at
    # import time (`from services.database import async_session_maker`), so
    # patching the source module does not redirect bootstrap. Patch the
    # bootstrap module's binding directly, mirroring test_auth_bootstrap.py.
    from api.v1.auth import bootstrap as bootstrap_module
    monkeypatch.setattr(bootstrap_module, "async_session_maker", session_maker)
    monkeypatch.setattr(
        "api.v1.auth.bootstrap.persist_auth_credentials",
        lambda username, password_hash: None,
    )

    async def _skip_backfill(_session, _user):
        return None

    monkeypatch.setattr(bootstrap_module, "_backfill_legacy_ownership", _skip_backfill)
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    asyncio.run(bootstrap_database_user())

    async def _get_user():
        async with session_maker() as session:
            return await session.get(User, DEMO_USER_ID)

    user = asyncio.run(_get_user())
    assert user is not None
    assert user.username == DEMO_USERNAME


def test_bootstrap_idempotent_on_restart(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path / "demo.db")
    _bootstrap_schema_sync(engine)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    from api.v1.auth import bootstrap as bootstrap_module
    monkeypatch.setattr(bootstrap_module, "async_session_maker", session_maker)
    monkeypatch.setattr(
        "api.v1.auth.bootstrap.persist_auth_credentials",
        lambda username, password_hash: None,
    )

    async def _skip_backfill(_session, _user):
        return None

    monkeypatch.setattr(bootstrap_module, "_backfill_legacy_ownership", _skip_backfill)
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    asyncio.run(bootstrap_database_user())

    async def _read_hash():
        async with session_maker() as session:
            return (await session.get(User, DEMO_USER_ID)).hashed_password

    first_hash = asyncio.run(_read_hash())

    asyncio.run(bootstrap_database_user())

    async def _stats():
        async with session_maker() as session:
            user_count = await session.scalar(
                select(func.count()).select_from(User)
            )
            second_hash = (await session.get(User, DEMO_USER_ID)).hashed_password
            return user_count, second_hash

    user_count, second_hash = asyncio.run(_stats())

    assert user_count == 1
    assert second_hash == first_hash