"""Shared SearXNG connection helpers.

Both pipelines talk to the same self-hosted SearXNG instance but stay
independent (spec NFR-1): text research routes through ``utils.web_search``,
automatic image search through ``services.image_search``. This module holds
the connection-level pieces they share so behavior cannot drift:

- base/``/search`` URL normalization (``build_searxng_search_url``)
- required-configuration enforcement (``SEARXNG_BASE_URL``)
- JSON response validation with bounded error bodies
- timeout construction
- credential-redacted logging

Nothing here knows about text vs. images; callers pass their own query
parameters.
"""

import logging
from urllib.parse import urlparse, urlunparse

import aiohttp
from fastapi import HTTPException

from utils.get_env import get_searxng_base_url_env

LOGGER = logging.getLogger(__name__)

# Shared User-Agent for every SearXNG request (text and images).
SEARXNG_USER_AGENT = "Presenton/1.0"

# Default total request timeout, mirroring the pre-extraction text-search
# behavior. The image-search adapter uses its own (bounded) timeout.
DEFAULT_SEARXNG_TIMEOUT_SECONDS = 15.0


def get_searxng_base_url() -> str | None:
    """Configured SearXNG base URL, or ``None`` when unset/blank."""
    return (get_searxng_base_url_env() or "").strip() or None


def build_searxng_search_url(base_url: str | None = None) -> str:
    """Normalize a base (or already-``/search``) URL into a ``/search`` URL.

    Accepts ``http://searxng:8080`` and ``http://searxng:8080/search`` alike;
    query/fragment are dropped. Raises ``HTTPException(400)`` when no base
    URL is configured, mirroring the pre-extraction text-search contract.
    """
    configured_url = base_url if base_url is not None else get_searxng_base_url()
    if not configured_url:
        raise HTTPException(status_code=400, detail="SEARXNG_BASE_URL is not configured")
    parsed = urlparse(configured_url)
    path = parsed.path.rstrip("/")
    if not path.endswith("/search"):
        path = f"{path}/search"
    return urlunparse(parsed._replace(path=path, params="", query="", fragment=""))


def redact_searxng_url_credentials(value: str) -> str:
    """Mask ``user:pass@`` in a URL before it reaches logs."""
    parsed = urlparse(value)
    if not parsed.username and not parsed.password:
        return value
    hostname = parsed.hostname or ""
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    return urlunparse(parsed._replace(netloc=f"***:***@{hostname}"))


async def parse_searxng_json_response(
    response: aiohttp.ClientResponse,
    *,
    error_prefix: str = "Web search request failed",
) -> dict:
    """Validate one SearXNG HTTP response into a JSON dict.

    Error bodies are truncated to 500 characters so upstream pages never
    reach users or logs verbatim. Non-dict payloads degrade to ``{}``
    (callers treat them as "no results").
    """
    if response.status >= 400:
        detail = (await response.text())[:500]
        raise HTTPException(response.status, detail=f"{error_prefix}: {detail}")
    payload = await response.json(content_type=None)
    return payload if isinstance(payload, dict) else {}


def make_searxng_client_timeout(total_seconds: float = DEFAULT_SEARXNG_TIMEOUT_SECONDS):
    """Bounded ``aiohttp`` timeout for one SearXNG request."""
    return aiohttp.ClientTimeout(total=total_seconds)


def log_searxng_request(search_url: str, **context: object) -> None:
    """Info-log a SearXNG request with credentials redacted."""
    LOGGER.info(
        "Using SearXNG instance: search_url=%s%s",
        redact_searxng_url_credentials(search_url),
        "".join(f" {key}={value}" for key, value in context.items()),
    )