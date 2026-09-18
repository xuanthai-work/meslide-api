"""Unit tests: object-storage readiness checks and the health endpoint."""

import asyncio
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from api.v1.ppt.endpoints.storage_health import get_storage_health
from services.object_storage.errors import (
    ObjectStorageUnavailableError,
)
from services.object_storage.factory import reset_object_storage
from services.object_storage.local import LocalObjectStorage
from services.object_storage.readiness import (
    check_object_storage_readiness,
    verify_object_storage_on_startup,
)
from services.object_storage.s3 import S3ObjectStorage
from utils.coded_exception import CodedHTTPException


class UnreachableBucketClient:
    def head_bucket(self, Bucket):
        raise ClientError(
            {"Error": {"Code": "503", "Message": "slow down"}}, "head_bucket"
        )


class MissingBucketClient:
    def head_bucket(self, Bucket):
        raise ClientError(
            {"Error": {"Code": "NoSuchBucket", "Message": "missing"}}, "head_bucket"
        )


def test_local_backend_is_always_ready(tmp_path: Path):
    result = asyncio.run(check_object_storage_readiness(LocalObjectStorage(root=tmp_path)))

    assert result == {"backend": "local", "available": True, "bucket": None}


def test_s3_readiness_reports_bucket():
    from tests.unit.test_object_storage_s3 import FakeS3Client

    storage = S3ObjectStorage(bucket="gslide", client=FakeS3Client())
    result = asyncio.run(check_object_storage_readiness(storage))

    assert result == {"backend": "s3", "available": True, "bucket": "gslide"}


def test_s3_readiness_missing_bucket_reports_not_available():
    storage = S3ObjectStorage(bucket="gslide", client=MissingBucketClient())

    result = asyncio.run(check_object_storage_readiness(storage))

    assert result["available"] is False
    assert result["bucket"] == "gslide"
    assert result["reason"] == "bucket_missing"


def test_s3_readiness_maps_unavailable_error():
    storage = S3ObjectStorage(bucket="gslide", client=UnreachableBucketClient())

    result = asyncio.run(check_object_storage_readiness(storage))

    assert result["available"] is False
    assert result["reason"] == "unavailable"


def test_storage_health_endpoint_local_backend_returns_200(monkeypatch, tmp_path):
    monkeypatch.delenv("OBJECT_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    reset_object_storage()
    try:
        response = asyncio.run(get_storage_health())

        assert response == {"backend": "local", "available": True, "bucket": None}
    finally:
        reset_object_storage()


def test_storage_health_endpoint_s3_unavailable_returns_coded_503(monkeypatch):
    monkeypatch.setenv("OBJECT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ACCESS_KEY", "k")
    monkeypatch.setenv("S3_SECRET_KEY", "s")
    monkeypatch.setenv("S3_BUCKET", "gslide")
    monkeypatch.setattr(
        "services.object_storage.factory.build_object_storage",
        lambda: S3ObjectStorage(bucket="gslide", client=UnreachableBucketClient()),
    )
    reset_object_storage()
    try:
        with pytest.raises(CodedHTTPException) as exc_info:
            asyncio.run(get_storage_health())

        assert exc_info.value.status_code == 503
        assert exc_info.value.error_code == "error.storage.unavailable"
    finally:
        reset_object_storage()


def test_startup_verification_logs_instead_of_raising(monkeypatch, tmp_path):
    monkeypatch.setenv("OBJECT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ACCESS_KEY", "k")
    monkeypatch.setenv("S3_SECRET_KEY", "s")
    monkeypatch.setattr(
        "services.object_storage.factory.build_object_storage",
        lambda: S3ObjectStorage(bucket="gslide", client=UnreachableBucketClient()),
    )
    reset_object_storage()
    try:
        # Must not raise even though the backend is unreachable.
        asyncio.run(verify_object_storage_on_startup())
    finally:
        reset_object_storage()


def test_s3_unreachable_bucket_raises_domain_unavailable():
    storage = S3ObjectStorage(bucket="gslide", client=UnreachableBucketClient())

    with pytest.raises(ObjectStorageUnavailableError):
        asyncio.run(storage.bucket_available())