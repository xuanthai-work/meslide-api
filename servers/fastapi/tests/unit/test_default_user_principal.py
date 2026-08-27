import asyncio
import os
import tempfile
import uuid

# IMPORTANT: `api.v1.auth.demo_user` -> `api.v1.auth.users` transitively imports
# `services.database`, which at import time calls `_ensure_sqlite_parent_dir()`
# on `os.path.join(get_app_data_directory_env() or "/tmp/gslide", "fastapi.db")`.
# On Windows that default becomes `//tmp/gslide/` and `os.makedirs` raises
# `FileNotFoundError: [WinError 53]`. Set APP_DATA_DIRECTORY BEFORE importing
# anything from the auth package so the path resolves to a writable temp dir.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.demo_user import (
    DEMO_USER_ID,
    DEMO_USERNAME,
    get_demo_principal,
    resolve_demo_user,
)
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


_run = asyncio.run


def test_demo_user_constants_are_locked():
    assert DEMO_USER_ID == uuid.UUID("00000000-0000-0000-0000-000000000001")
    assert DEMO_USERNAME == "demo"


def test_resolve_demo_user_seeds_when_missing(tmp_path):
    engine = _make_engine(tmp_path / "demo.db")
    _run(_bootstrap_schema(engine))
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _runner():
        async with session_maker() as session:
            return await resolve_demo_user(session)

    user = asyncio.run(_runner())
    assert user.id == DEMO_USER_ID
    assert user.username == DEMO_USERNAME
    assert user.is_active is True
    assert user.auth_version == 1
    assert user.hashed_password  # non-empty


def test_resolve_demo_user_is_idempotent(tmp_path):
    engine = _make_engine(tmp_path / "demo.db")
    _run(_bootstrap_schema(engine))
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _runner():
        async with session_maker() as session:
            first = await resolve_demo_user(session)
            first_hash = first.hashed_password
        async with session_maker() as session:
            second = await resolve_demo_user(session)
        return first, first_hash, second

    first, first_hash, second = asyncio.run(_runner())
    assert second.id == first.id
    assert second.hashed_password == first_hash  # not re-randomized


def test_get_demo_principal_shape():
    principal = get_demo_principal()
    assert principal.user_id == DEMO_USER_ID
    assert principal.username == DEMO_USERNAME
    assert principal.method == "default"


from api.v1.auth.principal import resolve_request_principal


def test_resolve_request_principal_returns_demo_when_no_cookie_no_bearer(tmp_path):
    engine = _make_engine(tmp_path / "demo.db")
    _run(_bootstrap_schema(engine))
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with session_maker() as session:
            await resolve_demo_user(session)

    _run(_seed())

    class _StubRequest:
        cookies: dict = {}
        headers: dict = {}

    async def _resolve():
        async with session_maker() as session:
            return await resolve_request_principal(_StubRequest(), session)

    principal, user = _run(_resolve())
    assert principal is not None
    assert principal.user_id == DEMO_USER_ID
    assert principal.method == "default"
    assert user is not None
    assert user.username == DEMO_USERNAME


def test_resolve_request_principal_demo_seeds_when_db_empty(tmp_path):
    engine = _make_engine(tmp_path / "demo.db")
    _run(_bootstrap_schema(engine))
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    class _StubRequest:
        cookies: dict = {}
        headers: dict = {}

    async def _resolve():
        async with session_maker() as session:
            return await resolve_request_principal(_StubRequest(), session)

    principal, user = _run(_resolve())
    assert principal is not None
    assert user is not None
    assert user.username == DEMO_USERNAME
