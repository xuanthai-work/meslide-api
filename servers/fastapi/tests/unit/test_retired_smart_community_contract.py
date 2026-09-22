import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.ppt.router import API_V1_PPT_ROUTER
from models.chat import ChatMessageRequest
from models.presentation_with_slides import PresentationWithSlides
from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from services.database import get_async_session


def _openapi_schema() -> dict:
    app = FastAPI()
    app.include_router(API_V1_PPT_ROUTER)
    return app.openapi()


def test_retired_community_and_mode_routes_are_absent() -> None:
    paths = _openapi_schema()["paths"]

    assert "/api/v1/ppt/community/presentations" not in paths
    assert "/api/v1/ppt/community/presentations/{community_id}" not in paths
    assert "/api/v1/ppt/presentation/{id}/mode" not in paths


def test_smart_html_edit_route_is_absent() -> None:
    paths = _openapi_schema()["paths"]

    assert "/api/v1/ppt/slide/edit-html" not in paths
    assert "/api/v1/ppt/slide/edit" in paths


def test_presentation_response_has_no_retired_mode_fields() -> None:
    fields = PresentationWithSlides.model_fields

    assert "generation_mode" not in fields
    assert "type" not in fields
    assert "community_design_ids" not in fields


def test_chat_request_has_no_presentation_type_switch() -> None:
    assert "presentation_type" not in ChatMessageRequest.model_fields


def test_runtime_sql_models_have_no_retired_fields() -> None:
    assert "generation_mode" not in PresentationModel.model_fields
    assert "community_design_ids" not in PresentationModel.model_fields
    assert "html_content" not in SlideModel.model_fields


def test_openapi_contains_no_retired_symbols() -> None:
    serialized = str(_openapi_schema()).lower()
    for retired in (
        "community_design_ids",
        "generation_mode",
        "html_content",
        "smart presentation",
    ):
        assert retired not in serialized


@pytest.mark.parametrize(
    ("retired_field", "retired_value"),
    (
        ("generation_mode", "smart"),
        ("community_design_ids", ["legacy-design"]),
        ("type", "smart"),
    ),
)
def test_create_rejects_retired_mode_fields(
    fake_async_session,
    retired_field: str,
    retired_value: object,
) -> None:
    app = FastAPI()
    app.include_router(API_V1_PPT_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: fake_async_session
    client = TestClient(app)

    response = client.post(
        "/api/v1/ppt/presentation/create",
        json={
            "content": "Standard deck",
            "include_table_of_contents": False,
            retired_field: retired_value,
        },
    )

    assert response.status_code == 422
    assert fake_async_session.added == []


def test_create_accepts_current_standard_body(fake_async_session) -> None:
    app = FastAPI()
    app.include_router(API_V1_PPT_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: fake_async_session
    client = TestClient(app)

    response = client.post(
        "/api/v1/ppt/presentation/create",
        json={
            "content": "Standard deck",
            "include_table_of_contents": False,
        },
    )

    assert response.status_code == 200
    assert response.json()["version"] == "v2-standard"
