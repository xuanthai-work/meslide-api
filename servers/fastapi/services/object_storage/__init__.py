"""Object-storage abstraction shared by the local filesystem and S3/MinIO."""

from services.object_storage.base import ObjectStorage
from services.object_storage.errors import (
    InvalidObjectKeyError,
    ObjectNotFoundError,
    ObjectStorageConfigError,
    ObjectStorageError,
    ObjectStoragePermissionError,
    ObjectStorageUnavailableError,
)
from services.object_storage.factory import (
    get_object_storage,
    get_object_storage_backend,
    reset_object_storage,
)
from services.object_storage.keys import (
    build_template_bundle_key,
    validate_object_key,
    validate_owner_object_key,
)
from services.object_storage.local import LocalObjectStorage

__all__ = [
    "ObjectStorage",
    "LocalObjectStorage",
    "ObjectStorageError",
    "ObjectStorageConfigError",
    "ObjectNotFoundError",
    "ObjectStoragePermissionError",
    "ObjectStorageUnavailableError",
    "InvalidObjectKeyError",
    "get_object_storage",
    "get_object_storage_backend",
    "reset_object_storage",
    "build_template_bundle_key",
    "validate_object_key",
    "validate_owner_object_key",
]