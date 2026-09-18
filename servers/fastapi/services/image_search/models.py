"""Typed contracts for the automatic web image-search pipeline.

``ImageSearchService`` is provider-neutral even though the first (and
currently only) adapter is SearXNG. ``ImageSearchCandidate`` mirrors the
fields actually returned by the SearXNG ``images`` category — see
``tests/fixtures/searxng/README.md`` for observed field semantics and quirks.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from models.sql.image_asset import ImageAsset


@dataclass(frozen=True)
class ImageSearchCandidate:
    """One normalized, deduplicated search result for a full image."""

    title: str
    image_url: str
    thumbnail_url: Optional[str] = None
    source_page_url: Optional[str] = None
    engine: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class DeckResolutionContext:
    """Presentation-scoped deduplication state for one generation job.

    Tracks already-selected image URLs and source domains so multiple image
    slots do not resolve to the same picture or to one dominant domain.
    """

    selected_urls: set[str] = field(default_factory=set)
    domain_counts: dict[str, int] = field(default_factory=dict)

    def record(self, image_url: str, domain: str) -> None:
        self.selected_urls.add(image_url)
        self.domain_counts[domain] = self.domain_counts.get(domain, 0) + 1

    def is_selected(self, image_url: str) -> bool:
        return image_url in self.selected_urls


class ImageSearchService(ABC):
    """Provider-neutral automatic image-search interface (FR-2).

    Implementations search the public web for existing images matching a
    query. They never generate images and never fall through to an AI image
    generator: on failure the caller shows a placeholder and a coded warning.
    """

    @abstractmethod
    async def search(
        self,
        query: str,
        *,
        limit: int,
        aspect_ratio: Optional[float] = None,
    ) -> list[ImageSearchCandidate]:
        """Return ranked, deduplicated candidates for ``query``.

        ``limit`` caps the number of candidates; ``aspect_ratio`` (width /
        height) is a ranking hint when the slot has a target shape.
        """

    @abstractmethod
    async def search_and_import(
        self,
        query: str,
        *,
        owner_id=None,
        aspect_ratio: Optional[float] = None,
        context: Optional[DeckResolutionContext] = None,
    ) -> "ImageSearchOutcome":
        """Search, download and persist the first usable candidate.

        On success the returned outcome holds a persisted, owner-scoped
        ``ImageAsset`` with a local ``/app_data/...`` URL plus source
        metadata in ``extras``. On failure the outcome carries a coded
        reason and no asset — the caller must fall back to a placeholder.
        """


class ImageCandidateImporter(ABC):
    """Secure remote-image import boundary (FR-6).

    Implemented in Phase 4 (SSRF-safe download, validation, re-encoding,
    owner-scoped ``ImageAsset`` persistence). The service treats any
    exception as "candidate unusable" and moves to the next one.
    """

    @abstractmethod
    async def import_candidate(
        self,
        candidate: ImageSearchCandidate,
        *,
        owner_id=None,
        query: str | None = None,
    ) -> "ImageAsset":
        """Download, validate and persist one candidate.

        ``query`` is the normalized search query that produced the
        candidate, stored in ``ImageAsset.extras`` (FR-7). Raises on any
        failure (unsafe destination, bad bytes, timeouts); the caller
        never retries the same candidate.
        """


@dataclass(frozen=True)
class ImageSearchOutcome:
    """Result of one ``search_and_import`` attempt for a single slot."""

    asset: Optional["ImageAsset"] = None
    # Machine-readable failure reason (Phase 7 warning code), e.g.
    # "image_search.no_results", "image_search.download_failed", ...
    failure_code: Optional[str] = None
    # Human-irrelevant detail kept for logs only; never shown raw to users.
    detail: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.asset is not None