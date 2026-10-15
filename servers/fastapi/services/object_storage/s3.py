"""S3-compatible object storage backend (MinIO, AWS S3, other S3 services).

Blocking boto3 calls are wrapped with ``asyncio.to_thread``. botocore's
standard retry mode already limits retries to transient errors (throttling,
network) and never retries validation/authentication failures; we surface
those immediately as domain errors.
"""

import asyncio
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from botocore.config import Config
from botocore.exceptions import ClientError

from services.object_storage.base import ObjectStorage
from services.object_storage.errors import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStoragePermissionError,
    ObjectStorageUnavailableError,
)

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"NoSuchKey", "NoSuchBucket", "404", "NotFound"}
_AUTH_CODES = {
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
    "AccessDenied",
    "Unauthorized",
    "InvalidToken",
    "InvalidClientId",
}

_RETRIABLE_CONNECTION_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
)


def _translate_error(exc: BaseException, operation: str) -> ObjectStorageError:
    if isinstance(exc, ClientError):
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "") or "")
        status = str(error.get("HTTPStatusCode", "") or "")
        if code in _NOT_FOUND_CODES or status == "404":
            return ObjectNotFoundError(f"Object not found ({operation}): {code or status}")
        if code in _AUTH_CODES:
            return ObjectStoragePermissionError(
                f"Storage credentials rejected ({operation}): {code}"
            )
        if status.startswith("4"):
            return ObjectStoragePermissionError(
                f"Storage request rejected ({operation}): {code or status}"
            )
        return ObjectStorageUnavailableError(
            f"Storage backend error ({operation}): {code or status}"
        )

    from botocore.exceptions import EndpointConnectionError

    if isinstance(exc, (EndpointConnectionError,) + _RETRIABLE_CONNECTION_ERRORS):
        return ObjectStorageUnavailableError(
            f"Storage backend unreachable ({operation}): {exc}"
        )
    return ObjectStorageError(f"Unexpected storage error ({operation}): {exc}")


def create_s3_client(
    *,
    endpoint_url: str | None,
    access_key: str,
    secret_key: str,
    region: str,
    force_path_style: bool,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
) -> Any:
    """Create a boto3 S3 client with bounded timeouts and retries."""
    import boto3

    config_kwargs: dict[str, Any] = {
        "connect_timeout": connect_timeout_seconds,
        "read_timeout": read_timeout_seconds,
        "retries": {"max_attempts": 2, "mode": "standard"},
        # botocore 1.35+ defaults to streaming ``aws-chunked`` trailing
        # checksums on PutObject. Several S3-compatible backends (ZetaBy S3,
        # Ceph) reject those with ``XAmzContentSHA256Mismatch``. Only compute
        # checksums when the operation actually requires them.
        "request_checksum_calculation": "when_required",
        "response_checksum_validation": "when_required",
    }
    if force_path_style:
        config_kwargs["s3"] = {"addressing_style": "path"}
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name=region,
        config=Config(**config_kwargs),
    )


class S3ObjectStorage(ObjectStorage):
    def __init__(self, *, bucket: str, client: Any):
        if not bucket or not bucket.strip():
            raise ObjectStorageError("S3 bucket must be a non-empty string")
        self._bucket = bucket.strip()
        self._client = client

    @property
    def bucket(self) -> str:
        return self._bucket

    async def bucket_available(self) -> bool:
        """True if the configured bucket exists and credentials can reach it.

        Transport/permission failures raise domain errors (they do not mean
        the bucket is missing); only a clean 404/NoSuchBucket returns False.
        """

        def _head() -> None:
            self._client.head_bucket(Bucket=self._bucket)

        try:
            await asyncio.to_thread(_head)
            return True
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = str(error.get("Code", "") or "")
            status = str(error.get("HTTPStatusCode", "") or "")
            if code in {"NoSuchBucket", "404", "NotFound"} or status == "404":
                return False
            raise _translate_error(exc, "head_bucket") from exc
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise _translate_error(exc, "head_bucket") from exc

    async def create_bucket_if_missing(self) -> bool:
        """Create the bucket when missing. Returns True if it was created."""
        if await self.bucket_available():
            return False

        def _create() -> None:
            self._client.create_bucket(Bucket=self._bucket)

        try:
            await asyncio.to_thread(_create)
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise _translate_error(exc, "create_bucket") from exc
        logger.info("object_storage.create backend=s3 op=create_bucket bucket=%s", self._bucket)
        return True

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
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Key": checked,
            "Body": data,
            "ContentType": content_type,
        }
        if content_encoding is not None:
            kwargs["ContentEncoding"] = content_encoding
        if metadata:
            kwargs["Metadata"] = dict(metadata)
        await self._call("put_object", checked, len(data), **kwargs)

    async def put_file(self, key: str, source_path: str | Path, content_type: str) -> None:
        checked = self._checked_key(key)
        path = Path(source_path)
        await self._call(
            "put_object",
            checked,
            path.stat().st_size,
            Bucket=self._bucket,
            Key=checked,
            Body=path.read_bytes(),
            ContentType=content_type,
        )

    async def get_bytes(self, key: str) -> bytes:
        checked = self._checked_key(key)

        def _read() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=checked)
            return response["Body"].read()

        return await self._call_read("get_object", checked, _read)

    async def download_to_file(self, key: str, destination_path: str | Path) -> None:
        checked = self._checked_key(key)

        def _download() -> None:
            self._client.download_file(
                self._bucket, checked, str(destination_path)
            )

        await self._call_read("download_file", checked, _download)

    async def delete(self, key: str) -> None:
        checked = self._checked_key(key)
        await self._call("delete_object", checked, 0, Bucket=self._bucket, Key=checked)

    async def exists(self, key: str) -> bool:
        checked = self._checked_key(key)

        def _head() -> bool:
            try:
                self._client.head_object(Bucket=self._bucket, Key=checked)
                return True
            except ClientError as exc:
                error = exc.response.get("Error", {})
                code = str(error.get("Code", "") or "")
                status = str(error.get("HTTPStatusCode", "") or "")
                if code in _NOT_FOUND_CODES or status == "404":
                    return False
                raise

        try:
            return await asyncio.to_thread(_head)
        except ClientError as exc:
            raise _translate_error(exc, "head_object") from exc
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise _translate_error(exc, "head_object") from exc

    async def presign_get(self, key: str, expires_seconds: int) -> str:
        checked = self._checked_key(key)

        def _presign() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": checked},
                ExpiresIn=expires_seconds,
            )

        # Log the key only — presigned URLs carry credentials in the query
        # string and must never be logged.
        try:
            return await asyncio.to_thread(_presign)
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise _translate_error(exc, "presign_get") from exc

    async def _call(self, operation: str, key: str, size: int, **kwargs: Any) -> None:
        method = getattr(self._client, operation)
        try:
            await asyncio.to_thread(method, **kwargs)
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise _translate_error(exc, operation) from exc
        logger.info(
            "object_storage.put backend=s3 op=%s bytes=%d key=%s",
            operation,
            size,
            key,
        )

    async def _call_read(self, operation: str, key: str, func: Any) -> Any:
        try:
            return await asyncio.to_thread(func)
        except ObjectStorageError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize transport failures
            raise _translate_error(exc, operation) from exc


__all__ = ["S3ObjectStorage", "create_s3_client"]