import json

from services.document_text import clean_extracted_document_text


def test_plain_text_is_not_rewritten():
    assert clean_extracted_document_text("a\nb") == "a\nb"


def test_clean_extracts_truncated_nested_liteparse_text():
    inner = (
        '{"ok": true, "filePath": "/inner.pdf", "text": '
        '"first\\nsecond\\u0021 and \\"quoted\\"'
    )
    outer = json.dumps({"ok": True, "filePath": "/outer.pdf", "text": inner})

    assert clean_extracted_document_text(outer) == 'first\nsecond! and "quoted"'
