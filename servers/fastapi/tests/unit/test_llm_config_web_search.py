"""Smoke tests for the native LLM config helpers."""

import pytest


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    """Each test sets its own LLM/env; clean stale keys first."""
    for key in (
        "LLM",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "CUSTOM_LLM_URL",
        "CUSTOM_LLM_API_KEY",
        "CUSTOM_LLM_MODEL",
        "DISABLE_THINKING",
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_GROUNDING",
    ):
        monkeypatch.delenv(key, raising=False)


def test_openai_provider_returns_openai_client(monkeypatch):
    from openai import OpenAI

    from utils.llm_provider import get_llm_client

    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    client = get_llm_client()

    assert isinstance(client, OpenAI)


def test_google_provider_returns_genai_client(monkeypatch):
    from google import genai

    from utils.llm_provider import get_llm_client

    monkeypatch.setenv("LLM", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")

    client = get_llm_client()

    assert isinstance(client, genai.Client)


def test_custom_provider_uses_openai_compatible_client(monkeypatch):
    from openai import OpenAI

    from utils.llm_provider import get_llm_client

    monkeypatch.setenv("LLM", "custom")
    monkeypatch.setenv("CUSTOM_LLM_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("CUSTOM_LLM_API_KEY", "custom-key")

    client = get_llm_client()

    assert isinstance(client, OpenAI)


def test_custom_disable_thinking_uses_legacy_payload(monkeypatch):
    from utils.llm_config import get_extra_body

    monkeypatch.setenv("LLM", "custom")
    monkeypatch.setenv("DISABLE_THINKING", "true")

    assert get_extra_body() == {"enable_thinking": False}


def test_openai_disable_thinking_yields_no_extra_body(monkeypatch):
    from utils.llm_config import get_extra_body

    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DISABLE_THINKING", "true")

    assert get_extra_body() is None
