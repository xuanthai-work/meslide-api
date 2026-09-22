from fastapi import FastAPI

from api.v1.ppt.router import API_V1_PPT_ROUTER
from models.chat import ChatMessageRequest
from models.presentation_with_slides import PresentationWithSlides
from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel


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
