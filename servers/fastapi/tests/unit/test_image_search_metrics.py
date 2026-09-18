"""Observability tests for the image-search pipeline (plan Phase 8).

- One structured ``image_search_slot`` metrics line per resolved slot:
  search latency, candidates returned/attempted, import latency, bytes,
  selected engine/domain, and outcome (imported / placeholder / cancelled).
- Sensitive queries never reach logs verbatim — only a short SHA-256
  prefix — and failure outcomes no longer carry the raw query either.
- The operator health endpoint reports the rollout gate, redacts SearXNG
  credentials, and its probe performs a read-only search (never imports).
"""

import asyncio
import logging
import uuid

import pytest
from fastapi import HTTPException

from models.sql.image_asset import ImageAsset
from services.image_search.metrics import (
    OUTCOME_CANCELLED,
    OUTCOME_IMPORTED,
    OUTCOME_PLACEHOLDER,
    ImageSearchSlotMetrics,
    hash_query_for_log,
)
from services.image_search.models import (
    ImageCandidateImporter,
    ImageSearchCandidate,
)
from services.image_search.service import (
    FAILURE_NO_RESULTS,
    SearxngImageSearchService,
)


METRICS_LOGGER = "services.image_search.metrics"


def _candidate(url="https://good.org/1.jpg", engine="bing images"):
    return ImageSearchCandidate(
        title="t", image_url=url, source_page_url="https://good.org/", engine=engine
    )


class _ListAdapter:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error

    async def fetch(self, normalized_query, *, limit):
        if self.error is not None:
            raise self.error
        return list(self.candidates)


def _asset(size_bytes=1234):
    return ImageAsset(
        id=uuid.uuid4(),
        path="/tmp/search-import-x.png",
        is_uploaded=False,
        extras={
            "origin": "web_search",
            "engine": "bing images",
            "size_bytes": size_bytes,
        },
    )


class _ScriptedImporter(ImageCandidateImporter):
    def __init__(self, asset=None, error=None):
        self.asset = asset or _asset()
        self.error = error

    async def import_candidate(self, candidate, *, owner_id=None, query=None):
        if self.error is not None:
            raise self.error
        return self.asset


class _CancellingImporter(ImageCandidateImporter):
    async def import_candidate(self, candidate, *, owner_id=None, query=None):
        raise asyncio.CancelledError()


def _slot_lines(records):
    return [
        record.getMessage()
        for record in records
        if record.name == METRICS_LOGGER and record.getMessage().startswith(
            "image_search_slot"
        )
    ]


# ---------------------------------------------------------------------------
# Query hashing
# ---------------------------------------------------------------------------


def test_query_hash_is_stable_and_irreversible():
    # The hash is over the *normalized* query: whitespace and trailing
    # punctuation collapse, but case and diacritics are preserved.
    assert hash_query_for_log("  Hanoi   Skyline!!  ") == hash_query_for_log(
        "Hanoi Skyline"
    )
    digest = hash_query_for_log("Hanoi Skyline")
    assert len(digest) == 12
    assert digest == hash_query_for_log("Hanoi Skyline")
    assert hash_query_for_log("Da Nang Bridge") != digest
    assert hash_query_for_log("") == ""


def test_slot_metrics_drop_none_fields():
    record = ImageSearchSlotMetrics(
        query_hash="abc123", outcome=OUTCOME_PLACEHOLDER, failure_code=None
    )
    fields = record.to_log_fields()
    assert fields == {"query_hash": "abc123", "outcome": OUTCOME_PLACEHOLDER}
    assert "failure_code" not in fields


# ---------------------------------------------------------------------------
# One metrics line per slot
# ---------------------------------------------------------------------------


def test_imported_slot_emits_metrics_with_latency_bytes_and_selection(caplog):
    service = SearxngImageSearchService(
        adapter=_ListAdapter([_candidate(), _candidate("https://other.org/2.jpg")]),
        importer=_ScriptedImporter(asset=_asset(size_bytes=2048)),
    )
    with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
        outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.ok
    lines = _slot_lines(caplog.records)
    assert len(lines) == 1
    line = lines[0]
    assert f"outcome={OUTCOME_IMPORTED}" in line
    assert "fetch_ms=" in line
    assert "import_ms=" in line
    assert "bytes=2048" in line
    assert "candidates=2" in line
    assert "attempted=1" in line
    assert "rejected=" not in line
    assert "cached=False" in line
    assert "engine=bing" in line
    assert "domain=good.org" in line


def test_placeholder_slot_emits_failure_code(caplog):
    service = SearxngImageSearchService(adapter=_ListAdapter([]))
    with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
        outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_NO_RESULTS
    lines = _slot_lines(caplog.records)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_PLACEHOLDER}" in lines[0]
    assert f"failure_code={FAILURE_NO_RESULTS}" in lines[0]


def test_rejected_candidates_are_counted(caplog):
    from services.image_search.downloader import ImageImportError

    service = SearxngImageSearchService(
        adapter=_ListAdapter([_candidate()]),
        importer=_ScriptedImporter(error=ImageImportError("blocked", "unsafe_url")),
    )
    with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
        outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert not outcome.ok
    lines = _slot_lines(caplog.records)
    assert len(lines) == 1
    assert "attempted=1" in lines[0]
    assert "rejected=1" in lines[0]
    assert "import_ms=" in lines[0]


