"""Coded warning payloads for failed image-search slots (FR-8/FR-10).

Mirrors the shape produced by ``utils.image_generation_error.
image_generation_warning`` so the existing per-slide ``warnings`` SSE
channel and frontend toast logic work unchanged. Localization keys live in
the web catalogs (Phase 7); ``detail`` here is the English fallback string.
"""

from services.image_search.downloader import (
    IMPORT_FAILURE_DOWNLOAD,
    IMPORT_FAILURE_REJECTED,
)
from services.image_search.service import (
    FAILURE_CANDIDATE_REJECTED,
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_EMPTY_QUERY,
    FAILURE_IMPORT_TIMEOUT,
    FAILURE_IMPORTER_UNAVAILABLE,
    FAILURE_NO_RESULTS,
    FAILURE_NO_USABLE_CANDIDATE,
    FAILURE_PLACEHOLDER_FALLBACK,
    FAILURE_SEARCH_TIMEOUT,
)

# Rollout flag on but SEARXNG_BASE_URL missing: placeholder + coded warning,
# never a silent fall-through to paid image generation.
IMAGE_SEARCH_NOT_CONFIGURED_CODE = "image_search.not_configured"
# Rollout flag off. Generation stays completely silent for flag-off decks
# (spec 3.3: feature gating, not a search failure); this code exists for
# operator diagnostics so a disabled feature is reported as disabled.
IMAGE_SEARCH_DISABLED_CODE = "image_search.disabled"

# English fallback strings. Every message names the real outcome: the slot
# keeps a placeholder, and the slide text is unaffected either way.
_PLACEHOLDER_DETAILS = {
    FAILURE_EMPTY_QUERY: "No usable image search query was produced for this slot.",
    FAILURE_NO_RESULTS: (
        "Web image search returned no results, so this slide keeps a placeholder."
    ),
    FAILURE_SEARCH_TIMEOUT: (
        "Web image search timed out, so this slide keeps a placeholder."
    ),
    FAILURE_IMPORT_TIMEOUT: (
        "Downloading a web image timed out, so this slide keeps a placeholder."
    ),
    FAILURE_DOWNLOAD_FAILED: (
        "A web image could not be downloaded, so this slide keeps a placeholder."
    ),
    FAILURE_CANDIDATE_REJECTED: (
        "Web image results were rejected for safety or format reasons, so this "
        "slide keeps a placeholder."
    ),
    IMPORT_FAILURE_REJECTED: (
        "Web image results were rejected for safety or format reasons, so this "
        "slide keeps a placeholder."
    ),
    IMPORT_FAILURE_DOWNLOAD: (
        "A web image could not be downloaded, so this slide keeps a placeholder."
    ),
    FAILURE_IMPORTER_UNAVAILABLE: (
        "Web image import is not available right now, so this slide keeps a "
        "placeholder."
    ),
    FAILURE_NO_USABLE_CANDIDATE: (
        "No downloadable image could be imported from the search results, so "
        "this slide keeps a placeholder."
    ),
    FAILURE_PLACEHOLDER_FALLBACK: (
        "Web image search could not provide an image, so this slide keeps a "
        "placeholder."
    ),
    IMAGE_SEARCH_NOT_CONFIGURED_CODE: (
        "Web image search is not configured on this server, so slides keep "
        "placeholders."
    ),
    IMAGE_SEARCH_DISABLED_CODE: "Web image search is disabled on this server.",
}


def image_search_warning(failure_code: str) -> dict:
    """Same channel/shape as ``image_generation_warning`` for search failures."""
    return {
        "status_code": 200,
        "detail": _PLACEHOLDER_DETAILS.get(
            failure_code,
            "Web image search could not provide an image for this slot.",
        ),
        "code": None,
        "error_code": failure_code,
    }