"""Availability and rollout gating for automatic web image search.

Two independent operator facts, kept separate because the Standard/Smart
resolvers need to distinguish them (plan Phase 5):

- ``web_image_search_feature_enabled()`` — the ``WEB_IMAGE_SEARCH_ENABLED``
  rollout gate. ``false`` (default) preserves the complete pre-feature
  image behavior for every presentation, even those with
  ``presentation.web_search=true``.
- ``web_image_search_configured()`` — a usable ``SEARXNG_BASE_URL`` exists.

An enabled feature whose search is unavailable or fails must NOT fall back
to AI image generation (spec 3.3): that case resolves to a placeholder with
a coded warning.
"""

from utils.get_env import (
    get_web_image_search_enabled_env,
    get_web_image_search_max_results_env,
)
from utils.parsers import parse_bool_or_none
from utils.searxng import get_searxng_base_url

from services.image_search.constants import limits


def web_image_search_feature_enabled() -> bool:
    """Operator rollout gate; default (unset) is disabled."""
    return parse_bool_or_none(get_web_image_search_enabled_env()) or False


def web_image_search_configured() -> bool:
    """A SearXNG instance is configured for image search."""
    return get_searxng_base_url() is not None


def web_image_search_available() -> bool:
    """Rollout gate on AND SearXNG configured."""
    return web_image_search_feature_enabled() and web_image_search_configured()


def get_web_image_search_max_results() -> int:
    """Effective per-query candidate cap, bounded by the locked limits.

    Unset/garbage falls back to the built-in default; the operator value can
    only narrow the bound, never widen it.
    """
    raw = (get_web_image_search_max_results_env() or "").strip()
    if not raw:
        return limits.max_results_per_query
    try:
        requested = int(raw)
    except ValueError:
        return limits.max_results_per_query
    return max(1, min(requested, limits.max_results_per_query))