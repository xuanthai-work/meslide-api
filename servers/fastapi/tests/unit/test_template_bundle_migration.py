"""Tests: template_v2 bundle columns migration (SQLite, via Alembic)."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

BUNDLE_COLUMNS = {
    "bundle_object_key",
    "bundle_sha256",
    "bundle_size_bytes",
    "bundle_schema_version",
    "layout_count",
}


def _alembic_config(database_url: str) -> Config:
    config = Config()
    config.set_main_option(
        "script_location", str(Path(__file__).resolve().parents[2] / "alembic")
    )
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _template_v2_columns(connection) -> set[str]:
    return {
        row[1] for row in connection.execute(text("PRAGMA table_info('template_v2')"))
    }


def test_bundle_columns_added_on_upgrade_and_removed_on_downgrade(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'bundle-columns.db'}"
    config = _alembic_config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert BUNDLE_COLUMNS <= _template_v2_columns(connection)
    finally:
        engine.dispose()

    # Downgrade back below the bundle migration drops the columns again
    # (PostgreSQL parity; SQLite supports drop_column since 3.35).
    command.downgrade(config, "-1")

    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            assert not (BUNDLE_COLUMNS & _template_v2_columns(connection))
    finally:
        engine.dispose()


def test_bundle_columns_are_nullable_on_existing_rows(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'existing-rows.db'}"
    config = _alembic_config(database_url)

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO template_v2 (id, name, is_default, created_at, "
                    "updated_at) VALUES ('t1', 'legacy', 0, '2026-01-01', "
                    "'2026-01-01')"
                )
            )
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT bundle_object_key, bundle_sha256, bundle_size_bytes, "
                    "bundle_schema_version, layout_count FROM template_v2 WHERE id = "
                    "'t1'"
                )
            ).one()
        assert row == (None, None, None, None, None)
    finally:
        engine.dispose()


def test_model_fields_match_migration_columns():
    from models.sql.template_v2 import TemplateV2

    columns = set(TemplateV2.__table__.columns.keys())
    assert BUNDLE_COLUMNS <= columns
    assert TemplateV2.__table__.columns["bundle_size_bytes"].nullable is True
    assert TemplateV2.__table__.columns["bundle_object_key"].nullable is True