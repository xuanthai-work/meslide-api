"""Brief in-memory cache for image searches (NFR-2).

Keyed by (normalized query, limit, aspect ratio, safe-search value, adapter
version) so a behavior change invalidates old entries and different slot
shapes never share results. Entries expire after the locked TTL; the cache
evicts oldest-first beyond ``max_entries``. Process-local and best-effort:
a miss is only a slower query.
"""

import time
from collections import deque

from services.image_search.constants import limits
from services.image_search.searxng import ADAPTER_VERSION, IMAGE_SEARCH_SAFESEARCH


class ImageSearchCache:
    def __init__(
        self,
        *,
        ttl_seconds: float = limits.search_cache_ttl_seconds,
        max_entries: int = limits.search_cache_max_entries,
        clock=time.monotonic,
    ):
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: dict[tuple, tuple[float, list]] = {}
        self._insert_order: deque[tuple] = deque()

    @staticmethod
    def make_key(
        normalized_query: str,
        *,
        limit: int,
        aspect_ratio: float | None,
    ) -> tuple:
        return (
            normalized_query,
            limit,
            round(aspect_ratio, 4) if aspect_ratio else None,
            IMAGE_SEARCH_SAFESEARCH,
            ADAPTER_VERSION,
        )

    def get(self, key: tuple) -> list | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        stored_at, value = entry
        if self._clock() - stored_at > self._ttl_seconds:
            # Lazy expiry: drop the stale entry so it cannot be hit again.
            self._entries.pop(key, None)
            return None
        return value

    def put(self, key: tuple, value: list) -> None:
        if key in self._entries:
            self._entries[key] = (self._clock(), value)
            return
        if len(self._entries) >= self._max_entries:
            evicted = self._insert_order.popleft()
            self._entries.pop(evicted, None)
        self._entries[key] = (self._clock(), value)
        self._insert_order.append(key)

    def clear(self) -> None:
        self._entries.clear()
        self._insert_order.clear()