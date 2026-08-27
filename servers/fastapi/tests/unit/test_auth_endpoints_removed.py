import asyncio
import os
import tempfile

# Importing `api.v1.auth.router` transitively imports `services.database`, which
# at import time calls `_ensure_sqlite_parent_dir` on a path derived from
# `get_app_data_directory_env()`. On Windows the default `/tmp/gslide` may not
# be writable, so set APP_DATA_DIRECTORY BEFORE importing.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.router import API_V1_AUTH_ROUTER
from models.sql.access_token import AccessToken
from models.sql.provider_settings import ProviderSettings
from models.sql.user import User
from models.sql.user_provider_settings import UserProviderSettings


def _make_client(monkeypatch, tmp_path):
    # `read_user_from_cookie` (a dependency of /status) calls
    # `get_or_create_auth_secret`, which persists to USER_CONFIG_PATH; and the
    # status handler reports `configured: True` when DISABLE_AUTH is set, which
    # keeps the status test independent of seeding a user.
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.setenv("DISABLE_AUTH", "1")

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'auth.db'}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def create_tables():
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(AccessToken.__table__.create)
            await conn.run_sync(ProviderSettings.__table__.create)
            await conn.run_sync(UserProviderSettings.__table__.create)

    asyncio.run(create_tables())

    app = FastAPI()
    app.include_router(API_V1_AUTH_ROUTER)
    return TestClient(app)


@pytest.mark.parametrize(
    "path,body",
    [
        ("/api/v1/auth/login", {"username": "x", "password": "x12345678"}),
        ("/api/v1/auth/register", {"username": "x", "password": "x12345678"}),
        ("/api/v1/auth/logout", {}),
    ],
)
def test_removed_auth_endpoints_return_410(monkeypatch, tmp_path, path, body):
    client = _make_client(monkeypatch, tmp_path)
    response = client.post(path, json=body)
    assert response.status_code == 410
    assert "no longer supported" in response.json()["detail"]


def test_status_endpoint_still_works(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path)
    response = client.get("/api/v1/auth/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is True