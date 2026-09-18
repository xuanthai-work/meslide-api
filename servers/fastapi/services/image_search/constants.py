"""Named, non-magic limits for the automatic web image-search feature.

Every bound used by the image-search pipeline is defined here once and
referenced by name everywhere else (adapter, downloader, resolver). Values
follow the NFR-2 targets in
``docs/gslide-searxng-image-search-spec.md`` (web repo):
search <= 10s per query, import <= 20s per candidate, brief caching,
bounded concurrency and bounded download sizes.
"""

from dataclasses import dataclass

from constants.documents import IMAGE_MIME_TYPES

# Re-encoding target for imported images (approved raster format, no SVG).
APPROVED_RASTER_FORMAT = "jpeg"
RE_ENCODE_QUALITY = 90

# Stable status codes streamed while slide images resolve (FR-10). The status
# bar translates these; the English text is only the legacy fallback.
IMAGE_STATUS_SEARCHING_WEB = "images.searching_web"
IMAGE_STATUS_IMPORTING = "images.importing"
IMAGE_STATUS_TEXT = {
    IMAGE_STATUS_SEARCHING_WEB: "Searching the web for slide images",
    IMAGE_STATUS_IMPORTING: "Importing slide images",
}

# Upload-validated image mime types accepted for import (aligned with
# constants/documents.py IMAGE_MIME_TYPES). SVG is rejected in the first
# release; formats here are what Pillow is allowed to decode.
IMPORTABLE_IMAGE_MIME_TYPES = frozenset(IMAGE_MIME_TYPES)


@dataclass(frozen=True)
class ImageSearchLimits:
    """Bounded defaults for the automatic image-search pipeline."""

    # --- Query contract -------------------------------------------------
    # Maximum normalized query length sent to SearXNG.
    max_query_length: int = 200

    # --- Search -----------------------------------------------------------
    # Hard cap on parsed candidates kept per query.
    max_results_per_query: int = 30
    # Search request timeout (NFR-2: at most 10 seconds per query).
    search_timeout_seconds: float = 10.0
    # Cache TTL for identical normalized queries (brief; includes limit,
    # aspect ratio, safe-search value and adapter version in the key).
    search_cache_ttl_seconds: float = 300.0
    # Maximum entries kept in the query cache.
    search_cache_max_entries: int = 256

    # --- Candidate selection ---------------------------------------------
    # Candidates attempted (download + validate) per image slot, in rank order.
    max_candidates_attempted: int = 3
    # Minimum usable size for ranking (smaller is only used if nothing else).
    min_usable_width: int = 400
    min_usable_height: int = 300
    # Aspect-ratio tolerance for the "close to target slot" ranking signal.
    aspect_ratio_tolerance: float = 0.35

    # --- Download / import (NFR-2: at most 20 seconds per candidate) ------
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 10.0
    import_total_timeout_seconds: float = 20.0
    # Each redirect hop is revalidated; redirect count is bounded.
    max_redirects: int = 3
    # Hard cap on streamed bytes per candidate.
    max_download_bytes: int = 20 * 1024 * 1024
    # Pillow decompression-bomb guard (max total pixels).
    max_image_pixels: int = 40_000_000

    # --- Concurrency -------------------------------------------------------
    # Global cap across the process.
    max_concurrent_imports: int = 8
    # Per-presentation cap within one generation job.
    max_concurrent_imports_per_presentation: int = 2

    # --- Smart HTML slots ----------------------------------------------------
    # Maximum data-image-query <img> slots resolved per Smart slide; extra
    # slots keep their placeholder and lose the marker before persistence.
    max_slots_per_smart_slide: int = 6


#: The single locked limits instance referenced by the whole pipeline.
limits = ImageSearchLimits()