"""Service tests: cache hit/miss/expiry, search_and_import orchestration."""

import asyncio
from dataclasses import dataclass

from services.image_search.cache import ImageSearchCache
from services.image_search.models import (
    DeckResolutionContext,
    ImageCandidateImporter,
    ImageSearchCandidate,
)
from services.image_search.service import (
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_IMPORTER_UNAVAILABLE,
    FAILURE_NO_RESULTS,
    SearxngImageSearchService,
)


@dataclass
class FakeAdapter:
    candidates: list
    calls: list = None

    def __post_init__(self):
        if self.calls is None:
            self.calls = []

    async def fetch(self, normalized_query, *, limit):
        self.calls.append((normalized_query, limit))
        return list(self.candidates)


class FakeImporter(ImageCandidateImporter):
    def __init__(self, fail_domains=()):
        self.fail_domains = set(fail_domains)
        self.imported: list[ImageSearchCandidate] = []

    async def import_candidate(self, candidate, *, owner_id=None, query=None):
        from urllib.parse import urlparse

        if urlparse(candidate.image_url).hostname in self.fail_domains:
            raise RuntimeError("boom")
        self.imported.append(candidate)

        class FakeAsset:
            pass

        return FakeAsset()


def _candidate(url, *, engine="bing images", width=800, height=600):
    return ImageSearchCandidate(
        title="t", image_url=url, engine=engine, width=width, height=height
    )


# ---------------------------------------------------------------------------
# Cache hit / miss / expiration
# ---------------------------------------------------------------------------


def test_search_hits_cache_on_repeated_query():
    adapter = FakeAdapter([_candidate("https://a.org/1.jpg")])
    service = SearxngImageSearchService(adapter=adapter)
    first = asyncio.run(service.search("vietnam solar farm", limit=5))
    second = asyncio.run(service.search("vietnam solar farm", limit=5))
    assert first == second
    assert adapter.calls == [("vietnam solar farm", 5)]


def test_search_misses_cache_for_different_limit_or_aspect():
    adapter = FakeAdapter([_candidate("https://a.org/1.jpg")])
    service = SearxngImageSearchService(adapter=adapter)
    asyncio.run(service.search("q", limit=5))
    asyncio.run(service.search("q", limit=10))
    asyncio.run(service.search("q", limit=5, aspect_ratio=1.5))
    assert len(adapter.calls) == 3


def test_cache_entry_expires_after_ttl():
    now = [0.0]
    adapter = FakeAdapter([_candidate("https://a.org/1.jpg")])
    cache = ImageSearchCache(ttl_seconds=100.0, clock=lambda: now[0])
    service = SearxngImageSearchService(adapter=adapter, cache=cache)

    asyncio.run(service.search("q", limit=5))
    now[0] = 50.0
    asyncio.run(service.search("q", limit=5))
    assert len(adapter.calls) == 1, "within TTL must be a cache hit"

    now[0] = 150.0
    asyncio.run(service.search("q", limit=5))
    assert len(adapter.calls) == 2, "past TTL must re-fetch"


def test_cache_evicts_beyond_max_entries():
    clock = [0.0]
    cache = ImageSearchCache(ttl_seconds=1000.0, max_entries=2, clock=lambda: clock[0])
    key_one = ImageSearchCache.make_key("a", limit=5, aspect_ratio=None)
    key_two = ImageSearchCache.make_key("b", limit=5, aspect_ratio=None)
    key_three = ImageSearchCache.make_key("c", limit=5, aspect_ratio=None)
    cache.put(key_one, [])
    cache.put(key_two, [])
    cache.put(key_three, [])
    assert cache.get(key_one) is None, "oldest entry evicted"
    assert cache.get(key_three) == []


# ---------------------------------------------------------------------------
# search_and_import orchestration
# ---------------------------------------------------------------------------


def test_search_and_import_no_results():
    service = SearxngImageSearchService(adapter=FakeAdapter([]))
    outcome = asyncio.run(service.search_and_import("nothing matches this"))
    assert outcome.asset is None
    assert outcome.failure_code == FAILURE_NO_RESULTS


def test_search_and_import_without_importer_never_succeeds():
    adapter = FakeAdapter([_candidate("https://a.org/1.jpg")])
    service = SearxngImageSearchService(adapter=adapter)
    outcome = asyncio.run(service.search_and_import("q"))
    assert outcome.failure_code == FAILURE_IMPORTER_UNAVAILABLE


def test_search_and_import_tries_next_candidate_on_failure():
    candidates = [
        _candidate("https://bad.org/1.jpg", engine="pexels"),
        _candidate("https://good.org/2.jpg", engine="flickr"),
    ]
    importer = FakeImporter(fail_domains={"bad.org"})
    service = SearxngImageSearchService(
        adapter=FakeAdapter(candidates), importer=importer
    )
    outcome = asyncio.run(service.search_and_import("q"))
    assert outcome.asset is not None
    assert importer.imported[0].image_url == "https://good.org/2.jpg"


def test_search_and_import_respects_candidate_attempt_bound():
    candidates = [
        _candidate(f"https://bad.org/{i}.jpg", engine="pexels") for i in range(10)
    ]
    importer = FakeImporter(fail_domains={"bad.org"})
    service = SearxngImageSearchService(
        adapter=FakeAdapter(candidates), importer=importer
    )
    outcome = asyncio.run(service.search_and_import("q"))
    # Generic importer failures classify as a download failure (Phase 7):
    # the first attempted candidate's reason becomes the slot warning code.
    assert outcome.failure_code == FAILURE_DOWNLOAD_FAILED
    assert len(importer.imported) == 0


def test_search_and_import_records_deck_context_on_success():
    context = DeckResolutionContext()
    candidates = [_candidate("https://a.org/1.jpg")]
    service = SearxngImageSearchService(
        adapter=FakeAdapter(candidates), importer=FakeImporter()
    )
    asyncio.run(service.search_and_import("q", context=context))
    assert context.is_selected("https://a.org/1.jpg")
    assert context.domain_counts["a.org"] == 1


def test_deck_context_prevents_duplicate_image_across_slots():
    candidates = [_candidate("https://a.org/1.jpg")]
    context = DeckResolutionContext()
    context.record("https://a.org/1.jpg", "a.org")
    service = SearxngImageSearchService(
        adapter=FakeAdapter(candidates), importer=FakeImporter()
    )
    outcome = asyncio.run(service.search_and_import("q", context=context))
    assert outcome.asset is None
    assert outcome.failure_code == FAILURE_NO_RESULTS