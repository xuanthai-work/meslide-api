import asyncio
from unittest.mock import patch

from services.database import create_db_and_tables


class _FakeConn:
    async def run_sync(self, fn):
        fn(object())


class _FakeBegin:
    async def __aenter__(self):
        return _FakeConn()

    async def __aexit__(self, *_args):
        return False


def test_create_db_and_tables_without_alembic_does_not_reference_ollama():
    with patch(
        "services.database.get_migrate_database_on_startup_env",
        return_value="false",
    ), patch("services.database.sql_engine") as engine, patch(
        "services.database.SQLModel.metadata.create_all"
    ) as create_all:
        engine.begin.return_value = _FakeBegin()
        asyncio.run(create_db_and_tables())

    create_all.assert_called_once()
    tables = create_all.call_args.kwargs["tables"]
    names = {getattr(table, "name", "") for table in tables}
    assert "ollamapullstatus" not in names
    assert "presenton_cloud_provider" not in names
