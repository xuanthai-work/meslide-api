import asyncio

from models.sql.user import User
from services.provider_settings import (
    fill_unset_from_runtime,
    migrate_provider_settings_from_file,
    sanitize_provider_settings,
)
from utils.user_config_store import read_user_config_file, update_user_config_file


class ProviderSettingsSession:
    def __init__(self):
        self.row = None

    async def get(self, _model, _key):
        return self.row

    def add(self, row):
        self.row = row

    async def commit(self):
        return None

    async def refresh(self, _row):
        return None


def test_user_table_has_username_and_no_email_column():
    columns = set(User.__table__.columns.keys())

    assert "username" in columns
    assert "email" not in columns


def test_provider_settings_exclude_all_legacy_auth_fields():
    assert sanitize_provider_settings(
        {
            "LLM": "openai",
            "AUTH_USERNAME": "admin",
            "AUTH_PASSWORD": "plain",
            "AUTH_PASSWORD_HASH": "hash",
            "AUTH_SECRET_KEY": "jwt-secret",
        }
    ) == {"LLM": "openai"}


def test_startup_migrates_user_config_and_rewrites_compatibility_file(
    monkeypatch, tmp_path
):
    path = tmp_path / "userConfig.json"
    monkeypatch.setenv("USER_CONFIG_PATH", str(path))
    update_user_config_file(
        str(path),
        lambda _: {
            "AUTH_USERNAME": "admin",
            "AUTH_PASSWORD_HASH": "legacy-hash",
            "AUTH_SECRET_KEY": "jwt-secret",
            "LLM": "openai",
            "OPENAI_API_KEY": "provider-key",
        },
    )
    session = ProviderSettingsSession()

    migrated = asyncio.run(migrate_provider_settings_from_file(session))

    assert session.row.config == {
        "LLM": "openai",
        "OPENAI_API_KEY": "provider-key",
    }
    assert migrated["LLM"] == "openai"
    assert migrated["OPENAI_API_KEY"] == "provider-key"
    assert "AUTH_USERNAME" not in migrated
    assert read_user_config_file(str(path))["LLM"] == "openai"
    assert read_user_config_file(str(path))["AUTH_USERNAME"] == "admin"
    assert read_user_config_file(str(path))["AUTH_PASSWORD_HASH"] == "legacy-hash"
    assert read_user_config_file(str(path))["AUTH_SECRET_KEY"] == "jwt-secret"


def test_empty_provider_settings_fill_custom_llm_from_env(monkeypatch, tmp_path):
    path = tmp_path / "userConfig.json"
    monkeypatch.setenv("USER_CONFIG_PATH", str(path))
    monkeypatch.setenv("LLM", "custom")
    monkeypatch.setenv("CUSTOM_LLM_URL", "http://llm.example/v1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "sk-env")
    monkeypatch.setenv("CUSTOM_MODEL", "cb/hnw-llm")
    monkeypatch.setenv("DISABLE_IMAGE_GENERATION", "true")
    update_user_config_file(str(path), lambda _: {})

    filled = fill_unset_from_runtime({})

    assert filled["LLM"] == "custom"
    assert filled["CUSTOM_LLM_URL"] == "http://llm.example/v1"
    assert filled["CUSTOM_LLM_API_KEY"] == "sk-env"
    assert filled["CUSTOM_MODEL"] == "cb/hnw-llm"


def test_saved_provider_settings_are_not_overwritten_by_env(monkeypatch, tmp_path):
    path = tmp_path / "userConfig.json"
    monkeypatch.setenv("USER_CONFIG_PATH", str(path))
    monkeypatch.setenv("LLM", "custom")
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    update_user_config_file(str(path), lambda _: {"LLM": "openai"})

    filled = fill_unset_from_runtime({"LLM": "openai", "OPENAI_API_KEY": "db-key"})

    assert filled["LLM"] == "openai"
    assert filled["OPENAI_API_KEY"] == "db-key"
