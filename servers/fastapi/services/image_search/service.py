"""`ImageSearchService` implementation over SearXNG (FR-2).

Orchestration only: the adapter fetches, the cache memoizes, the ranker
orders, the importer (Phase 4) persists. The service never touches image
generation and never falls through to a generator: failures surface as a
coded `ImageSearchOutcome` the resolver turns into a placeholder.

Every resolution emits one structured ``image_search_slot`` metrics line
(Phase 8): latencies, candidate counts, imported bytes, selected
engine/domain, outcome. Queries are never logged or carried in outcome
details verbatim — only their hash (``metrics.hash_query_for_log``).
"""

import asyncio
import logging
import time

from services.image_search.availability import get_web_image_search_max_results
from services.image_search.cache import ImageSearchCache
from services.image_search.constants import (
    IMAGE_STATUS_IMPORTING,
    IMAGE_STATUS_SEARCHING_WEB,
    limits,
)
from services.image_search.downloader import ImageImportError
from services.image_search.metrics import (
    OUTCOME_CANCELLED,
    OUTCOME_IMPORTED,
    OUTCOME_PLACEHOLDER,
    ImageSearchSlotMetrics,
    emit_slot_metrics,
    hash_query_for_log,
)
from services.image_search.models import (
    DeckResolutionContext,
    ImageCandidateImporter,
    ImageSearchCandidate,
    ImageSearchOutcome,
    ImageSearchService,
)
from services.image_search.query import normalize_image_query
from services.image_search.ranking import candidate_domain, rank_candidates
from services.image_search.searxng import SearxngImageSearchAdapter

LOGGER = logging.getLogger(__name__)

# Machine-readable failure reasons, one per user-visible warning (Phase 7).
FAILURE_EMPTY_QUERY = "image_search.empty_query"
FAILURE_NO_RESULTS = "image_search.no_results"
FAILURE_SEARCH_TIMEOUT = "image_search.search_timeout"
FAILURE_IMPORT_TIMEOUT = "image_search.import_timeout"
FAILURE_DOWNLOAD_FAILED = "image_search.download_failed"
FAILURE_CANDIDATE_REJECTED = "image_search.candidate_rejected"
FAILURE_IMPORTER_UNAVAILABLE = "image_search.importer_unavailable"
FAILURE_NO_USABLE_CANDIDATE = "image_search.no_usable_candidate"
# Unexpected failure: the slot keeps its placeholder, nothing else changes.
FAILURE_PLACEHOLDER_FALLBACK = "image_search.placeholder_fallback"


