"""Domain errors for the object-storage layer.

Every storage backend normalizes its low-level exceptions (botocore, OS
errors) into this taxonomy so callers can branch on stable error types
instead of backend-specific exceptions.
"""


class ObjectStorageError(Exception):
    """Base class for all object-storage domain errors."""


class InvalidObjectKeyError(ObjectStorageError):
    """The object key is malformed or escapes its allowed namespace."""


class ObjectNotFoundError(ObjectStorageError):
    """The requested object does not exist."""


class ObjectStorageConfigError(ObjectStorageError):
    """The storage backend is misconfigured (unknown backend, missing credentials)."""


class ObjectStoragePermissionError(ObjectStorageError):
    """Credentials lack the required access, or the request was rejected as
    unauthorized. Must not be retried."""


class ObjectStorageUnavailableError(ObjectStorageError):
    """The storage backend could not be reached (connection, timeout, 5xx)."""


__all__ = [
    "ObjectStorageError",
    "InvalidObjectKeyError",
    "ObjectNotFoundError",
    "ObjectStorageConfigError",
    "ObjectStoragePermissionError",
    "ObjectStorageUnavailableError",
]