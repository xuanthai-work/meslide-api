"""Unit tests: object keys, error taxonomy, local backend, factory."""

import asyncio
from pathlib import Path

import pytest

from services.object_storage.errors import (
    InvalidObjectKeyError,
    ObjectNotFoundError,
    ObjectStorageConfigError,
    ObjectStorageError,
    ObjectStoragePermissionError,
    ObjectStorageUnavailableError,
)
from services.object_storage.keys import (
    build_template_bundle_key,
    validate_object_key,
    validate_owner_object_key,
)
from services.object_storage.local import LocalObjectStorage
from services.object_storage.factory import (
    get_object_storage,
    reset_object_storage,
)


# --- Keys -------------------------------------------------------------------


def test_template_bundle_key_uses_owner_template_revision_prefix():
    key = build_template_bundle_key(
        owner_id="11111111-1111-1111-1111-111111111111",
        template_id="22222222-2222-2222-2222-222222222222",
        sha256="abc123",
    )

    assert key == (
        "users/11111111-1111-1111-1111-111111111111/"
        "templates/22222222-2222-2222-2222-222222222222/"
        "revisions/abc123.json.gz"
    )


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "/absolute/path.json",
        "users/../etc/passwd",
        "users/1/../../secret.json",
        "users/1/templates\tinjected/x.json",
        "users/1/x" + "/y" * 600,
    ],
)
def test_validate_object_key_rejects_unsafe_keys(bad_key):
    with pytest.raises(InvalidObjectKeyError):
        validate_object_key(bad_key)


def test_validate_object_key_accepts_owner_scoped_key():
    validate_object_key("users/1/templates/2/revisions/abc.json.gz")


def test_validate_owner_object_key_enforces_owner_prefix():
    validate_owner_object_key("users/42/templates/2/revisions/abc.json.gz", owner_id="42")

    with pytest.raises(InvalidObjectKeyError):
        validate_owner_object_key("users/43/templates/2/revisions/abc.json.gz", owner_id="42")


# --- Errors -----------------------------------------------------------------


def test_storage_errors_form_domain_taxonomy():
    for error_cls in (
        ObjectNotFoundError,
        ObjectStorageConfigError,
        ObjectStoragePermissionError,
        ObjectStorageUnavailableError,
        InvalidObjectKeyError,
    ):
        assert issubclass(error_cls, ObjectStorageError)


# --- Local backend ----------------------------------------------------------


def test_local_backend_put_get_roundtrip(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    key = "users/1/templates/2/revisions/abc.json.gz"

    asyncio.run(
        storage.put_bytes(
            key,
            b"payload",
            content_type="application/json",
            content_encoding="gzip",
            metadata={"sha256": "abc"},
        )
    )

    assert asyncio.run(storage.exists(key)) is True
    assert asyncio.run(storage.get_bytes(key)) == b"payload"
    # Writes stay under the storage root.
    assert (tmp_path / key).is_file()


def test_local_backend_get_missing_key_raises_domain_not_found(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)

    with pytest.raises(ObjectNotFoundError):
        asyncio.run(storage.get_bytes("users/1/missing.json"))


def test_local_backend_delete_is_idempotent(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    key = "users/1/templates/2/revisions/abc.json.gz"
    asyncio.run(storage.put_bytes(key, b"x", content_type="application/json"))

    asyncio.run(storage.delete(key))
    asyncio.run(storage.delete(key))

    assert asyncio.run(storage.exists(key)) is False


def test_local_backend_put_file_and_download_to_file(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    source = tmp_path / "source.pptx"
    source.write_bytes(b"pptx-bytes")

    asyncio.run(storage.put_file("users/1/imports/a.pptx", source, "application/vnd..."))

    destination = tmp_path / "downloaded.pptx"
    asyncio.run(storage.download_to_file("users/1/imports/a.pptx", destination))

    assert destination.read_bytes() == b"pptx-bytes"


def test_local_backend_rejects_key_escaping_root(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)

    with pytest.raises(InvalidObjectKeyError):
        asyncio.run(
            storage.put_bytes("users/../../escape.txt", b"x", content_type="text/plain")
        )


def test_local_backend_presign_get_reports_unsupported(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)

    with pytest.raises(ObjectStorageError, match="[Ll]ocal"):
        asyncio.run(storage.presign_get("users/1/x.json", expires_seconds=60))


# --- Factory ----------------------------------------------------------------


def test_factory_defaults_to_local_backend(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OBJECT_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    reset_object_storage()

    storage = get_object_storage()

    assert isinstance(storage, LocalObjectStorage)


def test_factory_is_process_singleton(monkeypatch, tmp_path):
    monkeypatch.delenv("OBJECT_STORAGE_BACKEND", raising=False)
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    reset_object_storage()

    assert get_object_storage() is get_object_storage()

    reset_object_storage()


def test_factory_s3_backend_requires_credentials(monkeypatch):
    monkeypatch.setenv("OBJECT_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_ENDPOINT_URL", "http://minio:9000")
    monkeypatch.setenv("S3_ACCESS_KEY", "")
    monkeypatch.setenv("S3_SECRET_KEY", "")
    monkeypatch.setenv("S3_BUCKET", "gslide")
    reset_object_storage()

    with pytest.raises(ObjectStorageConfigError):
        get_object_storage()


def test_factory_rejects_unknown_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("OBJECT_STORAGE_BACKEND", "ftp")
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    reset_object_storage()

    with pytest.raises(ObjectStorageConfigError):
        get_object_storage()