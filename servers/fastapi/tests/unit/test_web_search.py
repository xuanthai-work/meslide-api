import asyncio
import logging

from enums.llm_provider import LLMProvider
from enums.web_search_provider import WebSearchProvider
from utils import web_search


def test_auto_uses_native_search_for_supported_llm(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.OPENAI.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.AUTO.value)

    assert web_search.should_use_native_web_search() is True
    assert web_search.should_expose_external_web_search_tool() is False


def test_unknown_web_search_provider_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.OPENAI.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "serper")

    assert web_search.get_selected_web_search_provider() == WebSearchProvider.AUTO
    assert web_search.should_use_native_web_search() is True


def test_legacy_tavily_provider_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.OPENAI.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "tavily")

    assert web_search.get_selected_web_search_provider() == WebSearchProvider.AUTO
    assert web_search.should_use_native_web_search() is True


def test_auto_reports_unavailable_without_configured_external_provider(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.CUSTOM.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.AUTO.value)
    monkeypatch.delenv("SEARXNG_BASE_URL", raising=False)

    assert web_search.should_use_native_web_search() is False
    assert web_search.should_expose_external_web_search_tool() is False
    assert web_search.get_web_search_route() == ("unavailable", None)


def test_auto_uses_searxng_when_native_unavailable_and_url_configured(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.CUSTOM.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.AUTO.value)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    assert web_search.should_use_native_web_search() is False
    assert web_search.should_expose_external_web_search_tool() is True
    assert web_search.resolve_external_web_search_provider() == WebSearchProvider.SEARXNG
    assert web_search.get_web_search_route() == (
        "external",
        WebSearchProvider.SEARXNG,
    )


def test_explicit_searxng_search_is_supported(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.SEARXNG.value)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    assert web_search.resolve_external_web_search_provider() == WebSearchProvider.SEARXNG
    assert web_search.should_expose_external_web_search_tool() is True
    assert web_search.should_use_native_web_search() is False


def test_explicit_native_search_does_not_fallback_for_unsupported_llm(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.CUSTOM.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.NATIVE.value)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    assert web_search.should_use_native_web_search() is False
    assert web_search.should_expose_external_web_search_tool() is False
    assert web_search.get_web_search_route() == ("unavailable", None)


def test_auto_does_not_expose_external_search_when_native_tools_are_unavailable(
    monkeypatch,
):
    monkeypatch.setenv("LLM", LLMProvider.GOOGLE.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.AUTO.value)

    assert web_search.should_use_native_web_search() is True
    assert (
        web_search.should_expose_external_web_search_tool(
            native_search_available=False
        )
        is False
    )


def test_format_web_search_context_excludes_source_urls():
    context = web_search.format_web_search_context(
        [
            web_search.WebSearchResult(
                title="Search Result",
                url="https://example.com/page",
                snippet=(
                    "Presentation generation [6][7] with "
                    "[documentation](https://example.com/docs)"
                ),
            )
        ]
    )

    assert "Web search results" in context
    assert "https://example.com/page" not in context
    assert "https://example.com/docs" not in context
    assert "URL:" not in context
    assert "[6]" not in context
    assert "documentation" in context
    assert "Presentation generation" in context


def test_auto_does_not_resolve_external_provider_when_native_is_available(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.OPENAI.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.AUTO.value)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8080")

    assert web_search.resolve_external_web_search_provider() is None
    assert web_search.get_web_search_route() == ("native", None)


def test_explicit_searxng_overrides_native_llm(monkeypatch):
    monkeypatch.setenv("LLM", LLMProvider.OPENAI.value)
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.SEARXNG.value)
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8080")

    assert web_search.should_use_native_web_search() is False
    assert web_search.should_expose_external_web_search_tool() is True
    assert web_search.get_web_search_route() == (
        "external",
        WebSearchProvider.SEARXNG,
    )


def test_searxng_accepts_base_or_search_url(monkeypatch):
    monkeypatch.setenv("SEARXNG_BASE_URL", "http://127.0.0.1:8080")
    assert web_search._get_searxng_search_url() == "http://127.0.0.1:8080/search"

    monkeypatch.setenv(
        "SEARXNG_BASE_URL",
        "http://127.0.0.1:8080/search?q=ignored&format=json",
    )
    assert web_search._get_searxng_search_url() == "http://127.0.0.1:8080/search"


def test_searxng_log_url_redacts_credentials():
    assert (
        web_search._redact_url_credentials(
            "http://user:secret@127.0.0.1:8080/search"
        )
        == "http://***:***@127.0.0.1:8080/search"
    )


def test_search_searxng_maps_json_results(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def json(self, content_type=None):
            return {
                "results": [
                    {
                        "title": "SearXNG Result",
                        "url": "https://example.com/page",
                        "content": "Self-hosted search snippet.",
                    },
                    {"title": "Skip me", "url": ""},
                ]
            }

        async def text(self):
            return ""

    class FakeSession:
        def get(self, url, params):
            captured.update(url=url, params=params)
            return FakeResponse()

    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")

    results = asyncio.run(
        web_search._search_searxng(FakeSession(), "presentation ai", 5)
    )

    assert captured["url"] == "http://searxng:8080/search"
    assert captured["params"] == {"q": "presentation ai", "format": "json"}
    assert results == [
        web_search.WebSearchResult(
            title="SearXNG Result",
            url="https://example.com/page",
            snippet="Self-hosted search snippet.",
        )
    ]


def test_search_web_logs_provider_and_clamps_max_results(monkeypatch, caplog):
    captured = {}

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    async def fake_search(_session, query, limit):
        captured.update(query=query, limit=limit)
        return [
            web_search.WebSearchResult(
                title="Presenton",
                url="https://example.com/presenton",
            )
        ]

    monkeypatch.setenv("WEB_SEARCH_PROVIDER", WebSearchProvider.SEARXNG.value)
    monkeypatch.setattr(
        web_search.aiohttp,
        "ClientSession",
        lambda **_kwargs: FakeSession(),
    )
    monkeypatch.setattr(web_search, "_search_searxng", fake_search)
    caplog.set_level(logging.INFO, logger=web_search.__name__)

    results = asyncio.run(web_search.search_web(" current facts ", max_results=50))

    assert captured == {"query": "current facts", "limit": 10}
    assert len(results) == 1
    assert "provider=searxng" in caplog.text
    assert "results=1" in caplog.text
