"""Backend-agnostic object storage interface."""

import asyncio
from abc import ABC, abstractmethod
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from services.object_storage.errors import InvalidObjectKeyError
from services.object_storage.keys import validate_object_key


class ObjectStorage(ABC):
    """Minimal async contract shared by the local and S3 backends.

    Implementations must:
    - validate object keys before use (see ``services.object_storage.keys``),
    - wrap blocking I/O so the event loop is never held hostage,
    - translate backend errors into ``services.object_storage.errors``.
    """

    @abstractmethod
    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        content_encoding: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        """Store ``data`` at ``key``."""

    @abstractmethod
    async def put_file(self, key: str, source_path: str | Path, content_type: str) -> None:
        """Store the file at ``source_path`` under ``key``."""

    @abstractmethod
    async def get_bytes(self, key: str) -> bytes:
        """Read the object at ``key``. Raises ObjectNotFoundError if missing."""

    @abstractmethod
    async def download_to_file(self, key: str, destination_path: str | Path) -> None:
        """Stream the object at ``key`` to a local file."""

    @abstractmethod
    async def delete(self, key: str) -> None:
        """Delete the object at ``key``; missing objects are not an error."""

    @abstractmethod
    async def exists(self, key: str) -> bool:
        """Return True if the object exists."""

    @abstractmethod
    async def presign_get(self, key: str, expires_seconds: int) -> str:
        """Return a short-lived GET URL for the object at ``key``."""

    async def bucket_available(self) -> bool:
        """True if the backend's bucket/root is ready for use.

        Local backends are always ready (the directory is created lazily);
        S3 backends override this with a real bucket probe.
        """
        return True

    def _checked_key(self, key: str) -> str:
        if not isinstance(key, str):  # pragma: no cover - defensive
            raise InvalidObjectKeyError("Object key must be a string")
        return validate_object_key(key)

    def _describe(self, key: str) -> dict[str, Any]:
        return {"key": key, "backend": type(self).__name__}


def run_storage_io(func: Any, *args: Any, **kwargs: Any) -> Any:
    """Run a blocking storage call off the event loop."""
    return asyncio.to_thread(func, *args, **kwargs)


__all__ = ["ObjectStorage"]