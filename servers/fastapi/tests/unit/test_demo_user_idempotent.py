import asyncio
import os
import tempfile

# Importing `api.v1.auth.bootstrap` transitively imports `services.database`.
# Set APP_DATA_DIRECTORY BEFORE importing so tests use an isolated writable location.
os.environ.setdefault("APP_DATA_DIRECTORY", tempfile.gettempdir())

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from api.v1.auth.bootstrap import bootstrap_database_user
from api.v1.auth.users import PASSWORD_HELPER
from models.sql.access_token import AccessToken
from models.sql.user import User


def _make_engine(database_path):
    return create_async_engine(f"sqlite+aiosqlite:///{database_path}")


def _bootstrap_schema_sync(engine):
    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(User.__table__.create)
            await conn.run_sync(AccessToken.__table__.create)

    asyncio.run(_create())


def test_bootstrap_does_not_create_a_user_when_db_empty(tmp_path, monkeypatch):
    engine = _make_engine(tmp_path / "auth.db")
    _bootstrap_schema_sync(engine)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.delenv("AUTH_USERNAME", raising=False)
    monkeypatch.delenv("AUTH_PASSWORD", raising=False)

    asyncio.run(bootstrap_database_user())

    async def _count():
        async with session_maker() as session:
            return await session.scalar(select(func.count()).select_from(User))

    assert asyncio.run(_count()) == 0


def test_bootstrap_leaves_existing_users_unchanged(tmp_path):
    engine = _make_engine(tmp_path / "auth.db")
    _bootstrap_schema_sync(engine)
    session_maker = async_sessionmaker(engine, expire_on_commit=False)

    async def _seed():
        async with session_maker() as session:
            user = User(
                username="user1",
                hashed_password=PASSWORD_HELPER.hash("keep-me-secret"),
                is_active=True,
                is_verified=True,
                auth_version=1,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user.id, user.hashed_password

    user_id, password_hash = asyncio.run(_seed())
    asyncio.run(bootstrap_database_user())

    async def _read():
        async with session_maker() as session:
            user = await session.get(User, user_id)
            count = await session.scalar(select(func.count()).select_from(User))
            return user, count

    user, count = asyncio.run(_read())
    assert count == 1
    assert user is not None
    assert user.username == "user1"
    assert user.hashed_password == password_hash
