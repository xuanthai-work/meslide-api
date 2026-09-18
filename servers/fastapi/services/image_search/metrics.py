"""Observability for the automatic web image-search pipeline (plan Phase 8).

No metrics daemon exists in this service, so every metric is emitted as one
structured ``LOGGER.info`` line per resolved slot (``image_search_slot``).
Operators aggregate the fields with any log pipeline. Metrics cover:
search latency, candidates returned/rejected, import latency, imported
bytes, selected engine/domain, placeholder outcomes (placeholder rate),
and cancellations.

Sensitive queries are never logged verbatim. Query identifiers are always
reduced to a short SHA-256 prefix so operational logs cannot expose the source
text.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Optional

from services.image_search.query import normalize_image_query

LOGGER = logging.getLogger(__name__)

# Length of the SHA-256 hex prefix used to correlate log lines per query
# without revealing the query itself.
_QUERY_HASH_LENGTH = 12

OUTCOME_IMPORTED = "imported"
OUTCOME_PLACEHOLDER = "placeholder"
OUTCOME_CANCELLED = "cancelled"


def hash_query_for_log(query: str) -> str:
    """Stable, non-reversible log identifier for one search query."""
    normalized = normalize_image_query(query)
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:_QUERY_HASH_LENGTH]


@dataclass
class ImageSearchSlotMetrics:
    """One slot-resolution attempt, as observed by ``search_and_import``."""

    query_hash: str
    outcome: str
    failure_code: Optional[str] = None
    # Search-side observations (None when search never completed).
    cached: Optional[bool] = None
    fetch_latency_ms: Optional[int] = None
    candidates_returned: Optional[int] = None
    # Import-side observations (None when no candidate was attempted).
    import_latency_ms: Optional[int] = None
    imported_bytes: Optional[int] = None
    candidates_attempted: Optional[int] = None
    candidates_rejected: Optional[int] = None
    selected_engine: Optional[str] = None
    selected_domain: Optional[str] = None

    def to_log_fields(self) -> dict:
        """Drop ``None`` values so each line only carries what happened."""
        fields = {
            "query_hash": self.query_hash,
            "outcome": self.outcome,
            "failure_code": self.failure_code,
            "cached": self.cached,
            "fetch_ms": self.fetch_latency_ms,
            "candidates": self.candidates_returned,
            "attempted": self.candidates_attempted,
            "rejected": self.candidates_rejected,
            "import_ms": self.import_latency_ms,
            "bytes": self.imported_bytes,
            "engine": self.selected_engine,
            "domain": self.selected_domain,
        }
        return {key: value for key, value in fields.items() if value is not None}


def emit_slot_metrics(record: ImageSearchSlotMetrics) -> None:
    """Emit one structured metric line for a resolved (or cancelled) slot."""
    LOGGER.info(
        "image_search_slot %s",
        " ".join(f"{key}={value}" for key, value in record.to_log_fields().items()),
    )
