from io import BytesIO
import os

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from constants.documents import (
    TEXT_EXTENSIONS,
    UPLOAD_ACCEPTED_EXTENSIONS,
    UPLOAD_ACCEPTED_FILE_TYPES,
)
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


def test_markdown_is_accepted_as_a_plain_text_document():
    assert TEXT_EXTENSIONS == [".txt", ".md", ".markdown"]
    for extension in (".md", ".markdown"):
        assert extension in UPLOAD_ACCEPTED_EXTENSIONS
        assert extension in UPLOAD_ACCEPTED_FILE_TYPES


@pytest.mark.parametrize(
    ("filename", "content_type"),
    (
        ("notes.md", "text/markdown"),
        ("notes.md", "text/plain"),
        ("notes.markdown", "text/markdown"),
        ("notes.markdown", "text/plain"),
        ("UPPER.MD", "text/markdown"),
    ),
)
def test_document_upload_accepts_markdown_files(
    filename: str, content_type: str
):
    upload = UploadFile(
        filename=filename,
        file=BytesIO(b"# Title\n\nBody"),
        headers=Headers({"content-type": content_type}),
    )

    validate_files(
        upload,
        nullable=False,
        multiple=False,
        max_size=100,
        accepted_types=UPLOAD_ACCEPTED_FILE_TYPES,
    )


def test_decompose_routes_markdown_through_the_plain_text_path(tmp_path, monkeypatch):
    """Markdown is already text, so it is returned without the loader."""
    import asyncio

    from api.v1.ppt.endpoints import files as files_endpoint

    managed_dir = tmp_path / "managed-temp"
    managed_dir.mkdir()
    monkeypatch.setattr(
        files_endpoint.TEMP_FILE_SERVICE, "base_dir", str(managed_dir), raising=False
    )
    monkeypatch.setattr(
        files_endpoint.TEMP_FILE_SERVICE,
        "create_temp_dir",
        lambda name: str(managed_dir),
    )
    monkeypatch.setattr(
        files_endpoint.TEMP_FILE_SERVICE,
        "resolve_existing_temp_paths",
        lambda paths: paths,
    )

    markdown_file = tmp_path / "notes.md"
    markdown_file.write_text("# Title", encoding="utf-8")
    long_form_file = tmp_path / "notes.markdown"
    long_form_file.write_text("Body", encoding="utf-8")
    pdf_file = tmp_path / "deck.pdf"
    pdf_file.write_bytes(b"fake")

    class _StubLoader:
        def __init__(self, file_paths, presentation_language=None):
            self._file_paths = file_paths

        async def load_documents(self, temp_dir):
            # The loader must only ever see the non plain-text files.
            assert self._file_paths == [str(pdf_file)]

        @property
        def documents(self):
            return ["pdf text"]

    monkeypatch.setattr(files_endpoint, "DocumentsLoader", _StubLoader)

    result = asyncio.run(
        files_endpoint.decompose_files(
            [str(markdown_file), str(long_form_file), str(pdf_file)]
        )
    )

    by_name = {item.name: item.file_path for item in result}
    assert by_name["notes.md"] == str(markdown_file)
    assert by_name["notes.markdown"] == str(long_form_file)
    # Parsed files are re-emitted as .txt, plain text keeps its original path.
    assert by_name["deck.pdf"] != str(pdf_file)
    assert by_name["deck.pdf"].endswith(".txt")
    assert os.path.exists(by_name["deck.pdf"])