class SearxngImageSearchService(ImageSearchService):
    def __init__(
        self,
        adapter: SearxngImageSearchAdapter | None = None,
        cache: ImageSearchCache | None = None,
        importer: ImageCandidateImporter | None = None,
    ):
        self._adapter = adapter or SearxngImageSearchAdapter()
        self._cache = cache or ImageSearchCache()
        self._importer = importer

    async def search(
        self,
        query: str,
        *,
        limit: int,
        aspect_ratio: float | None = None,
        context: DeckResolutionContext | None = None,
        stats: dict | None = None,
    ) -> list[ImageSearchCandidate]:
        """Normalized query -> cached fetch -> ranked candidates.

        Caching sits between fetch and ranking: the fetched list is shared
        across slots, while deck deduplication/ranking stays per-call so
        one deck never sees the same image twice even on a cache hit.
        ``stats`` (optional, caller-owned) records the cache hit and the
        adapter fetch latency for the Phase 8 metrics line.
        """
        normalized = normalize_image_query(query)
        if not normalized:
            return []
        bounded_limit = max(1, min(limit, get_web_image_search_max_results()))
        key = ImageSearchCache.make_key(
            normalized, limit=bounded_limit, aspect_ratio=aspect_ratio
        )
        fetched = self._cache.get(key)
        if fetched is None:
            started = time.perf_counter()
            fetched = await self._adapter.fetch(normalized, limit=bounded_limit)
            if stats is not None:
                stats["fetch_latency_ms"] = int((time.perf_counter() - started) * 1000)
                stats["cached"] = False
            self._cache.put(key, fetched)
        elif stats is not None:
            stats["cached"] = True
        ranked = rank_candidates(
            list(fetched), aspect_ratio=aspect_ratio, context=context
        )
        if stats is not None:
            stats.setdefault("cached", False)
            stats["candidates_returned"] = len(ranked)
        return ranked

    async def search_and_import(
        self,
        query: str,
        *,
        owner_id=None,
        aspect_ratio: float | None = None,
        context: DeckResolutionContext | None = None,
        on_status=None,
    ) -> ImageSearchOutcome:
        """Search SearXNG, then import the first usable candidate.

        ``on_status`` (optional, sync) receives the stable Phase 7 status
        codes so the streaming pipelines can report image-search progress
        without blocking slide text. Cancelling the returned task still
        emits a metrics line with ``outcome=cancelled`` before the
        ``CancelledError`` propagates to the stream pump.
        """
        if on_status is not None:
            on_status(IMAGE_STATUS_SEARCHING_WEB)
        metrics = ImageSearchSlotMetrics(
            query_hash=hash_query_for_log(query), outcome=OUTCOME_PLACEHOLDER
        )
        try:
            outcome = await self._resolve(
                query,
                owner_id=owner_id,
                aspect_ratio=aspect_ratio,
                context=context,
                on_status=on_status,
                metrics=metrics,
            )
        except asyncio.CancelledError:
            metrics.outcome = OUTCOME_CANCELLED
            emit_slot_metrics(metrics)
            raise
        if outcome.ok:
            metrics.outcome = OUTCOME_IMPORTED
        emit_slot_metrics(metrics)
        return outcome

    async def _resolve(
        self,
        query: str,
        *,
        owner_id,
        aspect_ratio: float | None,
        context: DeckResolutionContext | None,
        on_status,
        metrics: "ImageSearchSlotMetrics",
    ) -> ImageSearchOutcome:
        search_stats: dict = {}
        try:
            candidates = await self.search(
                query,
                limit=get_web_image_search_max_results(),
                aspect_ratio=aspect_ratio,
                context=context,
                stats=search_stats,
            )
        except TimeoutError:
            LOGGER.warning("Image search timed out: query_hash=%s", metrics.query_hash)
            metrics.failure_code = FAILURE_SEARCH_TIMEOUT
            return ImageSearchOutcome(failure_code=FAILURE_SEARCH_TIMEOUT)
        except Exception:
            LOGGER.warning(
                "Image search failed: query_hash=%s", metrics.query_hash, exc_info=True
            )
            metrics.failure_code = FAILURE_PLACEHOLDER_FALLBACK
            return ImageSearchOutcome(failure_code=FAILURE_PLACEHOLDER_FALLBACK)
        metrics.cached = search_stats.get("cached")
        metrics.fetch_latency_ms = search_stats.get("fetch_latency_ms")
        metrics.candidates_returned = search_stats.get(
            "candidates_returned", len(candidates)
        )
        if not candidates:
            metrics.failure_code = FAILURE_NO_RESULTS
            return ImageSearchOutcome(failure_code=FAILURE_NO_RESULTS)
        if self._importer is None:
            # Rollout safety: an enabled feature without its import path must
            # not fall back to AI image generation — placeholder + warning.
            metrics.failure_code = FAILURE_IMPORTER_UNAVAILABLE
            return ImageSearchOutcome(
                failure_code=FAILURE_IMPORTER_UNAVAILABLE,
                detail="no image importer configured",
            )
        attempted = 0
        normalized = normalize_image_query(query)
        first_failure_code: str | None = None
        for candidate in candidates:
            if attempted >= limits.max_candidates_attempted:
                break
            if context is not None and context.is_selected(candidate.image_url):
                continue
            if attempted == 0 and on_status is not None:
                on_status(IMAGE_STATUS_IMPORTING)
            attempted += 1
            metrics.candidates_attempted = attempted
            started = time.perf_counter()
            try:
                asset = await self._importer.import_candidate(
                    candidate, owner_id=owner_id, query=normalized
                )
            except asyncio.CancelledError:
                raise
            except ImageImportError as error:
                metrics.candidates_rejected = (
                    metrics.candidates_rejected or 0
                ) + 1
                if first_failure_code is None:
                    first_failure_code = error.reason or FAILURE_DOWNLOAD_FAILED
                LOGGER.warning(
                    "Image candidate import failed: engine=%s url_domain=%s reason=%s",
                    candidate.engine,
                    candidate_domain(candidate),
                    error.reason,
                    exc_info=True,
                )
                continue
            except TimeoutError:
                metrics.candidates_rejected = (
                    metrics.candidates_rejected or 0
                ) + 1
                if first_failure_code is None:
                    first_failure_code = FAILURE_IMPORT_TIMEOUT
                LOGGER.warning(
                    "Image candidate import timed out: engine=%s url_domain=%s",
                    candidate.engine,
                    candidate_domain(candidate),
                    exc_info=True,
                )
                continue
            except Exception:
                metrics.candidates_rejected = (
                    metrics.candidates_rejected or 0
                ) + 1
                if first_failure_code is None:
                    first_failure_code = FAILURE_DOWNLOAD_FAILED
                LOGGER.warning(
                    "Image candidate import failed: engine=%s url_domain=%s",
                    candidate.engine,
                    candidate_domain(candidate),
                    exc_info=True,
                )
                continue
            finally:
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                metrics.import_latency_ms = (metrics.import_latency_ms or 0) + elapsed_ms
            metrics.selected_engine = candidate.engine
            metrics.selected_domain = candidate_domain(candidate)
            extras = getattr(asset, "extras", None)
            if extras and isinstance(extras.get("size_bytes"), int):
                metrics.imported_bytes = extras["size_bytes"]
            if context is not None:
                context.record(candidate.image_url, candidate_domain(candidate))
            return ImageSearchOutcome(asset=asset)
        metrics.candidates_attempted = attempted
        metrics.failure_code = first_failure_code or FAILURE_NO_USABLE_CANDIDATE
        return ImageSearchOutcome(
            failure_code=metrics.failure_code, detail=f"attempted={attempted}"
        )
