# Backend Document Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tách chọn parser và làm sạch text trong `DocumentsLoader`, giữ temp path/OCR/fallback và kết quả upload.
**Architecture:** Loader vẫn sở hữu thứ tự file, temp-file resolution, parser services và assignment `_documents/_images`. Module text thuần xử lý LiteParse JSON; dispatch theo extension thành method nhỏ trong loader.
**Tech Stack:** Python 3.11, asyncio, pdfplumber, LiteParse fake, pytest.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0; nhánh riêng từ 0. Không cần Postgres/S3/LLM/OCR thật.
- Giữ extension priority PDF → text → Office → image → LiteParse, thứ tự tài liệu và image lists, exact HTTP detail/status.
- Không đổi `TEMP_FILE_SERVICE`, temp directory lifetime, OCR language/DPI, timeout, LiteParse fallback/cleanup hoặc upload route.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| Nhiều file khác định dạng trả sai thứ tự | Task 1 và `test_documents_loader.py` |
| PDF ảnh thiếu temp_dir đổi lỗi 400 | `test_load_pdf_requires_temp_dir_when_images_are_requested` |
| Office numbering/text đổi | `test_office_document_service.py` |
| LiteParse malformed JSON bị cắt text | Task 2 và `test_documents_loader_unwrap.py` |
| Parser chính lỗi nhưng fallback hoạt động | Task 3 và `test_documents_loader.py` |

## File map

| File dưới `servers/fastapi` | Vai trò |
| --- | --- |
| `services/documents_loader.py` | Dispatch, parser services, temp lifecycle. |
| `services/document_text.py` | LiteParse unwrap và cleaning thuần. |
| `services/document_conversion_service.py` | Giữ nguyên image conversion. |
| `tests/unit/test_document_text.py` | Cleaning edge cases. |
| `tests/unit/test_documents_loader.py` | Dispatch/PDF/image fake hiện có. |

### Task 1: Parser dispatch một file

**Files:** Modify `services/documents_loader.py:192-244`; extend `tests/unit/test_documents_loader.py`.
**Interfaces:** Produce `DocumentsLoader._load_one_document(file_path: str, load_text: bool, load_images: bool, temp_dir: str | None) -> tuple[str, list[str]]`.

- [ ] Thêm characterization test vào `test_documents_loader.py`:
  ```python
  import asyncio
  from services.documents_loader import DocumentsLoader
  from services.temp_file_service import TEMP_FILE_SERVICE

  def test_mixed_text_office_keeps_input_order(tmp_path, monkeypatch):
      text_file = tmp_path / "a.txt"
      text_file.write_text("first", encoding="utf-8")
      office_file = tmp_path / "b.docx"
      office_file.write_bytes(b"fake")
      monkeypatch.setattr(
          TEMP_FILE_SERVICE, "resolve_existing_temp_paths", lambda paths: paths
      )
      monkeypatch.setattr(
          DocumentsLoader, "load_office_document", staticmethod(lambda _: "second")
      )
      loader = DocumentsLoader([str(text_file), str(office_file)])
      asyncio.run(loader.load_documents())
      assert loader.documents == ["first", "second"]
      assert loader.images == [[], []]
  ```
  Chạy test; kỳ vọng PASS trên baseline.
- [ ] Chuyển nguyên nhánh `extension in PDF_EXTENSIONS/TEXT_EXTENSIONS/OFFICE_EXTENSIONS/IMAGE_EXTENSIONS/else` vào method. `load_documents` giữ `os.path.exists`, logger, `clean_extracted_document_text`, `append` và assignment sau vòng lặp:
  ```python
  document, imgs = await self._load_one_document(
      file_path, load_text, load_images, temp_dir
  )
  document = clean_extracted_document_text(document)
  documents.append(document)
  images.append(imgs)
  ```
  Không chuyển `asyncio.to_thread` ra khỏi nhánh đang dùng.
- [ ] Chạy `test_documents_loader.py`, `test_office_document_service.py`, `test_document_upload_formats.py`; kỳ vọng PASS. Commit `refactor: isolate document parser dispatch`.

### Task 2: Text normalization thuần

**Files:** Create `services/document_text.py`, `tests/unit/test_document_text.py`; modify `documents_loader.py:40-160`.
**Interfaces:** Move unchanged `_unwrap_liteparse_json_line_if_stored`, `_json_unescape_quoted_value`, `_try_extract_liteparse_text_value_from_malformed_json`, `_clean_extracted_one_pass`, `clean_extracted_document_text`; re-export old names from loader.

- [ ] Thêm test:
  ```python
  from services.document_text import clean_extracted_document_text

  def test_plain_text_is_not_rewritten():
      assert clean_extracted_document_text("a\nb") == "a\nb"
  ```
  Chạy test mới; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên năm function và import `json`/`re` cần thiết; tại loader:
  ```python
  from services.document_text import (
      _unwrap_liteparse_json_line_if_stored,
      clean_extracted_document_text,
  )
  ```
  Re-export thêm tên private nếu `rg` thấy caller/test import chúng; không đổi recursion/loop limit của cleaning.
- [ ] Chạy test mới, `test_documents_loader.py` và `tests/test_documents_loader_unwrap.py`; kỳ vọng PASS. Commit `refactor: isolate document text normalization`.

### Task 3: Fallback/cleanup gate

**Files:** Review `documents_loader.py:246-354`, `document_conversion_service.py`; tests `test_documents_loader.py` và `test_document_profile_service.py`.
**Interfaces:** Keep `DocumentsLoader._parse_with_liteparse` and `DocumentConversionService.convert_image_to_png` unchanged.

- [ ] Thêm characterization test vào `test_documents_loader.py`:
  ```python
  from unittest.mock import MagicMock
  from services.liteparse_service import LiteParseError

  def test_liteparse_fallback_returns_text_from_secondary_parser():
      loader = DocumentsLoader(file_paths=[])
      loader.liteparse_service = MagicMock()
      loader.liteparse_service.parse_to_markdown.side_effect = LiteParseError("broken")
      loader.document_service = MagicMock()
      loader.document_service.parse_to_markdown.return_value = "fallback"
      assert loader._parse_with_liteparse("/tmp/a.pdf") == "fallback"
  ```
  Chạy test mới cùng các test PDF scanned/threshold và PNG conversion; kỳ vọng PASS trên baseline.
- [ ] So diff xác nhận `_parse_with_liteparse` vẫn bắt đúng `LiteParseError`/`DocumentConversionError`/`OfficeDocumentError`, gọi fallback một lần, giữ 500 detail nếu cả hai lỗi. Không đổi temp directory creation/cleanup. Task này là gate review, không commit rỗng.

## Gate toàn đợt

Ruff, Vulture, Deptry và toàn `uv run --locked python tests/run_suite.py -q` trong `servers/fastapi`; `npm test` và `node --test tests/*.test.mjs` ở root. OpenAPI test đợt 0 phải pass. Review diff để xác nhận không thay temp policy, upload route, DB/storage.
