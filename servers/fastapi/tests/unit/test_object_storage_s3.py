"""Unit tests: S3 backend contract via a fake boto3 client."""

import asyncio
from typing import Any

import pytest
from botocore.exceptions import ClientError
from botocore.exceptions import EndpointConnectionError

from services.object_storage.errors import (
    ObjectNotFoundError,
    ObjectStoragePermissionError,
    ObjectStorageUnavailableError,
)
from services.object_storage.s3 import S3ObjectStorage


class FakeS3Client:
    """In-memory boto3 client stand-in speaking the tiny surface we use."""

    def __init__(self, *, bucket: str = "gslide", fail_auth: bool = False):
        self.bucket = bucket
        self.objects: dict[str, dict[str, Any]] = {}
        self.call_counts: dict[str, int] = {}
        self.fail_auth = fail_auth
        self.bucket_missing = False

    def _record(self, name: str) -> None:
        self.call_counts[name] = self.call_counts.get(name, 0) + 1
        if self.fail_auth and name != "head_bucket":
            raise ClientError(
                {"Error": {"Code": "InvalidAccessKeyId", "Message": "bad key"}},
                name,
            )

    def put_object(self, Bucket, Key, Body, ContentType, **kwargs):
        self._record("put_object")
        self.objects[Key] = {
            "Body": Body,
            "ContentType": ContentType,
            "ContentEncoding": kwargs.get("ContentEncoding"),
            "Metadata": kwargs.get("Metadata") or {},
        }

    def get_object(self, Bucket, Key):
        self._record("get_object")
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "get_object"
            )
        body = self.objects[Key]["Body"]

        class _Body:
            def read(self):
                return body

        return {"Body": _Body()}

    def head_object(self, Bucket, Key):
        self._record("head_object")
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "head_object"
            )
        return {}

    def delete_object(self, Bucket, Key):
        self._record("delete_object")
        self.objects.pop(Key, None)

    def head_bucket(self, Bucket):
        self._record("head_bucket")
        if self.bucket_missing:
            raise ClientError(
                {"Error": {"Code": "NoSuchBucket", "Message": "missing"}},
                "head_bucket",
            )
        return {}

    def create_bucket(self, Bucket, **kwargs):
        self._record("create_bucket")
        self.bucket_missing = False

    def download_file(self, Bucket, Key, Filename):
        self._record("download_file")
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "404", "Message": "Not Found"}}, "download_file"
            )
        with open(Filename, "wb") as handle:
            handle.write(self.objects[Key]["Body"])

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        self._record("generate_presigned_url")
        return f"https://s3.example/{Params['Bucket']}/{Params['Key']}?expires={ExpiresIn}"

    def raise_connection_error(self, name: str):
        def _fail(*args, **kwargs):
            self._record(name)
            raise EndpointConnectionError(endpoint_url="http://minio:9000")

        setattr(self, name, _fail)


def make_storage(client: FakeS3Client) -> S3ObjectStorage:
    return S3ObjectStorage(bucket="gslide", client=client)


def test_s3_put_get_roundtrip_carry_content_type_encoding_metadata():
    client = FakeS3Client()
    storage = make_storage(client)

    asyncio.run(
        storage.put_bytes(
            "users/1/templates/2/revisions/abc.json.gz",
            b"compressed",
            content_type="application/json",
            content_encoding="gzip",
            metadata={"sha256": "abc", "schema-version": "1"},
        )
    )

    stored = client.objects["users/1/templates/2/revisions/abc.json.gz"]
    assert stored["ContentType"] == "application/json"
    assert stored["ContentEncoding"] == "gzip"
    assert stored["Metadata"] == {"sha256": "abc", "schema-version": "1"}

    assert asyncio.run(storage.get_bytes("users/1/templates/2/revisions/abc.json.gz")) == (
        b"compressed"
    )


def test_s3_get_missing_key_maps_to_domain_not_found():
    client = FakeS3Client()
    storage = make_storage(client)

    with pytest.raises(ObjectNotFoundError):
        asyncio.run(storage.get_bytes("users/1/missing.json"))


def test_s3_exists_false_for_missing_key_without_raising():
    client = FakeS3Client()
    storage = make_storage(client)

    assert asyncio.run(storage.exists("users/1/missing.json")) is False


def test_s3_auth_error_maps_to_permission_error_and_is_not_retried():
    client = FakeS3Client(fail_auth=True)
    storage = make_storage(client)

    with pytest.raises(ObjectStoragePermissionError):
        asyncio.run(storage.put_bytes("users/1/x", b"x", content_type="text/plain"))

    # Validation/auth errors must not be retried.
    assert client.call_counts["put_object"] == 1


def test_s3_connection_error_maps_to_unavailable():
    client = FakeS3Client()
    client.raise_connection_error("get_object")
    storage = make_storage(client)

    with pytest.raises(ObjectStorageUnavailableError):
        asyncio.run(storage.get_bytes("users/1/whatever.json"))


def test_s3_download_delete_presign(tmp_path):
    client = FakeS3Client()
    storage = make_storage(client)
    key = "users/1/exports/e1.pptx"
    asyncio.run(storage.put_bytes(key, b"pptx", content_type="application/octet-stream"))

    destination = tmp_path / "out.pptx"
    asyncio.run(storage.download_to_file(key, destination))
    assert destination.read_bytes() == b"pptx"

    url = asyncio.run(storage.presign_get(key, expires_seconds=120))
    assert "expires=120" in url

    asyncio.run(storage.delete(key))
    assert asyncio.run(storage.exists(key)) is False


def test_s3_rejects_key_escaping_owner_prefix():
    client = FakeS3Client()
    storage = make_storage(client)

    from services.object_storage.errors import InvalidObjectKeyError

    with pytest.raises(InvalidObjectKeyError):
        asyncio.run(
            storage.put_bytes("users/../escape", b"x", content_type="text/plain")
        )
    # Nothing reached the client.
    assert client.objects == {}


def test_s3_bucket_available_reports_presence():
    client = FakeS3Client()
    storage = make_storage(client)

    assert asyncio.run(storage.bucket_available()) is True

    client.bucket_missing = True
    assert asyncio.run(storage.bucket_available()) is False


def test_s3_create_bucket_if_missing_creates_once():
    client = FakeS3Client()
    client.bucket_missing = True
    storage = make_storage(client)

    assert asyncio.run(storage.create_bucket_if_missing()) is True
    assert asyncio.run(storage.create_bucket_if_missing()) is False
    assert client.call_counts["create_bucket"] == 1