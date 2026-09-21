import asyncio
import os
import tempfile

# IMPORTANT: importing `api.v1.auth.principal` transitively imports
# `services.database`, which at import time calls `_ensure_sqlite_parent_dir()`
# on a path derived from `get_app_data_directory_env()`. Set APP_DATA_DIRECTORY
# BEFORE importing so tests use an isolated writable location.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.principal import resolve_request_principal
from models.sql.access_token import AccessToken
from models.sql.user import User


def _make_engine(database_path):
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}")


def _bootstrap_schema(engine):
    async def _runner():
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(AccessToken.__table__.create)

    return _runner()


def test_resolve_request_principal_returns_none_when_no_cookie_no_bearer(tmp_path):
    engine = _make_engine(tmp_path / "auth.db")
    asyncio.run(_bootstrap_schema(engine))
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    class _StubRequest:
        cookies: dict = {}
        headers: dict = {}

    async def _resolve():
        async with session_maker() as session:
            return await resolve_request_principal(_StubRequest(), session)

    principal, user = asyncio.run(_resolve())
    assert principal is None
    assert user is None
