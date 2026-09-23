"""Supported database contract: a fresh database upgrades to Alembic head.

Unversioned legacy databases, orphan revision IDs, and upgrades from retired
intermediate revisions are outside support.
"""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from models.sql.access_token import AccessToken
from models.sql.async_presentation_generation_status import (
    AsyncPresentationGenerationTaskModel,
)
from models.sql.async_task import AsyncTaskModel
from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from models.sql.template import TemplateModel
from models.sql.template_v2 import TemplateV2
from models.sql.user import User
from models.sql.user_provider_settings import UserProviderSettings

OWNERSHIP_MODELS = (
    PresentationModel,
    SlideModel,
    TemplateModel,
    TemplateV2,
    AsyncTaskModel,
    AsyncPresentationGenerationTaskModel,
    AccessToken,
    UserProviderSettings,
)

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


def _ownership_column(model) -> str:
    columns = model.__table__.columns
    if "owner_id" in columns:
        return "owner_id"
    if "user_id" in columns:
        return "user_id"
    raise AssertionError(f"{model.__tablename__} has no ownership column")


def test_fresh_database_upgrades_to_head(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    config = _alembic_config(database_url)
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head

    command.upgrade(config, "head")

    engine = create_engine(database_url)
    try:
        inspector = inspect(engine)
        with engine.connect() as connection:
            version = connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version == head

        tables = set(inspector.get_table_names())
        assert User.__tablename__ in tables

        for model in OWNERSHIP_MODELS:
            assert model.__tablename__ in tables, model.__tablename__
            columns = {
                column["name"]: column
                for column in inspector.get_columns(model.__tablename__)
            }
            ownership = _ownership_column(model)
            assert ownership in columns
            assert (
                columns[ownership]["nullable"]
                == model.__table__.columns[ownership].nullable
            )

        template_columns = {
            column["name"]: column
            for column in inspector.get_columns(TemplateV2.__tablename__)
        }
        for name in BUNDLE_COLUMNS:
            assert name in template_columns
            assert template_columns[name]["nullable"] is True
            assert TemplateV2.__table__.columns[name].nullable is True
    finally:
        engine.dispose()
