"""Object-storage integration for the image pipeline (S3/MinIO)."""

import asyncio
import io
import uuid

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from api.v1.auth.context import reset_current_owner_id, set_current_owner_id
from api.v1.ppt.endpoints import images as images_module
from api.v1.ppt.endpoints.images import IMAGES_ROUTER
from models.sql.image_asset import ImageAsset
from services.database import get_async_session


class FakeObjectStorage:
    """In-memory stand-in for the object-storage contract."""

    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put_bytes(
        self,
        key,
        data,
        *,
        content_type,
        content_encoding=None,
        metadata=None,
    ):
        self.objects[key] = (data, content_type)

    async def exists(self, key):
        return key in self.objects

    async def get_bytes(self, key):
        return self.objects[key][0]

    async def delete(self, key):
        self.objects.pop(key, None)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color=(0, 128, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def _build_client(fake_async_session):
    app = FastAPI()
    app.include_router(IMAGES_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: fake_async_session
    return TestClient(app)


def test_create_s3_client_disables_streaming_checksums():
    from services.object_storage.s3 import create_s3_client

    client = create_s3_client(
        endpoint_url="http://minio:9000",
        access_key="key",
        secret_key="secret",
        region="us-east-1",
        force_path_style=True,
        connect_timeout_seconds=5.0,
        read_timeout_seconds=30.0,
    )

    config = client.meta.config
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert config.s3 == {"addressing_style": "path"}


def _patch_s3(monkeypatch, storage, module=images_module):
    monkeypatch.setattr(module, "get_object_storage_backend", lambda: "s3")
    monkeypatch.setattr(module, "get_object_storage", lambda: storage)


def test_upload_image_stores_bytes_in_s3(monkeypatch, fake_async_session):
    storage = FakeObjectStorage()
    _patch_s3(monkeypatch, storage)
    client = _build_client(fake_async_session)
    owner_id = uuid.uuid4()

    token = set_current_owner_id(owner_id)
    try:
        response = client.post(
            "/images/upload",
            files={"file": ("slide.png", _png_bytes(), "image/png")},
        )
    finally:
        reset_current_owner_id(token)

    assert response.status_code == 200
    body = response.json()
    assert body["path"].startswith(f"images/users/{owner_id}/")
    assert body["path"].endswith(".png")
    assert body["file_url"].startswith(f"/app_data/images/users/{owner_id}/")
    assert body["path"] in storage.objects
    data, content_type = storage.objects[body["path"]]
    assert content_type == "image/png"
    assert data == _png_bytes()
    # Nothing is written to a local images directory when S3 is the backend.
    assert fake_async_session.added[0].path == body["path"]


def test_uploaded_images_lists_only_present_s3_keys(monkeypatch):
    storage = FakeObjectStorage()
    _patch_s3(monkeypatch, storage)
    owner_id = uuid.uuid4()
    present = f"images/users/{owner_id}/present.png"
    missing = f"images/users/{owner_id}/missing.png"
    storage.objects[present] = (b"png", "image/png")

    class _Session:
        async def scalars(self, *_args, **_kwargs):
            return [
                ImageAsset(path=present, is_uploaded=True, owner_id=owner_id),
                ImageAsset(path=missing, is_uploaded=True, owner_id=owner_id),
            ]

    client = _build_client(_Session())

    token = set_current_owner_id(owner_id)
    try:
        response = client.get("/images/uploaded")
    finally:
        reset_current_owner_id(token)

    assert response.status_code == 200
    assert [item["path"] for item in response.json()] == [present]


def test_delete_image_removes_the_s3_object(monkeypatch):
    owner_id = uuid.uuid4()
    key = f"images/users/{owner_id}/gone.png"
    asset = ImageAsset(path=key, is_uploaded=True, owner_id=owner_id)
    asset_id = asset.id
    storage = FakeObjectStorage()
    storage.objects[key] = (b"png", "image/png")
    _patch_s3(monkeypatch, storage)

    class _Session:
        def __init__(self):
            self.deleted = []

        async def get(self, _model, _key):
            return asset

        async def delete(self, obj):
            self.deleted.append(obj)

        async def commit(self):
            return None

    session = _Session()
    client = _build_client(session)

    response = client.delete(f"/images/{asset_id}")

    assert response.status_code == 204
    assert key not in storage.objects
    assert session.deleted == [asset]


def test_app_data_image_route_streams_from_s3(monkeypatch, tmp_path):
    from api.main import serve_app_data_image

    storage = FakeObjectStorage()
    owner_id = uuid.uuid4()
    key = f"images/users/{owner_id}/pic.png"
    payload = _png_bytes()
    storage.objects[key] = (payload, "image/png")

    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    monkeypatch.setattr("api.main.get_object_storage_backend", lambda: "s3")
    monkeypatch.setattr("api.main.get_object_storage", lambda: storage)

    token = set_current_owner_id(owner_id)
    try:
        response = asyncio.run(serve_app_data_image(f"users/{owner_id}/pic.png"))
    finally:
        reset_current_owner_id(token)

    assert response.body == payload
    assert response.media_type == "image/png"
    assert response.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )


def test_app_data_image_route_404_when_missing_everywhere(monkeypatch, tmp_path):
    from api.main import serve_app_data_image

    storage = FakeObjectStorage()
    owner_id = uuid.uuid4()
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
    monkeypatch.setattr("api.main.get_object_storage_backend", lambda: "s3")
    monkeypatch.setattr("api.main.get_object_storage", lambda: storage)

    token = set_current_owner_id(owner_id)
    try:
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(serve_app_data_image(f"users/{owner_id}/missing.png"))
    finally:
        reset_current_owner_id(token)

    assert excinfo.value.status_code == 404
