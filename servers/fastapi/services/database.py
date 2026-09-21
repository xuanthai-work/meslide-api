from collections.abc import AsyncGenerator
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
    AsyncSession,
)
from sqlalchemy import event, or_
from sqlalchemy.orm import Session, with_loader_criteria
from sqlmodel import SQLModel

from models.sql.async_task import AsyncTaskModel
from models.sql.async_presentation_generation_status import (
    AsyncPresentationGenerationTaskModel,
)
from models.sql.chat_history_message import ChatHistoryMessageModel
from models.sql.font_upload import FontUpload
from models.sql.image_asset import ImageAsset
from models.sql.key_value import KeyValueSqlModel
from models.sql.presentation_layout_code import PresentationLayoutCodeModel
from models.sql.presentation import PresentationModel
from models.sql.template import TemplateModel
from models.sql.template_create_info import TemplateCreateInfoModel
from models.sql.template_v2 import TemplateV2
from models.sql.slide import SlideModel
from models.sql.webhook_subscription import WebhookSubscription
from models.sql.user import User
from models.sql.access_token import AccessToken
from models.sql.provider_settings import ProviderSettings
from api.v1.auth.context import get_current_owner_id_raw
from utils.get_env import get_migrate_database_on_startup_env
from utils.db_utils import get_database_url_and_connect_args, get_pool_kwargs


_engine_instance: AsyncEngine | None = None
_session_maker_instance: async_sessionmaker | None = None


def get_sql_engine() -> AsyncEngine:
    """Return the process-wide async engine, creating it on first call.

    Building the engine resolves the database URL, which creates the SQLite
    parent directory, so it must not happen at import time: importing this
    module has to stay free of filesystem side effects, and code paths that
    never touch the database must not pay for engine construction.
    """
    global _engine_instance
    if _engine_instance is None:
        database_url, connect_args = get_database_url_and_connect_args()
        # Apply connection-pool settings for server-class databases
        # (PostgreSQL, MySQL). SQLite uses a file-lock model and ignores
        # pool configuration, so we skip it.
        pool_kwargs = {} if "sqlite" in database_url else get_pool_kwargs()
        engine = create_async_engine(
            database_url, connect_args=connect_args, **pool_kwargs
        )
        if "sqlite" in database_url:
            event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
        _engine_instance = engine
    return _engine_instance


def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_async_session_maker() -> async_sessionmaker:
    """Return the process-wide session maker bound to the lazy engine."""
    global _session_maker_instance
    if _session_maker_instance is None:
        _session_maker_instance = async_sessionmaker(
            get_sql_engine(), expire_on_commit=False
        )
    return _session_maker_instance


class _LazyEngine:
    """Compatibility adapter for the ``sql_engine`` import surface.

    Consumers still do ``from services.database import sql_engine`` and use
    engine attributes (``begin``, ``dispose``, ``sync_engine``); reads are
    forwarded to the lazily constructed singleton. Remove this adapter in a
    dedicated PR once every caller has moved to ``get_sql_engine()``.
    """

    def __getattr__(self, name):
        return getattr(get_sql_engine(), name)


class _LazyAsyncSessionMaker:
    """Compatibility adapter for the ``async_session_maker`` import surface.

    Consumers still do ``from services.database import async_session_maker``
    and call it to open sessions; calls are forwarded to the lazily
    constructed singleton. Remove this adapter in a dedicated PR once every
    caller has moved to ``get_async_session_maker()``.
    """

    def __call__(self, *args, **kwargs):
        return get_async_session_maker()(*args, **kwargs)


sql_engine = _LazyEngine()
async_session_maker = _LazyAsyncSessionMaker()


_STRICT_OWNER_MODELS = (
    PresentationModel,
    SlideModel,
    PresentationLayoutCodeModel,
    TemplateModel,
    ChatHistoryMessageModel,
    ImageAsset,
    TemplateCreateInfoModel,
    AsyncTaskModel,
    AsyncPresentationGenerationTaskModel,
    WebhookSubscription,
)


@event.listens_for(Session, "do_orm_execute")
def _scope_owned_selects(execute_state) -> None:
    """Apply tenant criteria to every ORM SELECT performed during a request."""
    owner_id = get_current_owner_id_raw()
    if (
        owner_id is None
        or not execute_state.is_select
        or execute_state.execution_options.get("skip_owner_scope")
    ):
        return

    statement = execute_state.statement
    for model in _STRICT_OWNER_MODELS:
        statement = statement.options(
            with_loader_criteria(
                model,
                lambda row: row.owner_id == owner_id,
                include_aliases=True,
            )
        )
    statement = statement.options(
        with_loader_criteria(
            TemplateV2,
            lambda row: or_(
                row.owner_id == owner_id,
                (row.owner_id.is_(None) & row.is_default.is_(True)),
            ),
            include_aliases=True,
        )
    )
    execute_state.statement = statement


@event.listens_for(Session, "before_flush")
def _stamp_new_owned_rows(session, _flush_context, _instances) -> None:
    owner_id = get_current_owner_id_raw()
    if owner_id is None:
        return
    owner_models = _STRICT_OWNER_MODELS + (TemplateV2,)
    for instance in session.new:
        if isinstance(instance, TemplateV2) and instance.is_default:
            instance.owner_id = None
            continue
        if isinstance(instance, owner_models):
            instance.owner_id = owner_id


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


# Create Database and Tables
async def create_db_and_tables():
    should_run_alembic = get_migrate_database_on_startup_env() in ["true", "True"]
    if not should_run_alembic:
        async with sql_engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: SQLModel.metadata.create_all(
                    sync_conn,
                    tables=[
                        PresentationModel.__table__,
                        SlideModel.__table__,
                        KeyValueSqlModel.__table__,
                        TemplateV2.__table__,
                        ChatHistoryMessageModel.__table__,
                        ImageAsset.__table__,
                        FontUpload.__table__,
                        PresentationLayoutCodeModel.__table__,
                        TemplateCreateInfoModel.__table__,
                        TemplateModel.__table__,
                        WebhookSubscription.__table__,
                        AsyncTaskModel.__table__,
                        AsyncPresentationGenerationTaskModel.__table__,
                        User.__table__,
                        AccessToken.__table__,
                        ProviderSettings.__table__,
                    ],
                )
            )


async def dispose_engines():
    """Dispose all engine connection pools.

    Call this during application shutdown (e.g. in a FastAPI ``shutdown``
    event or lifespan context) to release every connection back to the
    database and prevent stale / leaked connections. Shutdown must not
    construct the engine, so skip disposal when nothing was ever created.
    """
    if _engine_instance is not None:
        await _engine_instance.dispose()
