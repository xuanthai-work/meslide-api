import asyncio
import os
import tempfile

# IMPORTANT: importing `api.v1.auth.router` transitively imports
# `services.database`, which at import time calls `_ensure_sqlite_parent_dir()`
# on a path derived from `get_app_data_directory_env()`. On Windows the default
# `/tmp/gslide` is not writable, so set APP_DATA_DIRECTORY BEFORE importing.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.demo_user import DEMO_USER_ID, DEMO_USERNAME
from api.v1.auth.router import API_V1_AUTH_ROUTER
from models.sql.access_token import AccessToken
from models.sql.provider_settings import ProviderSettings
from models.sql.user import User
from models.sql.user_provider_settings import UserProviderSettings
from services.database import get_async_session


def _build_client(tmp_path) -> tuple[TestClient, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def create_tables():
        async with engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
            await connection.run_sync(AccessToken.__table__.create)
            await connection.run_sync(ProviderSettings.__table__.create)
            await connection.run_sync(UserProviderSettings.__table__.create)

    asyncio.run(create_tables())

    async def override_session():
        async with session_maker() as session:
            yield session

    app = FastAPI()
    app.include_router(API_V1_AUTH_ROUTER)
    app.dependency_overrides[get_async_session] = override_session
    return TestClient(app), engine


def test_status_returns_demo_user_when_no_cookie_and_auth_enabled(
    monkeypatch, tmp_path
):
    """C1: /status must fall back to the demo user when no cookie is present
    and DISABLE_AUTH is unset. Without this, proxy.ts treats every API request
    as unauthenticated (401) and the default-on demo is functionally broken."""
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)

    client, engine = _build_client(tmp_path)
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["username"] == DEMO_USERNAME
    assert payload["user_id"] == str(DEMO_USER_ID)
    assert payload["user_id"] == "00000000-0000-0000-0000-000000000001"
    assert payload["configured"] is True

    asyncio.run(engine.dispose())


def test_status_cookie_user_takes_precedence_over_demo(monkeypatch, tmp_path):
    """A real cookie session still wins over the demo fallback."""
    from api.v1.auth.users import PASSWORD_HELPER, get_jwt_strategy
    from api.v1.auth.config import SESSION_COOKIE_NAME

    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)

    client, engine = _build_client(tmp_path)

    # Seed a real user and mint a JWT cookie.
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with session_maker() as session:
            user = User(
                username="alice",
                hashed_password=PASSWORD_HELPER.hash("secret123"),
                is_active=True,
                is_verified=True,
                auth_version=1,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            jwt = await get_jwt_strategy().write_token(user)
            return user, jwt

    user, jwt = asyncio.run(_seed())
    client.cookies.set(SESSION_COOKIE_NAME, jwt)

    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["authenticated"] is True
    assert payload["username"] == "alice"
    assert payload["user_id"] == str(user.id)

    asyncio.run(engine.dispose())