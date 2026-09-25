# Backend Template Generation and Editing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tách ép kiểu payload, asset metadata và merge layout khỏi `template.py`, giữ nguyên bundle/DB.
**Architecture:** Hai module thuần nằm cạnh route; route vẫn sở hữu Pydantic request, generation, task status, DB và `TemplateBundleRepository`. Re-export các tên cũ cần cho test/caller.
**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0 và 1; nhánh riêng từ kết quả đợt 1.
- Không đổi schema/query/commit, bundle upload/hydrate protocol, background task, S3/local implementation.
- Giữ legacy/bundled response, layout index/id, font/icon/thumbnail fallback, 78 OpenAPI operations.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| Patch layout ngoài thứ tự hoặc trùng ID | `test_patch_template_slide_layout_merges_out_of_order_init_saves` và duplicate-ID test |
| Một slide generation lỗi rồi fallback raw | `test_create_template_async_persists_when_one_slide_layout_fails` |
| Font map có key/value trắng, sai type | Task 1 |
| Legacy row thiếu bundle ref | `test_get_template_legacy_row_keeps_working` |
| Progress theo index hoàn thành lệch thứ tự | `test_template_task_contract.py` và async progress test |

## File map

| File dưới `servers/fastapi` | Vai trò |
| --- | --- |
| `api/v1/ppt/endpoints/template.py` | Route/task/persistence và re-export. |
| `api/v1/ppt/endpoints/template_payloads.py` | Layout và asset metadata thuần. |
| `api/v1/ppt/endpoints/template_layout_merge.py` | Merge layout/index thuần. |
| `tests/unit/test_template_payloads.py` | Normalization test mới. |
| `tests/unit/test_template_layout_merge.py` | Merge test mới. |
| `tests/unit/test_template_layout_ids.py` | Duplicate generated ID. |

### Task 1: Chuẩn hóa payload/asset

**Files:** Create `template_payloads.py`, `tests/unit/test_template_payloads.py`; modify `template.py:642-722`.
**Interfaces:** Move unchanged `_coerce_generated_slide_layouts`, `_coerce_template_slide_layouts`, `_get_template_slide_image_urls`, `_coerce_font_map`, `_get_template_fonts`, `_get_template_thumbnail_from_assets`; re-export from `template.py`.

- [ ] Thêm test trước:
  ```python
  from api.v1.ppt.endpoints.template_payloads import _coerce_font_map

  def test_font_map_drops_blank_and_non_string_entries():
      assert _coerce_font_map({" A ": " /a ", "": "/b", "C": None}) == {"A": "/a"}
  ```
  Chạy `tests/unit/test_template_payloads.py`; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên sáu function; giữ `.strip()`, fallback `None` và thứ tự vòng lặp. Trong `template.py` import lại đúng sáu tên:
  ```python
  from .template_payloads import (
      _coerce_font_map, _coerce_generated_slide_layouts,
      _coerce_template_slide_layouts, _get_template_fonts,
      _get_template_slide_image_urls, _get_template_thumbnail_from_assets,
  )
  ```
- [ ] Chạy test mới, `test_template_api.py`, `test_template_bundle_flows.py` và `test_templates_v2_schema.py`; kỳ vọng PASS. Commit `refactor: isolate template payload normalization`.

### Task 2: Merge layout/index

**Files:** Create `template_layout_merge.py`, `tests/unit/test_template_layout_merge.py`; modify `template.py:829-891`.
**Interfaces:** Move unchanged behavior of `_layout_indexes_from_assets`, `_raw_layout_count`, `_merge_template_layout_items`; return `(SlideLayouts, list[int])`. The new module must not import request models from `template.py`.

