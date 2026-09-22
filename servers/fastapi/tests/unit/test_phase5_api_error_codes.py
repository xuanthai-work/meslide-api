import asyncio
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import UploadFile

from api.v1.ppt.endpoints.files import upload_files
from api.v1.ppt.endpoints.images import _read_validated_image_upload
from api.v1.ppt.endpoints.presentation import create_presentation
from api.v1.ppt.endpoints.template import _require_private_template
from api.v1.ppt.endpoints.theme import _resolve_logo_url
from models.presentation_create_request import CreatePresentationRequest
from services.export_task_service import ExportTaskService
from utils.coded_exception import CodedHTTPException
from utils.provider_error_messages import classify_raw_provider_error
from utils.validators import validate_files


def _assert_code(expected: str, call):
    with pytest.raises(CodedHTTPException) as caught:
        call()
    assert caught.value.error_code == expected
    return caught.value


def test_presentation_validation_has_stable_code():
    async def run():
        await create_presentation(
            payload=CreatePresentationRequest(content="topic", n_slides=0)
        )

    _assert_code("error.slides_must_be_positive", lambda: asyncio.run(run()))


def test_invalid_image_type_has_stable_code_and_params():
    async def run():
        await _read_validated_image_upload(
            UploadFile(filename="notes.txt", file=BytesIO(b"not an image"))
        )

    error = _assert_code("error.image.invalid_type", lambda: asyncio.run(run()))
    assert ".png" in error.error_params["accepted"]


def test_files_endpoint_and_validator_have_stable_codes():
    async def run():
        await upload_files(None)

    _assert_code("error.file.documents_required", lambda: asyncio.run(run()))
    _assert_code(
        "error.file.required",
        lambda: validate_files(
            None,
            nullable=False,
            multiple=False,
            max_size=5,
            accepted_types=[".pdf"],
        ),
    )


def test_template_and_theme_helpers_have_stable_codes():
    _assert_code(
        "error.template.builtin_readonly",
        lambda: _require_private_template(SimpleNamespace(is_default=True)),
    )

    async def resolve_bad_logo():
        await _resolve_logo_url(SimpleNamespace(), "not-a-uuid")

    _assert_code("error.theme.invalid_logo_id", lambda: asyncio.run(resolve_bad_logo()))


def test_export_helper_has_stable_code():
    _assert_code(
        "error.export.no_output_path",
        lambda: ExportTaskService._resolve_output_path({}),
    )


@pytest.mark.parametrize(
    ("status_code", "code", "message", "expected"),
    [
        (401, "invalid_api_key", "invalid API key", "error.provider.invalid_api_key"),
        (400, "moderation_blocked", "blocked by safety system", "error.provider.safety_blocked"),
        (429, "insufficient_quota", "quota exceeded", "error.provider.quota"),
        (429, "rate_limit_exceeded", "rate limited", "error.provider.rate_limited"),
    ],
)
def test_provider_errors_classify_to_stable_codes(
    status_code, code, message, expected
):
    classification = classify_raw_provider_error(
        "OpenAI",
        "generation",
        status_code=status_code,
        code=code,
        message=message,
    )
    assert classification is not None
    assert classification[0] == expected
