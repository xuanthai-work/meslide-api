"""Process-wide object-storage construction from environment variables.

Environment contract (mirrors ``gslide-ops/.env.example``):

    OBJECT_STORAGE_BACKEND=local|s3   (default: local, backward compatible)
    S3_ENDPOINT_URL=http://minio:9000
    S3_ACCESS_KEY=...                (required for s3)
    S3_SECRET_KEY=...                (required for s3)
    S3_BUCKET=gslide
    S3_REGION=us-east-1
    S3_FORCE_PATH_STYLE=true
    S3_CONNECT_TIMEOUT_SECONDS=5
    S3_READ_TIMEOUT_SECONDS=30
"""

import os
from pathlib import Path

from services.object_storage.base import ObjectStorage
from services.object_storage.errors import ObjectStorageConfigError
from services.object_storage.local import LocalObjectStorage

OBJECT_STORAGE_LOCAL_BACKEND = "local"
OBJECT_STORAGE_S3_BACKEND = "s3"

_storage_singleton: ObjectStorage | None = None


def get_object_storage_backend() -> str:
    """Normalized backend name from the environment (default: local)."""
    backend = (os.getenv("OBJECT_STORAGE_BACKEND") or "").strip().lower()
    return backend or OBJECT_STORAGE_LOCAL_BACKEND


def get_object_storage() -> ObjectStorage:
    """Return the process-wide storage backend, building it on first call."""
    global _storage_singleton
    if _storage_singleton is None:
        _storage_singleton = build_object_storage()
    return _storage_singleton


def reset_object_storage() -> None:
    """Drop the cached singleton (tests and environment changes)."""
    global _storage_singleton
    _storage_singleton = None


def build_object_storage() -> ObjectStorage:
    backend = get_object_storage_backend()
    if backend == OBJECT_STORAGE_LOCAL_BACKEND:
        return LocalObjectStorage(root=_local_root())
    if backend == OBJECT_STORAGE_S3_BACKEND:
        return _build_s3_storage()
    raise ObjectStorageConfigError(
        f"Unknown OBJECT_STORAGE_BACKEND: {backend!r}. "
        f"Use {OBJECT_STORAGE_LOCAL_BACKEND!r} or {OBJECT_STORAGE_S3_BACKEND!r}."
    )


def _local_root() -> Path:
    from utils.get_env import get_app_data_directory_env

    app_data = (get_app_data_directory_env() or "").strip()
    if app_data:
        return Path(app_data) / "object-storage"
    return Path("app_data") / "object-storage"


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _build_s3_storage() -> ObjectStorage:
    from services.object_storage.s3 import S3ObjectStorage, create_s3_client

    access_key = (os.getenv("S3_ACCESS_KEY") or "").strip()
    secret_key = (os.getenv("S3_SECRET_KEY") or "").strip()
    bucket = (os.getenv("S3_BUCKET") or "").strip() or "gslide"
    if not access_key or not secret_key:
        raise ObjectStorageConfigError(
            "OBJECT_STORAGE_BACKEND=s3 requires S3_ACCESS_KEY and S3_SECRET_KEY"
        )
    client = create_s3_client(
        endpoint_url=(os.getenv("S3_ENDPOINT_URL") or "").strip() or None,
        access_key=access_key,
        secret_key=secret_key,
        region=(os.getenv("S3_REGION") or "").strip() or "us-east-1",
        force_path_style=_env_flag("S3_FORCE_PATH_STYLE", True),
        connect_timeout_seconds=_env_float("S3_CONNECT_TIMEOUT_SECONDS", 5.0),
        read_timeout_seconds=_env_float("S3_READ_TIMEOUT_SECONDS", 30.0),
    )
    return S3ObjectStorage(bucket=bucket, client=client)


__all__ = [
    "get_object_storage",
    "get_object_storage_backend",
    "reset_object_storage",
    "build_object_storage",
]