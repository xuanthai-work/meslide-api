"""Template bundle storage: canonical serialization, repository, cache."""

from services.template_bundles.bundle import (
    BUNDLE_SCHEMA_VERSION,
    TemplateBundle,
    deserialize_bundle,
    serialize_bundle,
)
from services.template_bundles.cache import BundleCache, get_template_bundle_cache
from services.template_bundles.errors import (
    BundleChecksumMismatchError,
    InvalidBundleError,
    TemplateBundleError,
)
from services.template_bundles.repository import (
    BundleReference,
    TemplateBundleRepository,
)

__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "BundleCache",
    "BundleReference",
    "TemplateBundle",
    "TemplateBundleError",
    "TemplateBundleRepository",
    "InvalidBundleError",
    "BundleChecksumMismatchError",
    "deserialize_bundle",
    "get_template_bundle_cache",
    "serialize_bundle",
]