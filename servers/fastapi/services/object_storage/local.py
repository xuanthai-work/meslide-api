"""Local filesystem object storage (default backend).

Objects live under a single root directory (``APP_DATA_DIRECTORY/object-storage``)
with the object key as the relative path. Used for backward-compatible
deployments and local development; the S3 backend implements the same
contract.
"""

import asyncio
import logging
import shutil
from collections.abc import Mapping
from pathlib import Path

from services.object_storage.base import ObjectStorage
from services.object_storage.errors import ObjectNotFoundError, ObjectStorageError

logger = logging.getLogger(__name__)


class LocalObjectStorage(ObjectStorage):
    def __init__(self, root: str | Path):
        self._root = Path(root).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, key: str) -> Path:
        path = (self._root / key).resolve()
        if not path.is_relative_to(self._root):
            # Defense in depth: validate_object_key already rejects traversal.
            raise ObjectStorageError(f"Object key escapes storage root: {key!r}")
        return path

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        content_encoding: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        checked = self._checked_key(key)
        path = self._path_for(checked)
        await asyncio.to_thread(self._write_bytes, path, data)
        logger.info(
            "object_storage.put backend=local op=put_bytes bytes=%d key=%s",
            len(data),
            checked,
        )

    async def put_file(self, key: str, source_path: str | Path, content_type: str) -> None:
        checked = self._checked_key(key)
        path = self._path_for(checked)
        await asyncio.to_thread(self._copy_file, source_path, path)
        logger.info(
            "object_storage.put backend=local op=put_file bytes=%d key=%s",
            path.stat().st_size,
            checked,
        )

    async def get_bytes(self, key: str) -> bytes:
        checked = self._checked_key(key)
        path = self._path_for(checked)
        if not path.is_file():
            raise ObjectNotFoundError(f"Object not found: {checked}")
        return await asyncio.to_thread(path.read_bytes)

    async def download_to_file(self, key: str, destination_path: str | Path) -> None:
        checked = self._checked_key(key)
        path = self._path_for(checked)
        if not path.is_file():
            raise ObjectNotFoundError(f"Object not found: {checked}")
        destination = Path(destination_path)
        await asyncio.to_thread(shutil.copyfile, str(path), str(destination))

    async def delete(self, key: str) -> None:
        checked = self._checked_key(key)
        path = self._path_for(checked)
        await asyncio.to_thread(lambda: path.unlink(missing_ok=True))
        logger.info("object_storage.delete backend=local key=%s", checked)

    async def exists(self, key: str) -> bool:
        checked = self._checked_key(key)
        path = self._path_for(checked)
        return path.is_file()

    async def presign_get(self, key: str, expires_seconds: int) -> str:
        raise ObjectStorageError(
            "presigned URLs are not supported by the local backend; "
            "serve stored files through the existing app_data routes instead"
        )

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    @staticmethod
    def _copy_file(source_path: str | Path, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(str(source_path), str(destination))


__all__ = ["LocalObjectStorage"]