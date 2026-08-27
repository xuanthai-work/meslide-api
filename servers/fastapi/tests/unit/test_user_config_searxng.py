from models.user_config import UserConfig
from utils import user_config
from utils.get_env import get_searxng_base_url_env


def test_get_user_config_includes_searxng_base_url(monkeypatch):
    monkeypatch.setattr(user_config, "read_user_config_file", lambda _path: {})
    monkeypatch.setenv("USER_CONFIG_PATH", "/tmp/missing-user-config.json")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    config = user_config.get_user_config()

    assert config.SEARXNG_BASE_URL == "http://searxng:8080"


def test_update_env_applies_searxng_base_url(monkeypatch):
    monkeypatch.setattr(
        user_config,
        "get_user_config",
        lambda: UserConfig(SEARXNG_BASE_URL="http://searxng:8080"),
    )

    user_config.update_env_with_user_config()

    assert get_searxng_base_url_env() == "http://searxng:8080"
