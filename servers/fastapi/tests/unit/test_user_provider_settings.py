import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.config import SESSION_COOKIE_NAME
from api.v1.auth.router import API_V1_AUTH_ROUTER
from api.v1.auth.users import PASSWORD_HELPER, get_jwt_strategy
from api.v1.settings.router import API_V1_SETTINGS_ROUTER
from models.sql.access_token import AccessToken
from models.sql.provider_settings import ProviderSettings
from models.sql.user import User
from models.sql.user_provider_settings import UserProviderSettings
from services.database import get_async_session
from utils.provider_overlay import (
    reset_provider_overlay,
    set_provider_overlay,
)
from utils.get_env import get_openai_api_key_env


def _build_client(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def create_user_table():
        async with engine.begin() as connection:
            await connection.run_sync(User.__table__.create)
            await connection.run_sync(AccessToken.__table__.create)
            await connection.run_sync(ProviderSettings.__table__.create)
            await connection.run_sync(UserProviderSettings.__table__.create)

    asyncio.run(create_user_table())

    async def override_session():
        async with session_maker() as session:
            yield session

    app = FastAPI()
    app.include_router(API_V1_AUTH_ROUTER)
    app.include_router(API_V1_SETTINGS_ROUTER)
    app.dependency_overrides[get_async_session] = override_session
    return TestClient(app), engine


async def _seed_user_with_session(engine, username, password="secret123"):
    """Insert a User directly and mint a JWT session token, returning (user_id, token).

    Replaces the old `POST /api/v1/auth/register` seeding path (now 410 Gone) so
    provider-settings isolation tests can still drive the cookie-authenticated
    settings router without exercising the removed register/login endpoints.
    The returned token is set on the TestClient as the `gslide_session` cookie,
    which is what `read_settings_user` consumes when a real session is present.
    """
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        user = User(
            username=username,
            hashed_password=PASSWORD_HELPER.hash(password),
            is_active=True,
            is_verified=True,
            auth_version=1,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        token = await get_jwt_strategy().write_token(user)
    return user.id, token


def _seed_and_authenticate(client, engine, username, password="secret123"):
    """Seed a user via DB and load its JWT session cookie onto ``client``."""
    _, token = asyncio.run(_seed_user_with_session(engine, username, password))
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return token


def test_two_overlays_do_not_leak(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    token_a = set_provider_overlay({"OPENAI_API_KEY": "sk-a"})
    try:
        assert get_openai_api_key_env() == "sk-a"
    finally:
        reset_provider_overlay(token_a)
    token_b = set_provider_overlay({})
    try:
        assert get_openai_api_key_env() == "sk-env"
    finally:
        reset_provider_overlay(token_b)


def test_settings_without_cookie_returns_401(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)

    response = client.get("/api/v1/settings/provider")
    assert response.status_code == 401

    asyncio.run(engine.dispose())


def test_user_overlay_isolated_per_user(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    _seed_and_authenticate(client, engine, "alice")

    put = client.put(
        "/api/v1/settings/provider",
        json={"LLM": "openai", "OPENAI_API_KEY": "sk-alice"},
    )
    assert put.status_code == 200
    assert put.json()["OPENAI_API_KEY"] == "sk-alice"

    bob = TestClient(client.app)
    _seed_and_authenticate(bob, engine, "bob")

    bob_get = bob.get("/api/v1/settings/provider")
    assert bob_get.status_code == 200
    assert bob_get.json().get("OPENAI_API_KEY") in (None, "")

    asyncio.run(engine.dispose())


def test_user_overlay_does_not_include_auth_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    _seed_and_authenticate(client, engine, "alice")

    response = client.put(
        "/api/v1/settings/provider",
        json={
            "LLM": "openai",
            "OPENAI_API_KEY": "sk-alice",
            "AUTH_USERNAME": "ignored",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert "AUTH_USERNAME" not in payload

    asyncio.run(engine.dispose())

def test_disable_auth_settings_work_without_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.setenv("DISABLE_AUTH", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")

    client, engine = _build_client(tmp_path)
    response = client.get("/api/v1/settings/provider")
    assert response.status_code == 200

    saved = client.put(
        "/api/v1/settings/provider",
        json={"LLM": "openai", "OPENAI_API_KEY": "sk-local"},
    )
    assert saved.status_code == 200
    assert saved.json()["OPENAI_API_KEY"] == "sk-local"
    assert get_openai_api_key_env() == "sk-local"

    asyncio.run(engine.dispose())
