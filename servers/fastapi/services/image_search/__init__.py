"""SearXNG-backed automatic web image search.

Provider-neutral interface (first adapter: SearXNG images category).
SearXNG is a search engine, never an AI image provider: nothing in this
package may be registered as an ``ImageProvider`` value, and nothing here
may instantiate ``ImageGenerationService``.
"""

from services.image_search.constants import (  # noqa: F401
    ImageSearchLimits,
    limits,
)
from services.image_search.models import (  # noqa: F401
    DeckResolutionContext,
    ImageSearchCandidate,
    ImageSearchOutcome,
    ImageSearchService,
)
from services.image_search.availability import (  # noqa: F401
    get_web_image_search_max_results,
    web_image_search_available,
    web_image_search_configured,
    web_image_search_feature_enabled,
)
from services.image_search.query import (  # noqa: F401
    image_prompt_to_image_query,
    normalize_image_query,
)
from services.image_search.service import SearxngImageSearchService  # noqa: F401
from services.image_search.warnings import (  # noqa: F401
    IMAGE_SEARCH_NOT_CONFIGURED_CODE,
    image_search_warning,
)

_IMAGE_SEARCH_SERVICE = None

__all__ = [
    "DeckResolutionContext",
    "IMAGE_SEARCH_NOT_CONFIGURED_CODE",
    "ImageSearchCandidate",
    "ImageSearchOutcome",
    "ImageSearchLimits",
    "ImageSearchService",
    "SearxngImageSearchService",
    "get_image_search_service",
    "get_web_image_search_max_results",
    "image_prompt_to_image_query",
    "image_search_warning",
    "limits",
    "normalize_image_query",
    "web_image_search_available",
    "web_image_search_configured",
    "web_image_search_feature_enabled",
]


def get_image_search_service() -> SearxngImageSearchService:
    """Process-wide image-search service with the secure importer attached.

    Lazy: nothing is constructed while the feature is off, so the rollout
    flag off costs zero setup. Callers must only reach this in search mode
    (``web_search`` on AND rollout flag on AND SearXNG configured).
    """
    global _IMAGE_SEARCH_SERVICE
    if _IMAGE_SEARCH_SERVICE is None:
        from services.image_search.downloader import SsrSafeImageImporter

        _IMAGE_SEARCH_SERVICE = SearxngImageSearchService(
            importer=SsrSafeImageImporter()
        )
    return _IMAGE_SEARCH_SERVICE