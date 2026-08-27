import asyncio
import json
import os
import stat
import tempfile

# On Windows the default app-data path resolves to a non-writable `//tmp/gslide`
# location; set APP_DATA_DIRECTORY BEFORE importing app modules so the SQLite
# parent dir can be created. Mirrors test_default_user_principal.py.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth import bootstrap
from api.v1.auth.users import PASSWORD_HELPER
from models.sql.access_token import AccessToken
from models.sql.user import User


async def _create_auth_database(database_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(AccessToken.__table__.create)
    return engine, session_maker


@pytest.mark.skip(
    reason="out-of-scope: tests removed RESET_AUTH token-deletion / "
    "auth_version-bump / secret-rotation flow on an arbitrary user; "
    "rewrite under the reset/override feature-removal follow-up"
)
def test_reset_auth_recovers_user_without_replacing_account(
    monkeypatch, tmp_path
):
    config_path = tmp_path / "userConfig.json"
    config_path.write_text(
        json.dumps(
            {
                "AUTH_USERNAME": "old-user",
                "AUTH_PASSWORD_HASH": "old-hash",
                "AUTH_SECRET_KEY": "old-secret",
                "LLM_PROVIDER": "openai",
            }
        )
    )
    monkeypatch.setenv("USER_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("RESET_AUTH", "true")
    monkeypatch.setenv("AUTH_USERNAME", "recovered-user")
    monkeypatch.setenv("AUTH_PASSWORD", "new-secret-123")
    monkeypatch.delenv("AUTH_OVERRIDE_FROM_ENV", raising=False)

    async def runner():
        engine, session_maker = await _create_auth_database(tmp_path / "auth.db")
        original_id = None
        try:
            async with session_maker() as session:
                user = User(
                    username="old-user",
                    hashed_password=PASSWORD_HELPER.hash("old-secret-123"),
                    is_active=True,
                    is_verified=True,
                    auth_version=4,
                )
                session.add(user)
                await session.flush()
                original_id = user.id
                session.add(AccessToken(token="sk-test-old", user_id=user.id))
                await session.commit()

            async def skip_ownership_backfill(_session, _user):
                return None

            monkeypatch.setattr(bootstrap, "async_session_maker", session_maker)
            monkeypatch.setattr(
                bootstrap,
                "_backfill_legacy_ownership",
                skip_ownership_backfill,
            )
            await bootstrap.bootstrap_database_user()

            async with session_maker() as session:
                recovered = await session.scalar(select(User))
                tokens = list(await session.scalars(select(AccessToken)))

            assert recovered is not None
            assert recovered.id == original_id
            assert recovered.username == "recovered-user"
            assert recovered.auth_version == 5
            assert not hasattr(recovered, "is_superuser") or getattr(
                recovered, "is_superuser", False
            ) is False
            verified, _ = PASSWORD_HELPER.verify_and_update(
                "new-secret-123",
                recovered.hashed_password,
            )
            assert verified is True
            assert tokens == []
        finally:
            await engine.dispose()

    asyncio.run(runner())

    config = json.loads(config_path.read_text())
    assert config["AUTH_USERNAME"] == "recovered-user"
    assert config["AUTH_PASSWORD_HASH"] != "old-hash"
    assert config["AUTH_SECRET_KEY"] != "old-secret"
    assert config["LLM_PROVIDER"] == "openai"
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "userConfig.json.bak").stat().st_mode) == 0o600


def test_reset_auth_without_password_refuses_to_delete_or_replace_user(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.setenv("RESET_AUTH", "true")
    monkeypatch.setenv("DEMO_AUTH_FROM_ENV", "true")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_OVERRIDE_FROM_ENV", raising=False)

    async def runner():
        engine, session_maker = await _create_auth_database(
            tmp_path / "missing-password.db"
        )
        try:
            async with session_maker() as session:
                user = User(
                    username="user",
                    hashed_password=PASSWORD_HELPER.hash("old-secret-123"),
                    is_active=True,
                    is_verified=True,
                    auth_version=1,
                )
                session.add(user)
                await session.commit()
                original_id = user.id

            monkeypatch.setattr(bootstrap, "async_session_maker", session_maker)
            with pytest.raises(RuntimeError, match="requires DEMO_PASSWORD"):
                await bootstrap.bootstrap_database_user()

            async with session_maker() as session:
                users = list(await session.scalars(select(User)))
            assert len(users) == 1
            assert users[0].id == original_id
            assert users[0].username == "user"
        finally:
            await engine.dispose()

    asyncio.run(runner())
