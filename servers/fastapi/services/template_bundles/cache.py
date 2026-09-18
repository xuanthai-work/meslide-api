"""Bounded process-local LRU/TTL cache for decoded template bundles.

Cache key is the immutable bundle checksum (``bundle_sha256``): revisions
never change content, so entries need no invalidation — only size and age
bounds.
"""

import time
from collections import OrderedDict

from services.template_bundles.bundle import TemplateBundle

DEFAULT_MAX_ENTRIES = 128
DEFAULT_TTL_SECONDS = 300.0


class BundleCache:
    def __init__(
        self,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ):
        self._max_entries = max(1, max_entries)
        self._ttl_seconds = ttl_seconds
        self._entries: OrderedDict[str, tuple[TemplateBundle, float]] = OrderedDict()

    def get(self, sha256: str | None) -> TemplateBundle | None:
        if not sha256:
            return None
        entry = self._entries.get(sha256)
        if entry is None:
            return None
        bundle, expires_at = entry
        if self._ttl_seconds <= 0 or time.monotonic() >= expires_at:
            self._entries.pop(sha256, None)
            return None
        self._entries.move_to_end(sha256)
        return bundle

    def put(self, sha256: str | None, bundle: TemplateBundle) -> None:
        if not sha256:
            return
        self._entries[sha256] = (bundle, time.monotonic() + self._ttl_seconds)
        self._entries.move_to_end(sha256)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


_default_cache: BundleCache | None = None


def get_template_bundle_cache() -> BundleCache:
    """Process-wide shared cache; repositories default to this instance."""
    global _default_cache
    if _default_cache is None:
        _default_cache = BundleCache()
    return _default_cache


__all__ = ["BundleCache", "get_template_bundle_cache"]