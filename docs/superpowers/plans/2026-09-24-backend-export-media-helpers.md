# Backend Export and Media Helpers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tách export payload và font URL mapping khỏi adapter/subprocess, giữ output path và public media URL.
**Architecture:** `ExportTaskService` vẫn chạy child process và xác thực output path; module mới chỉ tạo dict payload. `asset_directory_utils.py` sở hữu phép đổi path font thành URL, `font_uploads.py` giữ API và re-export cũ.
**Tech Stack:** Python 3.11, FastAPI, pytest, Node test gates.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0; phối hợp với đợt 1–3 về presentation/template payload nhưng không cần DB/S3 local.
- Không đổi `ExportTaskService._run_task`, child process, timeout, output path/owner scope, S3/local storage calls hay runtime `presentation-export`.
- Giữ PDF/PPTX/preview/font/image/icon result, lỗi/status, cookie và `/app_data`/`/static` URL.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| JSON-to-image bỏ font hoặc đổi image source | Task 1 và `test_asset_ownership_security.py` |
| PPTX-to-JSON bỏ key optional khi `None` | Task 1 |
| Same-origin/split API font URL khác slide URL | Task 2 và `test_asset_url_contract.py` |
| Cookie legacy/session rò sai export URL | `test_export_cookie_header.py` |
| Output path hoặc image endpoint owner scope đổi | `test_asset_ownership_security.py` và `test_images_endpoints.py` |

## File map

| File dưới `servers/fastapi` | Vai trò |
| --- | --- |
| `services/export_task_service.py` | Runtime, subprocess, path validation giữ nguyên. |
| `services/export_payloads.py` | Dict payload thuần. |
| `services/export_image_assets.py` | Embed local image asset khi dựng JSON preview. |
| `utils/asset_directory_utils.py` | Public URL và path font. |
| `utils/font_uploads.py` | Font model, persistence, compatibility import. |
| `tests/unit/test_export_payloads.py` | Payload shape mới. |
| `tests/unit/test_export_image_assets.py` | Local/remote image source. |
| `tests/unit/test_font_asset_url.py` | Same-origin và split origin. |

### Task 1: Export task payload

**Files:** Create `services/export_payloads.py`, `tests/unit/test_export_payloads.py`; modify `services/export_task_service.py:621-744`.
**Interfaces:** Produce `json_to_image_payload(localized_data, width, height, fonts=None) -> dict[str, Any]` and `pptx_to_json_payload(pptx_path: str, slide_concurrency: int | None) -> dict[str, Any]`. Keep filesystem localization in `export_task_service.py` to avoid a circular import.

- [ ] Thêm test trước:
  ```python
  from services.export_payloads import pptx_to_json_payload

  def test_pptx_payload_omits_optional_concurrency_when_none():
      assert pptx_to_json_payload("/tmp/deck.pptx", None) == {
          "type": "pptx-to-json", "pptx_path": "/tmp/deck.pptx"
      }
      assert pptx_to_json_payload("/tmp/deck.pptx", 2)["slide_concurrency"] == 2
  ```
  Chạy `tests/unit/test_export_payloads.py`; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên dict tạo task từ `render_json_to_image` và `convert_pptx_to_json` sang helper. `json_to_image_payload` chỉ thêm `fonts` khi truthy; caller vẫn localize assets trước khi tạo payload. Call site:
  ```python
  task_payload = json_to_image_payload(
      _localize_json_image_assets(data), width, height, fonts
  )
  task_payload = pptx_to_json_payload(pptx_path, slide_concurrency)
  ```
  Hai dòng trên thay ở hai method tương ứng, không cùng scope. Giữ validation trước `_run_task`, lỗi JSON, `_resolve_output_path`, `_scope_conversion_artifacts`.
- [ ] Thêm test:
  ```python
  from services.export_payloads import json_to_image_payload

  def test_json_image_payload_keeps_fonts_and_dimensions():
      data = [{"type": "text", "data": "hello"}]
      assert json_to_image_payload(data, 800, 600, {"A": "/a"}) == {
          "type": "json-to-image", "data": data,
          "width": 800, "height": 600, "fonts": {"A": "/a"},
      }
  ```
  Chạy test mới, `test_export_cookie_header.py`, `test_asset_ownership_security.py`; kỳ vọng PASS. Commit `refactor: isolate export task payloads`.

### Task 2: Image source cho JSON preview

