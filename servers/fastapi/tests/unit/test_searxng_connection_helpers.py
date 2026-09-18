"""Phase 2 tests: shared SearXNG connection helpers + image-search gating.

Covers the plan's Phase 2 test list:
- base URL and ``/search`` URL normalization (missing config included)
- credential redaction
- native text search coexisting with SearXNG image-search availability
- missing/disabled SearXNG image-search state
- text-search routing regression is covered by tests/unit/test_web_search.py
"""

import pytest
from fastapi import HTTPException

from services.image_search.availability import (
    get_web_image_search_max_results,
    web_image_search_available,
    web_image_search_configured,
    web_image_search_feature_enabled,
)
from utils import searxng


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def test_search_url_appends_search_path():
    assert searxng.build_searxng_search_url("http://searxng:8080") == (
        "http://searxng:8080/search"
    )


def test_search_url_accepts_existing_search_path():
    assert searxng.build_searxng_search_url(
        "http://searxng:8080/search?q=ignored&format=json"
    ) == "http://searxng:8080/search"


def test_search_url_strips_trailing_slash_and_drops_query_fragment():
    assert searxng.build_searxng_search_url(
        "http://searxng.example/search/#/results"
    ) == "http://searxng.example/search"


def test_search_url_requires_configuration(monkeypatch):
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    with pytest.raises(HTTPException) as exc_info:
        searxng.build_searxng_search_url()
    assert exc_info.value.status_code == 400
    assert "SEARXNG_BASE_URL" in exc_info.value.detail


def test_get_searxng_base_url_blank_is_none(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "   ")
    assert searxng.get_searxng_base_url() is None


# ---------------------------------------------------------------------------
# Credential redaction
# ---------------------------------------------------------------------------


def test_redact_masks_user_and_password():
    assert searxng.redact_searxng_url_credentials(
        "http://user:secret@127.0.0.1:8080/search"
    ) == "http://***:***@127.0.0.1:8080/search"


def test_redact_leaves_plain_urls_untouched():
    value = "http://127.0.0.1:8080/search"
    assert searxng.redact_searxng_url_credentials(value) is value


# ---------------------------------------------------------------------------
# Image-search availability (operator rollout gate x SearXNG configuration)
# ---------------------------------------------------------------------------


def test_feature_disabled_by_default(monkeypatch):
    monkeypatch.delenv("WEB_IMAGE_SEARCH_ENABLED", raising=False)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")
    assert web_image_search_feature_enabled() is False
    assert web_image_search_available() is False


def test_feature_enabled_with_searxng_configured(monkeypatch):
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")
    assert web_image_search_feature_enabled() is True
    assert web_image_search_configured() is True
    assert web_image_search_available() is True


def test_feature_enabled_but_searxng_missing(monkeypatch):
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "true")
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)
    assert web_image_search_feature_enabled() is True
    assert web_image_search_configured() is False
    assert web_image_search_available() is False


def test_flag_garbage_values_stay_disabled(monkeypatch):
    # Case-insensitive "true" is enabled; everything else stays disabled.
    for value in ("", "yes", "1"):
        monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", value)
        monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")
        assert web_image_search_feature_enabled() is False, value
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "TRUE")
    assert web_image_search_feature_enabled() is True


# ---------------------------------------------------------------------------
# Native text search must coexist with SearXNG image search: the text route
# (native, external, unavailable) is orthogonal to the image rollout gate.
# ---------------------------------------------------------------------------


def test_native_text_route_with_image_search_enabled(monkeypatch):
    from enums.llm_provider import LLMProvider
    from utils import web_search

    monkeypatch.setenv("LLM", LLMProvider.OPENAI.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    # Text: native route for the OpenAI LLM...
    assert web_search.get_web_search_route() == ("native", None)
    # ...while image search independently reports SearXNG availability.
    assert web_image_search_available() is True


def test_external_text_route_with_image_search_enabled(monkeypatch):
    from enums.llm_provider import LLMProvider
    from enums.web_search_provider import WebSearchProvider
    from utils import web_search

    monkeypatch.setenv("LLM", LLMProvider.CUSTOM.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "auto")
    monkeypatch.setenv("WEB_IMAGE_SEARCH_ENABLED", "true")
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    # AUTO + custom LLM + configured SearXNG = external (SearXNG) text route.
    assert web_search.get_web_search_route() == (
        "external",
        WebSearchProvider.SEARXNG,
    )
    assert web_image_search_available() is True


# ---------------------------------------------------------------------------
# Bounded per-query result cap
# ---------------------------------------------------------------------------


def test_max_results_default_when_unset(monkeypatch):
    monkeypatch.delenv("WEB_IMAGE_SEARCH_MAX_RESULTS", raising=False)
    assert get_web_image_search_max_results() > 0


def test_max_results_operator_can_only_narrow(monkeypatch):
    monkeypatch.setenv("WEB_IMAGE_SEARCH_MAX_RESULTS", "1000000")
    assert get_web_image_search_max_results() <= 30

    monkeypatch.setenv("WEB_IMAGE_SEARCH_MAX_RESULTS", "5")
    assert get_web_image_search_max_results() == 5


def test_max_results_garbage_falls_back(monkeypatch):
    monkeypatch.setenv("WEB_IMAGE_SEARCH_MAX_RESULTS", "not-a-number")
    assert get_web_image_search_max_results() > 0