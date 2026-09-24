import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.ppt.endpoints.images import IMAGES_ROUTER
from api.v1.ppt.endpoints.template import TEMPLATE_ROUTER
from services.database import get_async_session
from tests.conftest import FakeAsyncSession
from utils.coded_exception import register_coded_exception_handler


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(TEMPLATE_ROUTER)
    app.include_router(IMAGES_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: FakeAsyncSession()
    register_coded_exception_handler(app)
    return TestClient(app)


def test_missing_template_keeps_coded_404_body():
    response = _client().get("/template/missing")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Template not found",
        "error_code": "error.template.not_found",
    }


def test_missing_image_delete_keeps_current_wrapped_error_body():
    image_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    response = _client().delete(f"/images/{image_id}")
    assert response.status_code == 500
    assert response.json() == {
        "detail": "Failed to delete image: 404: Image not found",
        "error_code": "error.image.delete_failed",
        "error_params": {"reason": "404: Image not found"},
    }
