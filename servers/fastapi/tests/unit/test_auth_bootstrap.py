import asyncio
import os
import tempfile

# Set APP_DATA_DIRECTORY BEFORE importing app modules so the SQLite parent
# dir lands in an isolated writable location. Mirrors test_default_user_principal.py.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

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


def test_bootstrap_does_not_require_demo_password_or_replace_users(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("USER_CONFIG_PATH", str(tmp_path / "userConfig.json"))
    monkeypatch.setenv("RESET_AUTH", "true")
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("AUTH_OVERRIDE_FROM_ENV", raising=False)

    async def runner():
        engine, session_maker = await _create_auth_database(
            tmp_path / "existing-user.db"
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

            await bootstrap.bootstrap_database_user()

            async with session_maker() as session:
                users = list(await session.scalars(select(User)))
            assert len(users) == 1
            assert users[0].id == original_id
            assert users[0].username == "user"
        finally:
            await engine.dispose()

    asyncio.run(runner())