- [ ] Thêm test trước:
  ```python
  from api.v1.ppt.endpoints.template_layout_merge import _layout_indexes_from_assets

  def test_indexes_accept_unique_mapping_and_fallback_on_duplicate():
      assert _layout_indexes_from_assets({"layout_indexes": [2, 0]}, 2) == [2, 0]
      assert _layout_indexes_from_assets({"layout_indexes": [1, 1]}, 2) == [0, 1]
  ```
  Chạy test mới; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên ba function; import `_coerce_template_slide_layouts` từ Task 1. Vì `PatchTemplateSlideLayoutItem` định nghĩa trong router, tạo Protocol ngay trong module mới để tránh circular import:
  ```python
  class _LayoutPatchItem(Protocol):
      index: int
      layout: SlideLayout
  ```
  Dùng signature `_merge_template_layout_items(template: TemplateV2, items: Sequence[_LayoutPatchItem]) -> tuple[SlideLayouts, list[int]]` và chuyển toàn bộ thân hiện có tại `template.py:849-891`. Runtime chỉ đọc `index`/`layout`. Giữ `CodedHTTPException` detail/error_code và `sorted(layout_by_index)`. Trong `template.py` re-export:
  ```python
  from .template_layout_merge import (
      _layout_indexes_from_assets, _merge_template_layout_items, _raw_layout_count,
  )
  ```
- [ ] Chạy test mới, `test_template_api.py`, `test_template_bundle_flows.py`; kỳ vọng PASS. Commit `refactor: isolate template layout merge`.

### Task 3: Generated layout ID uniqueness

**Files:** Modify `template.py:482-503`, `template_layout_merge.py`; create `tests/unit/test_template_layout_ids.py`.
**Interfaces:** Move `_ensure_unique_async_slide_layout_ids(layouts: list[SlideLayout]) -> list[SlideLayout]` unchanged; re-export from `template.py`.

- [ ] Thêm test:
  ```python
  from templates.v2.models.layouts import SlideLayout
  from api.v1.ppt.endpoints.template_layout_merge import (
      _ensure_unique_async_slide_layout_ids,
  )

  def test_duplicate_generated_ids_get_stable_suffix_without_mutating_source():
      layout = SlideLayout(id="a", description="", components=[])
      result = _ensure_unique_async_slide_layout_ids([layout, layout])
      assert [item.id for item in result] == ["a", "a_2"]
      assert layout.id == "a"
  ```
  Chạy test mới; kỳ vọng FAIL vì function chưa có trong module mới.
- [ ] Chuyển nguyên function vào `template_layout_merge.py`; giữ `model_copy(deep=True)` và vòng tăng suffix. Trong `template.py` import/re-export:
  ```python
  from .template_layout_merge import _ensure_unique_async_slide_layout_ids
  ```
  Không đổi chỗ gọi sau `asyncio.as_completed` hoặc commit progress.
- [ ] Chạy test mới, `test_template_api.py`, `test_template_task_contract.py`; kỳ vọng PASS. Commit `refactor: isolate generated layout ID normalization`.

### Task 4: Xác nhận boundary task/bundle

**Files:** Review `template.py:461-627,1155-1297`; test `tests/regression/test_template_task_contract.py`.
**Interfaces:** Keep `_generate_slide_layouts_with_task_progress`, `_run_create_template_task` signatures and location.

- [ ] So diff của `_generate_slide_layouts_with_task_progress`, `_create_template_with_task_progress` và `_run_create_template_task`: không di chuyển `asyncio.as_completed`, `_commit_template_task_progress`, bundle upload hay DB commit.
- [ ] Chạy:
  ```text
  tests/unit/test_template_api.py::test_create_template_async_task_updates_slide_status_before_batch_completes
  tests/unit/test_template_api.py::test_patch_template_slide_layout_merges_out_of_order_init_saves
  tests/regression/test_template_task_contract.py
  ```
  Kỳ vọng PASS. Task này là gate review, không tạo commit rỗng.

## Gate toàn đợt

Trong `servers/fastapi` chạy Ruff, Vulture, Deptry và `uv run --locked python tests/run_suite.py -q`; ở root chạy `npm test` và `node --test tests/*.test.mjs`. Test OpenAPI đợt 0 phải pass. Review diff: chỉ route/helper/test, không SQL/model/storage implementation.
