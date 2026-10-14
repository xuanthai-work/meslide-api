import io
import uuid
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from api.v1.auth.context import reset_current_owner_id, set_current_owner_id
from api.v1.ppt.endpoints.images import IMAGES_ROUTER
from models.sql.image_asset import ImageAsset
from services.database import get_async_session


class _AssetSession:
    def __init__(self, assets):
        self._assets = assets

    async def scalars(self, *_args, **_kwargs):
        return self._assets


def _build_client(fake_async_session):
    app = FastAPI()
    app.include_router(IMAGES_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: fake_async_session
    return TestClient(app)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


def test_generate_image_returns_image_path_and_persists_image_asset(fake_async_session):
    client = _build_client(fake_async_session)
    generated_asset = ImageAsset(path="/tmp/generated/a.png", is_uploaded=False)

    with patch(
        "api.v1.ppt.endpoints.images.get_images_directory", return_value="/tmp"
    ), patch("api.v1.ppt.endpoints.images.ImageGenerationService") as mock_service_cls:
        service = Mock()
        service.generate_image = AsyncMock(return_value=generated_asset)
        mock_service_cls.return_value = service

        response = client.get("/images/generate?prompt=business-dashboard")

    assert response.status_code == 200
    assert response.json() == "/tmp/generated/a.png"
    assert fake_async_session.added[-1] == generated_asset
    assert fake_async_session.commit_count == 1


def test_generate_image_returns_placeholder_without_db_write(fake_async_session):
    client = _build_client(fake_async_session)

    with patch(
        "api.v1.ppt.endpoints.images.get_images_directory", return_value="/tmp"
    ), patch("api.v1.ppt.endpoints.images.ImageGenerationService") as mock_service_cls:
        service = Mock()
        service.generate_image = AsyncMock(return_value="/static/images/placeholder.jpg")
        mock_service_cls.return_value = service

        response = client.get("/images/generate?prompt=business-dashboard")

    assert response.status_code == 200
    assert response.json() == "/static/images/placeholder.jpg"
    assert fake_async_session.added == []


def test_generate_image_returns_provider_error_status(fake_async_session):
    client = _build_client(fake_async_session)

    with patch(
        "api.v1.ppt.endpoints.images.get_images_directory", return_value="/tmp"
    ), patch("api.v1.ppt.endpoints.images.ImageGenerationService") as mock_service_cls:
        service = Mock()
        service.generate_image = AsyncMock(
            side_effect=HTTPException(
                status_code=429,
                detail="OpenAI image generation failed because API quota is unavailable.",
            )
        )
        mock_service_cls.return_value = service

        response = client.get("/images/generate?prompt=business-dashboard")

    assert response.status_code == 429
    assert "API quota is unavailable" in response.json()["detail"]
    assert fake_async_session.added == []


def test_upload_image_accepts_valid_image_and_persists_asset(
    tmp_path, fake_async_session
):
    client = _build_client(fake_async_session)

    with patch(
        "api.v1.ppt.endpoints.images.get_images_directory", return_value=str(tmp_path)
    ):
        response = client.post(
            "/images/upload",
            files={"file": ("slide.png", _png_bytes(), "image/png")},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["is_uploaded"] is True
    assert body["path"].endswith(".png")
    assert body["file_url"]
    assert fake_async_session.commit_count == 1
    assert len(fake_async_session.added) == 1
    assert fake_async_session.added[0].path == body["path"]
    assert (tmp_path / body["path"].split("/")[-1]).is_file()


def test_upload_image_rejects_non_image_bytes(tmp_path, fake_async_session):
    client = _build_client(fake_async_session)

    with patch(
        "api.v1.ppt.endpoints.images.get_images_directory", return_value=str(tmp_path)
    ):
        response = client.post(
            "/images/upload",
            files={"file": ("slide.png", b"not an image", "image/png")},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Uploaded file is not a valid image"
    assert fake_async_session.added == []
    assert fake_async_session.commit_count == 0
    assert list(tmp_path.iterdir()) == []


def test_upload_image_rejects_unsupported_extension(tmp_path, fake_async_session):
    client = _build_client(fake_async_session)

    with patch(
        "api.v1.ppt.endpoints.images.get_images_directory", return_value=str(tmp_path)
    ):
        response = client.post(
            "/images/upload",
            files={"file": ("slide.txt", _png_bytes(), "image/png")},
        )

    assert response.status_code == 400
    assert "Invalid image file type" in response.json()["detail"]
    assert fake_async_session.added == []
    assert fake_async_session.commit_count == 0
    assert list(tmp_path.iterdir()) == []


def test_uploaded_images_endpoint_hides_assets_whose_file_is_missing(
    monkeypatch, tmp_path
):
    app_data = tmp_path / "app_data"
    owner_id = uuid.uuid4()
    existing = app_data / "images" / "users" / str(owner_id) / "keep.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"png")
    missing = app_data / "images" / "users" / str(owner_id) / "gone.png"
    remote = "https://cdn.example.com/remote.png"
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))

    session = _AssetSession(
        [
            ImageAsset(path=str(existing), is_uploaded=True, owner_id=owner_id),
            ImageAsset(path=str(missing), is_uploaded=True, owner_id=owner_id),
            ImageAsset(path=remote, is_uploaded=True, owner_id=owner_id),
        ]
    )
    client = _build_client(session)

    token = set_current_owner_id(owner_id)
    try:
        response = client.get("/images/uploaded")
    finally:
        reset_current_owner_id(token)

    assert response.status_code == 200
    paths = [item["path"] for item in response.json()]
    assert str(existing) in paths
    assert remote in paths
    assert str(missing) not in paths


def test_generated_images_endpoint_keeps_only_available_assets(
    monkeypatch, tmp_path
):
    app_data = tmp_path / "app_data"
    owner_id = uuid.uuid4()
    existing = app_data / "images" / "users" / str(owner_id) / "gen.png"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"png")
    missing = app_data / "images" / "users" / str(owner_id) / "lost.png"
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))

    session = _AssetSession(
        [
            ImageAsset(path=str(missing), is_uploaded=False, owner_id=owner_id),
            ImageAsset(path=str(existing), is_uploaded=False, owner_id=owner_id),
        ]
    )
    client = _build_client(session)

    token = set_current_owner_id(owner_id)
    try:
        response = client.get("/images/generated")
    finally:
        reset_current_owner_id(token)

    assert response.status_code == 200
    paths = [item["path"] for item in response.json()]
    assert paths == [str(existing)]
