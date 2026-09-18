"""Unit tests for the lazy database engine seam.

Importing ``services.database`` used to call
``get_database_url_and_connect_args()`` and ``create_async_engine()`` at
module scope, so the import created the SQLite parent directory and the
engine before any test or request needed them. These tests pin the new
contract:

- importing the module creates nothing on disk,
- the engine and session maker are constructed on first use (lazy
  factories returning process-wide singletons),
- the module-level ``sql_engine``/``async_session_maker`` adapters keep
  delegating so existing imports and call sites stay unchanged,
- the SQLite foreign-keys listener is attached to the created engine.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from services import database as database_module

SERVERS_FASTAPI_ROOT = Path(__file__).resolve().parents[2]


def test_cold_import_does_not_create_database_directory():
    """A fresh interpreter importing the module must not touch the filesystem."""
    code = textwrap.dedent(
        """
        import os
        import shutil
        import sys
        import tempfile

        root = sys.argv[1]
        probe = os.path.join(tempfile.gettempdir(), "gslide-db-import-probe")
        shutil.rmtree(probe, ignore_errors=True)

        os.environ["APP_DATA_DIRECTORY"] = probe
        os.environ.pop("DATABASE_URL", None)
        sys.path.insert(0, root)

        import services.database as module

        assert not os.path.exists(probe), (
            "importing the module must not create the database parent dir"
        )

        module.get_sql_engine()

        assert os.path.exists(probe), (
            "first use of the factory must create the database parent dir"
        )
        """
    )
    env = {
        **os.environ,
        "APP_DATA_DIRECTORY": "",
        "TEMP_DIRECTORY": "",
        "DATABASE_URL": "",
    }
    result = subprocess.run(
        [sys.executable, "-c", code, str(SERVERS_FASTAPI_ROOT)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr


def test_engine_factory_returns_process_wide_singleton():
    first = database_module.get_sql_engine()
    second = database_module.get_sql_engine()
    assert first is second


def test_session_maker_factory_returns_process_wide_singleton():
    first = database_module.get_async_session_maker()
    second = database_module.get_async_session_maker()
    assert first is second


def test_lazy_adapters_delegate_to_singletons():
    engine = database_module.get_sql_engine()
    # Attribute reads on the adapter forward to the singleton engine...
    assert database_module.sql_engine.dialect is engine.dialect
    # ...and session-maker calls produce sessions on that engine.
    async def _make_session():
        async with database_module.async_session_maker() as session:
            return session

    import asyncio

    session = asyncio.run(_make_session())
    assert isinstance(session, AsyncSession)
    asyncio.run(session.close())


def test_sqlite_foreign_keys_listener_is_attached():
    engine = database_module.get_sql_engine()
    if "sqlite" not in engine.dialect.name:
        return  # listener only applies to the SQLite default stack
    assert event.contains(
        engine.sync_engine, "connect", database_module._enable_sqlite_foreign_keys
    )


def test_session_maker_defaults_keep_expire_on_commit_false():
    maker = database_module.get_async_session_maker()
    assert maker.kw["expire_on_commit"] is False