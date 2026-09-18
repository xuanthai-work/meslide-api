
from utils.get_env import get_openai_api_key_env
from utils.provider_overlay import (
    overlay_or_env,
    reset_provider_overlay,
    set_provider_overlay,
)


def test_overlay_wins_over_process_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    token = set_provider_overlay({"OPENAI_API_KEY": "sk-user"})
    try:
        assert get_openai_api_key_env() == "sk-user"
        assert overlay_or_env("OPENAI_API_KEY") == "sk-user"
    finally:
        reset_provider_overlay(token)
    assert get_openai_api_key_env() == "sk-env"


def test_blank_overlay_field_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env")
    token = set_provider_overlay({"OPENAI_API_KEY": "  "})
    try:
        assert get_openai_api_key_env() == "sk-env"
    finally:
        reset_provider_overlay(token)
