"""Startup and per-request readiness checks for the object-storage backend.

- Local backend needs no probe: the storage root is created lazily.
- The S3 backend must prove the bucket exists and the credentials work.
  Readiness failures never crash startup — requests surface them as a
  stable 503 (``error.storage.unavailable``) via the health endpoint, and
  other flows raise the same domain errors.
- Buckets are NOT auto-created at startup unless S3_AUTO_CREATE_BUCKET is
  explicitly enabled (production buckets are provisioned by ops; local
  MinIO buckets are bootstrapped by Compose).
"""

import logging
import os

from services.object_storage.base import ObjectStorage
from services.object_storage.errors import ObjectStorageError
from services.object_storage.factory import (
    get_object_storage,
    get_object_storage_backend,
    OBJECT_STORAGE_LOCAL_BACKEND,
    OBJECT_STORAGE_S3_BACKEND,
)
from services.object_storage.s3 import S3ObjectStorage

logger = logging.getLogger(__name__)


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _storage_backend_name(storage: ObjectStorage) -> str:
    """The authoritative backend is the storage instance, not the env."""
    if isinstance(storage, S3ObjectStorage):
        return OBJECT_STORAGE_S3_BACKEND
    if get_object_storage_backend() == OBJECT_STORAGE_S3_BACKEND:
        # Misuse guard: env says s3 but a non-S3 storage was injected.
        return OBJECT_STORAGE_S3_BACKEND
    return OBJECT_STORAGE_LOCAL_BACKEND


async def check_object_storage_readiness(
    storage: ObjectStorage | None = None,
) -> dict:
    """Report whether the object-storage backend is usable right now."""
    if storage is None:
        storage = get_object_storage()
    backend = _storage_backend_name(storage)

    if backend != OBJECT_STORAGE_S3_BACKEND:
        return {"backend": backend, "available": True, "bucket": None}

    bucket = getattr(storage, "bucket", None)
    try:
        if not await storage.bucket_available():
            return {
                "backend": backend,
                "available": False,
                "bucket": bucket,
                "reason": "bucket_missing",
            }
    except ObjectStorageError as exc:
        logger.warning("Object storage readiness probe failed: %s", exc)
        return {
            "backend": backend,
            "available": False,
            "bucket": bucket,
            "reason": "unavailable",
        }
    return {"backend": backend, "available": True, "bucket": bucket}


async def verify_object_storage_on_startup() -> None:
    """Best-effort backend verification; logs instead of raising."""
    try:
        storage = get_object_storage()
    except ObjectStorageError as exc:
        logger.error("Object storage configuration is invalid: %s", exc)
        return

    if _storage_backend_name(storage) != OBJECT_STORAGE_S3_BACKEND:
        return

    bucket = getattr(storage, "bucket", None)
    try:
        if await storage.bucket_available():
            logger.info("Object storage ready: s3 bucket %s", bucket)
            return
    except ObjectStorageError as exc:
        logger.error(
            "Object storage unavailable at startup (bucket=%s): %s. "
            "Requests needing storage will fail with error.storage.unavailable "
            "until it recovers.",
            bucket,
            exc,
        )
        return

    if _env_flag("S3_AUTO_CREATE_BUCKET", False):
        try:
            created = await storage.create_bucket_if_missing()
        except ObjectStorageError as exc:
            logger.error(
                "Failed to auto-create S3 bucket %s: %s", bucket, exc
            )
            return
        if created:
            logger.info("Auto-created S3 bucket %s", bucket)
        return

    logger.error(
        "S3 bucket %s is missing and S3_AUTO_CREATE_BUCKET is not enabled. "
        "Provision the bucket (locally: the Compose minio-init service) or "
        "requests needing storage will fail with error.storage.unavailable.",
        bucket,
    )


__all__ = ["check_object_storage_readiness", "verify_object_storage_on_startup"]