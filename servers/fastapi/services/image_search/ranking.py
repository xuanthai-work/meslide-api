"""Candidate ranking (FR-5).

Deterministic, no LLM, no network. Signals, in priority order:

1. already-selected URLs (deck dedupe context) are never returned;
2. known dimensions preferred over unknown;
3. minimum usable size preferred;
4. aspect ratio close to the target slot (when both are known);
5. engine preference order (the ops allow-list order);
6. source-domain diversity: domains already used by this deck are
   penalized so one site cannot dominate a presentation;
7. results that look like charts/graphs/dashboards/infographics are
   deprioritized — they must not substitute editable slide components.
"""

import math
from urllib.parse import urlparse

from services.image_search.constants import limits
from services.image_search.models import DeckResolutionContext, ImageSearchCandidate

# Engine preference order matches the ops allow-list in
# gslide-ops/deploy/searxng/settings.yml; keep both files in sync.
_ENGINE_PREFERENCE = (
    "bing images",
    "google cse images",
    "pexels",
    "flickr",
    "wikicommons.images",
)

# Result titles that signal non-photographic, chart-like content.
_CHART_TITLE_KEYWORDS = (
    "chart",
    "graph",
    "diagram",
    "infographic",
    "dashboard",
    "plot",
    "biểu đồ",
)


def candidate_domain(candidate: ImageSearchCandidate) -> str:
    try:
        return (urlparse(candidate.image_url).hostname or "").lower()
    except ValueError:
        return ""


def _aspect_score(
    candidate: ImageSearchCandidate, aspect_ratio: float | None
) -> float:
    if (
        aspect_ratio is None
        or aspect_ratio <= 0
        or not candidate.width
        or not candidate.height
    ):
        return 0.0
    deviation = abs(
        math.log(candidate.width / candidate.height) - math.log(aspect_ratio)
    )
    if deviation <= limits.aspect_ratio_tolerance:
        # Full bonus inside tolerance, decaying outside it.
        return 2.0 * (1.0 - deviation / (limits.aspect_ratio_tolerance * 2))
    return max(0.0, 1.0 - deviation / 2.0) * 0.5


def _chart_penalty(candidate: ImageSearchCandidate) -> float:
    title = candidate.title.lower()
    return -5.0 if any(word in title for word in _CHART_TITLE_KEYWORDS) else 0.0


def candidate_score(
    candidate: ImageSearchCandidate,
    *,
    aspect_ratio: float | None = None,
    context: DeckResolutionContext | None = None,
) -> float:
    score = 0.0
    if candidate.width and candidate.height:
        score += 3.0
        if (
            candidate.width >= limits.min_usable_width
            and candidate.height >= limits.min_usable_height
        ):
            score += 2.0
    score += _aspect_score(candidate, aspect_ratio)
    if candidate.engine:
        try:
            preference_index = _ENGINE_PREFERENCE.index(candidate.engine)
        except ValueError:
            score -= 1.0  # unknown engine: keep, but prefer allow-list order
        else:
            score += 2.0 * (len(_ENGINE_PREFERENCE) - preference_index) / len(
                _ENGINE_PREFERENCE
            )
    score += _chart_penalty(candidate)
    if context is not None:
        domain = candidate_domain(candidate)
        score -= 3.0 * context.domain_counts.get(domain, 0)
    return score


def rank_candidates(
    candidates: list[ImageSearchCandidate],
    *,
    aspect_ratio: float | None = None,
    context: DeckResolutionContext | None = None,
) -> list[ImageSearchCandidate]:
    """Order candidates best-first, dropping deck-duplicate URLs.

    Stable: equal scores keep adapter order, so results are deterministic
    for tests and reproducible ranking.
    """
    if context is None:
        return sorted(
            candidates,
            key=lambda c: -candidate_score(c, aspect_ratio=aspect_ratio),
        )
    fresh = [c for c in candidates if not context.is_selected(c.image_url)]
    return sorted(
        fresh,
        key=lambda c: -candidate_score(
            c, aspect_ratio=aspect_ratio, context=context
        ),
    )