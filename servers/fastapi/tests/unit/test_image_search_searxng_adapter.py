"""Adapter tests: exact request params, fixture parsing, URL canonicalization."""

import asyncio
import json
from pathlib import Path

import pytest

from services.image_search import searxng as searxng_adapter

FIXTURES_DIR = Path(__file__).parents[1] / "fixtures" / "searxng"


def _load_fixture(name: str) -> dict:
    return json.loads(
        (FIXTURES_DIR / name).read_text(encoding="utf-8")
    )


# ---------------------------------------------------------------------------
# Exact SearXNG request parameters (FR-3)
# ---------------------------------------------------------------------------


def test_fetch_sends_exact_image_search_params(monkeypatch):
    captured = {}

    class FakeResponse:
        status = 200

        async def text(self):
            return ""

        async def json(self, content_type=None):
            return {"results": []}

    class FakeCM:
        def __init__(self, response):
            self._response = response

        async def __aenter__(self):
            return self._response

        async def __aexit__(self, *_args):
            return None

    class FakeSession:
        def __init__(self, **_kwargs):
            pass

        def get(self, url, params):
            captured.update(url=url, params=params)
            return FakeCM(FakeResponse())

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setenv("SEARXNG_BASE_URL", "http://searxng:8080")
    monkeypatch.setattr(
        searxng_adapter.aiohttp, "ClientSession", lambda **kwargs: FakeSession(**kwargs)
    )

    adapter = searxng_adapter.SearxngImageSearchAdapter()
    results = asyncio.run(adapter.fetch("vietnam solar farm", limit=10))

    assert results == []
    assert captured["url"] == "http://searxng:8080/search"
    assert captured["params"] == {
        "q": "vietnam solar farm",
        "format": "json",
        "categories": "images",
        "safesearch": "1",
    }


# ---------------------------------------------------------------------------
# URL canonicalization: schemes, protocol-relative, tracking params
# ---------------------------------------------------------------------------


def test_canonicalize_fixes_protocol_relative_flickr_url():
    assert (
        searxng_adapter.canonicalize_url("//live.staticflickr.com/1/a_c.jpg")
        == "https://live.staticflickr.com/1/a_c.jpg"
    )


def test_canonicalize_strips_tracking_params_and_fragment():
    assert (
        searxng_adapter.canonicalize_url(
            "https://Upload.Wikimedia.org/x.jpg?utm_source=c&utm_medium=x&w=2#frag"
        )
        == "https://upload.wikimedia.org/x.jpg?w=2"
    )


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "   ",
        "ftp://host/img.jpg",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "file:///etc/passwd",
        "/static/images/placeholder.jpg",
        12345,
    ],
)
def test_canonicalize_rejects_invalid_schemes(bad):
    assert searxng_adapter.canonicalize_url(bad) is None


# ---------------------------------------------------------------------------
# Fixture parsing: real SearXNG response quirks (see fixtures/README.md)
# ---------------------------------------------------------------------------


def test_parse_fixture_vietnam_solar_farm():
    payload = _load_fixture("images_vietnam_solar_farm.json")
    candidates = searxng_adapter.parse_searxng_image_results(payload, limit=30)
    assert candidates, "fixture must yield candidates"
    for candidate in candidates:
        assert candidate.image_url.startswith("https://")
        assert candidate.title != "" or candidate.engine is not None
    # Dimension strings in all observed formats parse to ints.
    with_dims = [c for c in candidates if c.width and c.height]
    assert with_dims, "fixture contains resolution strings"
    assert all(c.width > 0 and c.height > 0 for c in with_dims)


def test_parse_dedupes_canonical_urls_across_engines():
    payload = {
        "results": [
            {
                "title": "Same photo",
                "img_src": "https://cdn.example.org/photo.jpg",
                "engine": "bing images",
            },
            {
                "title": "Same photo again",
                "img_src": "https://CDN.example.org/photo.jpg?utm_source=x",
                "engine": "flickr",
            },
            {
                "title": "Other",
                "img_src": "https://cdn.example.org/other.jpg",
                "engine": "pexels",
            },
        ]
    }
    candidates = searxng_adapter.parse_searxng_image_results(payload, limit=10)
    assert len(candidates) == 2
    assert candidates[0].image_url == "https://cdn.example.org/photo.jpg"


def test_parse_tolerates_missing_fields():
    payload = {
        "results": [
            {
                "img_src": "//live.staticflickr.com/7136/x_c.jpg",
                # no title, no thumbnail, no url, no resolution, no engine
            },
            {"title": "no image src at all"},
            {"img_src": "javascript:alert(1)", "title": "bad scheme"},
        ]
    }
    candidates = searxng_adapter.parse_searxng_image_results(payload, limit=10)
    assert len(candidates) == 1
    assert candidates[0].image_url == "https://live.staticflickr.com/7136/x_c.jpg"
    assert candidates[0].title == ""
    assert candidates[0].thumbnail_url is None


def test_parse_respects_limit():
    payload = _load_fixture("images_ha_noi_skyline.json")
    candidates = searxng_adapter.parse_searxng_image_results(payload, limit=5)
    assert len(candidates) == 5


def test_parse_tolerates_non_list_results():
    assert searxng_adapter.parse_searxng_image_results({"results": None}, 5) == []
    assert searxng_adapter.parse_searxng_image_results({}, 5) == []