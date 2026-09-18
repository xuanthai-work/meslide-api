"""SearXNG adapter for the `images` category (FR-3).

Talks to the shared self-hosted SearXNG instance through
``utils.searxng`` connection helpers and normalizes the response — whose
field quirks are documented in ``tests/fixtures/searxng/README.md`` — into
``ImageSearchCandidate`` objects. The adapter knows nothing about ranking,
deck deduplication, or downloads.
"""

import logging
import re
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import aiohttp

from services.image_search.constants import limits
from services.image_search.models import ImageSearchCandidate
from utils.searxng import (
    SEARXNG_USER_AGENT,
    build_searxng_search_url,
    make_searxng_client_timeout,
    parse_searxng_json_response,
)

LOGGER = logging.getLogger(__name__)

# Bumped whenever parsing/canonicalization changes; part of the cache key so
# stale entries never survive an adapter behavior change.
ADAPTER_VERSION = 1

# Mandatory image-search safe-search level (spec FR-3).
IMAGE_SEARCH_SAFESEARCH = 1

# resolution variants observed across engines: "824x464" (google cse),
# "1000×431" (bing, U+00D7), "800 x 609" (flickr), "1181 x 536" (wikicommons).
_RESOLUTION_PATTERN = re.compile(r"(\d{2,5})\s*[x×]\s*(\d{2,5})")

# Tracking parameters stripped during URL canonicalization.
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset(
    {"fbclid", "gclid", "ref", "ref_src", "ref_url", "si", "output"}
)

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def canonicalize_url(value: Any) -> Optional[str]:
    """Normalize one result URL or return ``None`` if unusable.

    Handles the observed quirks: protocol-relative flickr URLs, tracking
    parameters, mixed-case hosts, fragments. Returns ``None`` for anything
    that is not a plain http(s) URL so the caller can skip it.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.startswith("//"):
        text = f"https:{text}"
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES or not parsed.netloc:
        return None
    kept_params = [
        (key, val)
        for key, val in parse_qsl(parsed.query, keep_blank_values=False)
        if not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
        and key.lower() not in _TRACKING_PARAMS
    ]
    return urlunparse(
        parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            query=urlencode(kept_params),
            fragment="",
        )
    )


def _parse_resolution(value: Any) -> tuple[Optional[int], Optional[int]]:
    if not isinstance(value, str):
        return None, None
    match = _RESOLUTION_PATTERN.search(value)
    if not match:
        return None, None
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0:
        return None, None
    return width, height


def _candidate_from_result(item: dict) -> Optional[ImageSearchCandidate]:
    image_url = canonicalize_url(item.get("img_src"))
    if image_url is None:
        return None
    width, height = _parse_resolution(item.get("resolution"))
    return ImageSearchCandidate(
        title=str(item.get("title") or ""),
        image_url=image_url,
        thumbnail_url=canonicalize_url(item.get("thumbnail_src")),
        source_page_url=canonicalize_url(item.get("url")),
        engine=(str(item.get("engine")) if item.get("engine") else None),
        width=width,
        height=height,
    )


def parse_searxng_image_results(
    payload: dict, limit: int
) -> list[ImageSearchCandidate]:
    """Normalize a SearXNG JSON payload into deduplicated candidates.

    Tolerates missing optional fields, drops results without a usable
    ``img_src``, and deduplicates by canonical image URL (first occurrence
    wins). Capped at ``limit``.
    """
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    candidates: list[ImageSearchCandidate] = []
    seen_urls: set[str] = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        candidate = _candidate_from_result(item)
        if candidate is None or candidate.image_url in seen_urls:
            continue
        seen_urls.add(candidate.image_url)
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


class SearxngImageSearchAdapter:
    """Fetches ranked-source candidates from the configured SearXNG."""

    async def fetch(
        self, normalized_query: str, *, limit: int
    ) -> list[ImageSearchCandidate]:
        search_url = build_searxng_search_url()
        params = {
            "q": normalized_query,
            "format": "json",
            "categories": "images",
            "safesearch": str(IMAGE_SEARCH_SAFESEARCH),
        }
        timeout = make_searxng_client_timeout(limits.search_timeout_seconds)
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers={"User-Agent": SEARXNG_USER_AGENT},
        ) as session:
            async with session.get(search_url, params=params) as response:
                payload = await parse_searxng_json_response(
                    response, error_prefix="Image search request failed"
                )
        return parse_searxng_image_results(payload, limit)