**Files:** Create `services/export_image_assets.py`, `tests/unit/test_export_image_assets.py`; modify `services/export_task_service.py:36-88`.
**Interfaces:** Move `_localize_json_image_assets(value: Any, data_uri_cache: dict[str, str] | None = None) -> Any` unchanged; re-export it from `export_task_service.py`.

- [ ] Thêm test:
  ```python
  from services.export_image_assets import _localize_json_image_assets

  def test_remote_image_source_is_preserved():
      source = [{"type": "image", "data": "https://cdn.example.test/image.png"}]
      assert _localize_json_image_assets(source) == source

  def test_local_image_source_embeds_file_bytes(monkeypatch, tmp_path):
      image = tmp_path / "image.png"
      image.write_bytes(b"png")
      monkeypatch.setattr(
          "services.export_image_assets.resolve_app_path_to_filesystem",
          lambda _path: str(image),
      )
      source = [{"type": "image", "data": "/app_data/images/image.png"}]
      assert _localize_json_image_assets(source)[0]["data"] == (
          "data:image/png;base64,cG5n"
      )
  ```
  Chạy `tests/unit/test_export_image_assets.py`; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên function từ `export_task_service.py:36-88`. Giữ `resolve_app_path_to_filesystem`, URL parsing, MIME fallback, cache theo resolved path và OSError fallback. Trong runtime import/re-export:
  ```python
  from services.export_image_assets import _localize_json_image_assets
  ```
  `render_json_to_image` vẫn gọi function trước khi dựng payload Task 1.
- [ ] Chạy test mới, `test_asset_ownership_security.py`, `test_images_endpoints.py` và `test_export_payloads.py`; kỳ vọng PASS. Commit `refactor: isolate export image asset localization`.

### Task 3: Font public URL mapping

**Files:** Modify `utils/asset_directory_utils.py`, `utils/font_uploads.py:173-186`; create `tests/unit/test_font_asset_url.py`.
**Interfaces:** Produce `font_path_to_url(path: str) -> str` in `asset_directory_utils.py`; `font_uploads._font_path_to_url` delegates to it.

- [ ] Thêm test:
  ```python
  from utils.asset_directory_utils import font_path_to_url

  def test_font_url_uses_public_api_origin(monkeypatch, tmp_path):
      monkeypatch.setenv("APP_DATA_DIRECTORY", str(tmp_path))
      monkeypatch.setenv("NEXT_PUBLIC_FAST_API", "https://api.example.test/")
      path = tmp_path / "fonts" / "A.ttf"
      assert font_path_to_url(str(path)) == "https://api.example.test/app_data/fonts/A.ttf"
  ```
  Chạy test mới; kỳ vọng FAIL vì helper chưa có.
- [ ] Chuyển nguyên logic `os.path.abspath/commonpath/relpath` từ `font_uploads._font_path_to_url` sang `font_path_to_url`, dùng `absolute_fastapi_asset_url` hiện có. Giữ wrapper:
  ```python
  def _font_path_to_url(path: str) -> str:
      return font_path_to_url(path)
  ```
  Không đổi `persist_font_file`, `backfill_font_uploads_from_disk` hoặc DB writes.
- [ ] Chạy test mới, `test_font_upload_limits.py`, `test_pptx_font_utils.py`, `test_asset_url_contract.py`; kỳ vọng PASS. Commit `refactor: centralize font asset URL mapping`.

### Task 4: Media/export boundary review

**Files:** Review `api/v1/ppt/endpoints/icons.py`, `images.py`, `fonts.py`, `utils/export_utils.py`, `services/export_task_service.py`.
**Interfaces:** No new interface; keep `ICON_FINDER_SERVICE.search_icons`, `_build_presentation_export_url` and export runtime methods unchanged.

- [ ] Chạy `test_icon_weights.py`, `test_images_endpoints.py`, `test_export_cookie_header.py` và `test_asset_ownership_security.py`; kỳ vọng PASS.
- [ ] So diff để xác nhận không đổi image/icon provider calls, font persistence, output path validation, cookie forwarding và subprocess. Task này là review gate, không tạo commit rỗng.

## Gate toàn đợt

Trong `servers/fastapi` chạy Ruff, Vulture, Deptry và toàn `uv run --locked python tests/run_suite.py -q`; ở root chạy `npm test` và `node --test tests/*.test.mjs`. OpenAPI và asset URL test đợt 0 phải pass. Không cần Chromium/S3/Postgres thật ở local; adapter được fake.
