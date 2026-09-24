import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.config import SESSION_COOKIE_NAME
from api.v1.auth.router import API_V1_AUTH_ROUTER
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

    Inserts a user directly. `POST /api/v1/auth/register` is not a route.
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


def test_public_registration_is_not_a_route(monkeypatch, tmp_path):
    """Public self-signup is absent from the auth router and cannot create a user.

    This client mounts only the auth router, so a missing path is 404.
    SessionAuthMiddleware on the full app can return 401 or 428 before the
    router runs, so that status is not evidence the route is gone.
    """
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    client, engine = _build_client(tmp_path)

    assert "/api/v1/auth/register" not in {
        getattr(route, "path", None) for route in API_V1_AUTH_ROUTER.routes
    }
    assert "/api/v1/auth/register" not in client.app.openapi()["paths"]

    response = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": "secret123"},
    )
    assert response.status_code == 404

    async def count_users() -> int:
        session_maker = async_sessionmaker(engine, expire_on_commit=False)
        async with session_maker() as session:
            return int(
                await session.scalar(select(func.count()).select_from(User)) or 0
            )

    assert asyncio.run(count_users()) == 0
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


def test_login_on_empty_db_is_unauthorized(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    client, engine = _build_client(tmp_path)
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "alice", "password": "secret123"},
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid username or password"
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
    asyncio.run(_seed_user_with_session(engine, "admin", "secret123"))
    client.cookies.clear()
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "ADMIN", "password": "secret123"},
    )

    assert response.status_code == 200
    assert response.json()["username"] == "admin"
    cookie = response.cookies.get(SESSION_COOKIE_NAME)
    assert cookie
    set_cookie = response.headers.get("set-cookie", "")
    assert "HttpOnly" in set_cookie or "httponly" in set_cookie.lower()

    status = client.get("/api/v1/auth/status")
    assert status.json()["authenticated"] is True
    assert status.json()["username"] == "admin"

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


def test_legacy_six_character_password_login_still_works(monkeypatch, tmp_path):
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

    assert response.status_code == 200
    assert response.json()["username"] == "legacy-user"
    asyncio.run(engine.dispose())


def test_logout_deletes_gslide_and_legacy_session_cookies(monkeypatch, tmp_path):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.delenv("DISABLE_AUTH", raising=False)
    client, engine = _build_client(tmp_path)
    asyncio.run(_seed_user_with_session(engine, "admin", "secret123"))
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "secret123"},
    )
    assert login.status_code == 200
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    set_cookie = response.headers.get("set-cookie", "").lower()
    assert SESSION_COOKIE_NAME in set_cookie
    assert "max-age=0" in set_cookie or "max-age=0" in set_cookie.replace(" ", "")
    asyncio.run(engine.dispose())
