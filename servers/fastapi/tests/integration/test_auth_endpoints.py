import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.config import SESSION_COOKIE_NAME
from api.v1.auth.router import API_V1_AUTH_ROUTER
from api.v1.auth.rate_limit import LOGIN_RATE_LIMITER, login_rate_limit_key
from api.v1.auth.users import PASSWORD_HELPER, get_jwt_strategy
from models.sql.access_token import AccessToken
from models.sql.provider_settings import ProviderSettings
from models.sql.user import User
from models.sql.user_provider_settings import UserProviderSettings
from services.database import get_async_session


def _build_client(tmp_path) -> tuple[TestClient, object]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
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
    app.dependency_overrides[get_async_session] = override_session
    return TestClient(app), engine


async def _seed_user_with_session(engine, username, password="secret123"):
    """Insert a User directly and mint a JWT session token, returning (user, jwt).

    Replaces the old `POST /api/v1/auth/register` seeding path (now 410 Gone).
    The JWT is set as the `gslide_session` cookie on the TestClient by the caller
    so cookie-authenticated endpoints (e.g. `token/create`) work without register.
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
        jwt = await get_jwt_strategy().write_token(user)
    return user, jwt


async def _seed_access_token(engine, token, user_id):
    """Insert an AccessToken row directly (used to restore /verify Bearer coverage)."""
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with session_maker() as session:
        session.add(AccessToken(token=token, user_id=user_id))
        await session.commit()


def _seed_user(client: TestClient, engine, username: str, password: str) -> None:
    client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password},
    )


def test_register_allowed_when_instance_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    # Registration is removed (410 Gone); gslide ships as a miniweb inside Gapo.
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]
    asyncio.run(engine.dispose())


def test_setup_endpoint_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    client, engine = _build_client(tmp_path)
    response = client.post(
        "/api/v1/auth/setup",
        json={"username": "alice", "password": "secret123"},
    )
    assert response.status_code == 404
    asyncio.run(engine.dispose())


def test_login_on_empty_db_is_428_without_setup_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    client, engine = _build_client(tmp_path)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "secret123"},
    )
    # Login is removed (410 Gone); the previous 428 "no accounts yet" path is gone.
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]
    asyncio.run(engine.dispose())


def test_admin_router_gone(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    client, engine = _build_client(tmp_path)
    assert client.get("/api/v1/admin/users").status_code == 404
    asyncio.run(engine.dispose())


def test_login_sets_http_only_jwt_cookie_for_username_only_account(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)

    client, engine = _build_client(tmp_path)
    register = client.post(
        "/api/v1/auth/register",
        json={"username": "admin", "password": "secret123"},
    )
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "ADMIN", "password": "secret123"},
    )

    # Both register and login are removed (410 Gone); cookie side-effects are gone.
    assert register.status_code == 410
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]

    asyncio.run(engine.dispose())


def test_access_key_uses_current_users_session(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)

    # Seed alice directly via DB and load a JWT session cookie so the
    # cookie-authenticated `token/create` endpoint works without the removed
    # register/login endpoints.
    _, jwt = asyncio.run(_seed_user_with_session(engine, "alice"))
    client.cookies.set(SESSION_COOKIE_NAME, jwt)

    token_response = client.post("/api/v1/auth/token/create")
    assert token_response.status_code == 200
    token = token_response.json()["token"]
    assert token.startswith("sk-gslide-")
    client.cookies.clear()

    response = client.get(
        "/api/v1/auth/verify",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "api_key"
    assert body["username"] == "alice"
    assert "role" not in body

    asyncio.run(engine.dispose())


def test_legacy_presenton_api_key_still_verifies(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)

    # Seed admin directly via DB, then insert the legacy `sk-presenton-*` API
    # key directly (register/login are removed, so the old seed-via-login path
    # no longer applies). /verify itself is unchanged and still accepts the
    # legacy `sk-presenton-*` Bearer token via resolve_request_principal.
    admin, _ = asyncio.run(_seed_user_with_session(engine, "admin"))
    asyncio.run(
        _seed_access_token(engine, "sk-presenton-legacyfixture", admin.id)
    )
    client.cookies.clear()
    response = client.get(
        "/api/v1/auth/verify",
        headers={"Authorization": "Bearer sk-presenton-legacyfixture"},
    )
    assert response.status_code == 200
    assert response.json()["method"] == "api_key"
    assert "role" not in response.json()
    asyncio.run(engine.dispose())


def test_legacy_six_character_password_login_is_removed(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)

    async def seed_legacy_user():
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session:
            session.add(
                User(
                    username="legacy-user",
                    hashed_password=PASSWORD_HELPER.hash("123456"),
                    is_active=True,
                    is_verified=True,
                )
            )
            await session.commit()

    asyncio.run(seed_legacy_user())
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "legacy-user", "password": "123456"},
    )

    # Login is removed (410 Gone) even for legacy seeded users.
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]
    asyncio.run(engine.dispose())


@pytest.mark.skip(
    reason="out-of-scope: login/register/logout removed (410); rate-limiting "
    "the removed login flow is no longer meaningful"
)
def test_failed_logins_are_rate_limited(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    _seed_user(client, engine, "rate-user", "secret123")
    key = login_rate_limit_key("testclient", "rate-user")
    asyncio.run(LOGIN_RATE_LIMITER.clear(key))

    try:
        for _ in range(5):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "rate-user", "password": "wrong-password"},
            )
            assert response.status_code == 401

        blocked = client.post(
            "/api/v1/auth/login",
            json={"username": "rate-user", "password": "wrong-password"},
        )
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0
    finally:
        asyncio.run(LOGIN_RATE_LIMITER.clear(key))
        asyncio.run(engine.dispose())


def test_register_creates_normal_user_and_sets_cookie(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)

    setup = client.post(
        "/api/v1/auth/register",
        json={"username": "first-user", "password": "secret123"},
    )
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    # Register is removed (410 Gone); cookie/created-user side-effects are gone.
    assert setup.status_code == 410
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]

    asyncio.run(engine.dispose())


@pytest.mark.skip(
    reason="out-of-scope: register removed (410); duplicate-username conflict "
    "detection on the removed register flow is no longer meaningful"
)
def test_register_conflict_on_duplicate_username(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    _seed_user(client, engine, "admin", "secret123")
    first = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    client.cookies.clear()
    second = client.post(
        "/api/v1/auth/register",
        json={"username": "ALICE", "password": "otherpass1"},
    )
    assert first.status_code == 201
    assert second.status_code == 409
    asyncio.run(engine.dispose())


def test_logout_deletes_gslide_and_legacy_session_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    _seed_user(client, engine, "admin", "secret123")
    client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "secret123"},
    )
    response = client.post("/api/v1/auth/logout")
    # Logout is removed (410 Gone); cookie-deletion side-effects are gone.
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]
    asyncio.run(engine.dispose())