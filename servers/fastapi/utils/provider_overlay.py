"""Per-request provider overlay.

Stores a small JSON-shaped dict in a ContextVar so that LLM/image/search keys
saved by the current authenticated user take precedence over process env, without
mutating ``os.environ``. Use ``set_provider_overlay`` inside a request and
``reset_provider_overlay`` in the matching ``finally`` block.
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from typing import Any

# Names whose getters in ``utils.get_env`` should look at the overlay first.
# Names NOT in this list (CAN_CHANGE_KEYS, DATABASE_URL, APP_DATA_DIRECTORY,
# DISABLE_AUTH, SEARXNG_BASE_URL, ENABLE_PARALLEL_IMAGE_GENERATION,
# USER_CONFIG_PATH, AUTH_*, ...) keep reading ``os.environ`` only.
_OVERLAYED_ENV_NAMES: frozenset[str] = frozenset(
    {
        "LLM",
        "OPENAI_API_KEY",
        "OPENAI_MODEL",
        "GOOGLE_API_KEY",
        "GOOGLE_MODEL",
        "CUSTOM_LLM_URL",
        "CUSTOM_LLM_API_KEY",
        "CUSTOM_MODEL",
        "IMAGE_PROVIDER",
        "DISABLE_IMAGE_GENERATION",
        "DISABLE_THINKING",
        "EXTENDED_REASONING",
        "WEB_GROUNDING",
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_MAX_RESULTS",
        "GPT_IMAGE_1_5_QUALITY",
        "OPENAI_COMPAT_IMAGE_BASE_URL",
        "OPENAI_COMPAT_IMAGE_API_KEY",
        "OPENAI_COMPAT_IMAGE_MODEL",
    }
)


_provider_overlay: ContextVar[dict[str, Any] | None] = ContextVar(
    "gslide_provider_overlay", default=None
)


def get_provider_overlay() -> dict[str, Any] | None:
    """Return the current request's overlay dict, or ``None`` outside a request."""
    return _provider_overlay.get()


def set_provider_overlay(config: dict[str, Any] | None) -> Token:
    """Set the overlay for this request. Use ``reset_provider_overlay`` to clear."""
    return _provider_overlay.set(config)


def reset_provider_overlay(token: Token) -> None:
    """Reset the overlay back to the previous state for this request."""
    _provider_overlay.reset(token)


def is_overlaid_env(name: str) -> bool:
    """Whether the named env var is eligible for overlay."""
    return name in _OVERLAYED_ENV_NAMES


def overlay_or_env(name: str) -> str | None:
    """Return the overlay value when set and non-blank, else ``os.getenv(name)``.

    Only overlaid names are read from the overlay; other names always come from
    process env. ``None`` is returned when the value is unset or blank in both.
    """
    overlay = _provider_overlay.get()
    if overlay is not None and name in _OVERLAYED_ENV_NAMES:
        raw = overlay.get(name)
        if raw is not None and str(raw).strip() != "":
            return str(raw)
    return os.getenv(name)