def test_cache_hit_is_reported_in_metrics(caplog):
    adapter = _ListAdapter([_candidate()])
    service = SearxngImageSearchService(
        adapter=adapter, importer=_ScriptedImporter()
    )
    with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
        asyncio.run(service.search_and_import("hanoi skyline"))
        asyncio.run(service.search_and_import("hanoi skyline"))
    lines = _slot_lines(caplog.records)
    assert len(lines) == 2
    assert "cached=False" in lines[0]
    assert "cached=True" in lines[1]


def test_cancelled_slot_emits_cancelled_metrics_and_reraises(caplog):
    service = SearxngImageSearchService(
        adapter=_ListAdapter([_candidate()]), importer=_CancellingImporter()
    )
    with caplog.at_level(logging.INFO, logger=METRICS_LOGGER):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(service.search_and_import("hanoi skyline"))
    lines = _slot_lines(caplog.records)
    assert len(lines) == 1
    assert f"outcome={OUTCOME_CANCELLED}" in lines[0]


# ---------------------------------------------------------------------------
# Sensitive queries stay out of logs
# ---------------------------------------------------------------------------


def test_raw_query_never_reaches_image_search_logs(caplog):
    secret = "hanoi skyline"
    adapter = _ListAdapter(
        candidates=[_candidate()],
        error=RuntimeError("search exploded"),
    )
    service = SearxngImageSearchService(adapter=adapter)
    with caplog.at_level(logging.INFO):
        outcome = asyncio.run(service.search_and_import(secret))
    assert outcome.failure_code == "image_search.placeholder_fallback"
    # Neither the metrics lines nor any other image-search log carries the
    # query itself — only its hash.
    assert secret not in caplog.text
    assert hash_query_for_log(secret) in caplog.text
    assert outcome.detail is None


def test_no_results_outcome_no_longer_carries_the_raw_query():
    service = SearxngImageSearchService(adapter=_ListAdapter([]))
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_NO_RESULTS
    assert outcome.detail is None


# ---------------------------------------------------------------------------
# Operator health endpoint
# ---------------------------------------------------------------------------


class _ProbeService:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error
        self.queries: list[str] = []

    async def search(self, query, *, limit, stats=None):
        self.queries.append(query)
        if self.error is not None:
            raise self.error
        if stats is not None:
            stats["fetch_latency_ms"] = 42
            stats["cached"] = False
        return list(self.candidates)


@pytest.fixture
def available_gates(monkeypatch):
    import services.image_search.availability as availability
    import utils.searxng as searxng_utils

    monkeypatch.setattr(
        availability, "web_image_search_feature_enabled", lambda: True
    )
    monkeypatch.setattr(availability, "web_image_search_configured", lambda: True)
    monkeypatch.setattr(availability, "web_image_search_available", lambda: True)
    monkeypatch.setattr(availability, "get_web_image_search_max_results", lambda: 5)
    monkeypatch.setattr(
        searxng_utils,
        "get_searxng_base_url",
        lambda: "http://operator:secret@searxng:8080/search",
    )


def _health(monkeypatch, service=None):
    import services.image_search as image_search_package
    from api.v1.ppt.endpoints.images import get_image_search_health

    if service is not None:
        monkeypatch.setattr(
            image_search_package, "get_image_search_service", lambda: service
        )
    return asyncio.run(get_image_search_health(probe=True))


def test_health_reports_gates_and_redacts_base_url(available_gates):
    from api.v1.ppt.endpoints.images import get_image_search_health

    health = asyncio.run(get_image_search_health())
    assert health["feature_enabled"] is True
    assert health["configured"] is True
    assert health["available"] is True
    assert health["max_results"] == 5
    assert health["base_url"] == "http://***:***@searxng:8080/search"
    assert "probe" not in health


def test_health_probe_reports_candidates_and_engines(available_gates, monkeypatch):
    service = _ProbeService(
        candidates=[
            _candidate(engine="bing images"),
            _candidate("https://good.org/2.jpg", engine="duckduckgo"),
        ]
    )
    health = _health(monkeypatch, service=service)
    probe = health["probe"]
    assert probe["ok"] is True
    assert probe["candidates"] == 2
    assert probe["engines"] == ["bing images", "duckduckgo"]
    assert probe["latency_ms"] == 42
    assert probe["cached"] is False
    # The probe searches; it never imports, so nothing is mutated.
    assert len(service.queries) == 1
    assert service.queries[0] == "gslide image search health probe"


def test_health_probe_reports_timeout_without_raising(available_gates, monkeypatch):
    import asyncio as _asyncio

    class _TimingOutService(_ProbeService):
        async def search(self, query, *, limit, stats=None):
            raise _asyncio.TimeoutError()

    health = _health(monkeypatch, service=_TimingOutService())
    assert health["probe"] == {"ok": False, "reason": "timeout"}


def test_health_probe_reports_searxng_http_error(available_gates, monkeypatch):
    health = _health(
        monkeypatch, service=_ProbeService(error=HTTPException(400, "SEARXNG_BASE_URL is not configured"))
    )
    assert health["probe"]["ok"] is False
    assert "not configured" in health["probe"]["reason"]


def test_health_probe_without_configuration_reports_reason(monkeypatch):
    import services.image_search.availability as availability

    monkeypatch.setattr(
        availability, "web_image_search_feature_enabled", lambda: False
    )
    monkeypatch.setattr(availability, "web_image_search_configured", lambda: False)
    monkeypatch.setattr(availability, "web_image_search_available", lambda: False)
    health = _health(monkeypatch)
    assert health["probe"] == {"ok": False, "reason": "not_enabled"}
