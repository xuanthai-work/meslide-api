from io import BytesIO

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from constants.documents import UPLOAD_ACCEPTED_FILE_TYPES
from utils.coded_exception import CodedHTTPException
from utils.validators import validate_files


@pytest.mark.parametrize(
    ("filename", "content_type"),
    (
        ("legacy.doc", "application/msword"),
        ("legacy.ppt", "application/vnd.ms-powerpoint"),
        ("legacy.xls", "application/vnd.ms-excel"),
        ("legacy.rtf", "application/rtf"),
        ("legacy.rtf", "text/rtf"),
    ),
)
def test_document_upload_rejects_legacy_binary_office_formats(
    filename: str, content_type: str
):
    upload = UploadFile(
        filename=filename,
        file=BytesIO(b"legacy"),
        headers=Headers({"content-type": content_type}),
    )

    with pytest.raises(CodedHTTPException) as caught:
        validate_files(
            upload,
            nullable=False,
            multiple=False,
            max_size=100,
            accepted_types=UPLOAD_ACCEPTED_FILE_TYPES,
        )

    assert caught.value.error_code == "error.file.type_not_accepted"
