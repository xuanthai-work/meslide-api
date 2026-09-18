"""Domain errors for template bundles."""


class TemplateBundleError(Exception):
    """Base class for template-bundle domain errors."""


class InvalidBundleError(TemplateBundleError):
    """The bundle payload failed validation and must not be persisted."""


class BundleChecksumMismatchError(TemplateBundleError):
    """The stored object does not match the checksum recorded in the database."""


__all__ = [
    "TemplateBundleError",
    "InvalidBundleError",
    "BundleChecksumMismatchError",
]