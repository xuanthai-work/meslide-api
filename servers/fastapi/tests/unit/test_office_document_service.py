import zipfile

import pytest

from services.office_document_service import (
    OfficeDocumentError,
    extract_office_document_text,
)


def _write_zip(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


def test_extracts_pptx_slide_text_in_slide_order(tmp_path):
    path = tmp_path / "deck.pptx"
    _write_zip(
        path,
        {
            "ppt/slides/slide10.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Ten</a:t></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>Two</a:t></p:sld>",
            "ppt/slides/slide1.xml": "<p:sld xmlns:p='p' xmlns:a='a'><a:t>One</a:t></p:sld>",
        },
    )

    assert extract_office_document_text(str(path)) == "One\n\nTwo\n\nTen"


def test_extracts_docx_paragraphs(tmp_path):
    path = tmp_path / "document.docx"
    _write_zip(
        path,
        {
            "word/document.xml": (
                "<w:document xmlns:w='w'><w:body>"
                "<w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t>world</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>Second paragraph</w:t></w:r></w:p>"
                "</w:body></w:document>"
            )
        },
    )

    assert extract_office_document_text(str(path)) == "Hello world\nSecond paragraph"


def test_extracts_docx_automatic_numbering_including_empty_list_cells(tmp_path):
    path = tmp_path / "numbered-table.docx"
    _write_zip(
        path,
        {
            "word/document.xml": (
                "<w:document xmlns:w='w'><w:body><w:tbl>"
                "<w:tr><w:tc><w:p><w:pPr><w:numPr><w:ilvl w:val='0'/>"
                "<w:numId w:val='1'/></w:numPr></w:pPr></w:p></w:tc>"
                "<w:tc><w:p><w:r><w:t>Topic one</w:t></w:r></w:p></w:tc></w:tr>"
                "<w:tr><w:tc><w:p><w:pPr><w:numPr><w:ilvl w:val='0'/>"
                "<w:numId w:val='1'/></w:numPr></w:pPr></w:p></w:tc>"
                "<w:tc><w:p><w:r><w:t>Topic two</w:t></w:r></w:p></w:tc></w:tr>"
                "</w:tbl></w:body></w:document>"
            ),
            "word/numbering.xml": (
                "<w:numbering xmlns:w='w'>"
                "<w:abstractNum w:abstractNumId='0'><w:lvl w:ilvl='0'>"
                "<w:start w:val='1'/><w:numFmt w:val='decimal'/>"
                "<w:lvlText w:val='%1'/></w:lvl></w:abstractNum>"
                "<w:num w:numId='1'><w:abstractNumId w:val='0'/></w:num>"
                "</w:numbering>"
            ),
        },
    )

    assert extract_office_document_text(str(path)) == "1\nTopic one\n2\nTopic two"


def test_rejects_legacy_binary_office_formats(tmp_path):
    path = tmp_path / "legacy.ppt"
    path.write_bytes(b"legacy")

    with pytest.raises(OfficeDocumentError, match="external office conversion engine"):
        extract_office_document_text(str(path))
