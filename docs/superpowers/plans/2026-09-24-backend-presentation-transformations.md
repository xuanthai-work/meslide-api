# Backend Presentation Transformations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tách phép tính prepare, TOC và chuyển đổi slide khỏi route presentation mà không đổi kết quả.
**Architecture:** Dùng `presentation_layout.py` và `presentation_template_content.py` sẵn có. Route giữ validation, DB, memory write và commit; helper nhận dữ liệu đã resolve.
**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0 ở `fix/api-ref-0`; nhánh mới từ commit chứa test contract.
- Chỉ backend; giữ 78 operations, 119 schemas, lỗi/status/SSE/task/URL hiện tại.
- Không sửa SQL, model, session, thứ tự commit, storage, auth hoặc Web.
- Giữ symbol test/caller import từ module cũ và nguồn random mà test patch.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| Structure trả index ngoài số layout hoặc thừa slide | Task 1: test helper mới |
| Random đã patch từ route không còn được dùng | Task 1 và generation flow |
| TOC sai vị trí khi có title slide | Task 2 và outline_utils |
| Update slide đổi trường immutable/commit | Slide update API trong gate Task 3 |
| Template markdown mất bold/italic khi hydrate | Task 3 và template V2 UI |

## File map

| File (dưới `servers/fastapi`) | Vai trò |
| --- | --- |
| `api/v1/ppt/endpoints/presentation.py` | Route prepare; giữ SQL/commit/Mem0. |
| `api/v1/ppt/endpoints/presentation_layout.py` | Chuẩn hóa structure và chèn TOC. |
| `api/v1/ppt/endpoints/presentation_template_content.py` | UI/content hiện có; giữ public import. |
| `api/v1/ppt/endpoints/presentation_markdown.py` | Parse markdown text thành styled runs. |
| `tests/unit/test_presentation_prepare_structure.py` | Helper mới. |
| `tests/integration/test_presentation_generation_flow.py` | Prepare/generation contract hiện có. |

### Task 1: Chuẩn hóa structure

**Files:** Modify `presentation.py:188-201`, `presentation_layout.py`; create `tests/unit/test_presentation_prepare_structure.py`.
**Interfaces:** Produce `_normalize_prepared_structure(structure: PresentationStructureModel, total_outlines: int, total_slide_layouts: int, randint: Callable[[int, int], int]) -> None`.

- [ ] Viết test trước:
  ```python
  from models.presentation_structure_model import PresentationStructureModel
  from api.v1.ppt.endpoints.presentation_layout import _normalize_prepared_structure

  def test_structure_replaces_invalid_index_and_truncates():
      value = PresentationStructureModel(slides=[0, 99, 1])
      calls = []
      def choose(low, high):
          calls.append((low, high))
          return 1
      _normalize_prepared_structure(value, 2, 2, choose)
      assert value.slides == [0, 1]
      assert calls == [(0, 1), (0, 1)]
  ```
- [ ] Chạy `uv run --locked python tests/run_suite.py -q tests/unit/test_presentation_prepare_structure.py`; kỳ vọng FAIL do thiếu helper.
- [ ] Chuyển nguyên vòng chuẩn hóa từ route vào helper; vẫn gọi `randint(0, total_slide_layouts - 1)` ở mỗi iteration. Route gọi:
  ```python
  _normalize_prepared_structure(
      presentation_structure, total_outlines, total_slide_layouts, random.randint
  )
  ```
- [ ] Chạy test mới và `tests/integration/test_presentation_generation_flow.py`; kỳ vọng PASS. Commit `refactor: isolate prepared structure normalization`.

### Task 2: TOC tại layout boundary

**Files:** Modify `presentation.py:203-222` và `presentation_layout.py`.
**Interfaces:** Consume structure Task 1; produce `_prepare_toc_layouts(presentation, outline, structure, structure_layout, total_outlines) -> PresentationOutlineModel`.

- [ ] Ghi baseline bằng `test_prepare_presentation_accepts_template_layout_id`, `test_prepare_presentation_clears_stale_language_for_reviewed_outlines` và `tests/unit/test_outline_utils.py`; kỳ vọng PASS.
- [ ] Chuyển nguyên block `if presentation.include_table_of_contents:` sau chuẩn hóa sang helper. Giữ thứ tự `get_no_of_toc_required_for_n_outlines` → `select_toc_or_list_slide_layout_index` → `_insert_toc_layouts` → `get_presentation_outline_model_with_toc`. Route dùng:
  ```python
  presentation_outline_model = _prepare_toc_layouts(
      presentation, presentation_outline_model, presentation_structure,
      structure_layout, total_outlines,
  )
  ```
  Helper trả outline gốc nếu không có TOC. Không đổi SQL/commit/Mem0 sau đó.
- [ ] Chạy lại các test baseline và toàn `tests/integration/test_presentation_generation_flow.py`; kỳ vọng PASS. Commit `refactor: isolate presentation TOC preparation`.

### Task 3: Markdown parsing của template content

**Files:** Create `presentation_markdown.py`, `tests/unit/test_presentation_markdown.py`; modify `presentation_template_content.py:70-75,595-668`.
**Interfaces:** Move `_parse_template_markdown_text(text: str) -> list[tuple[str, dict[str, bool]]]` and its two delimiter helpers; re-export parser from `presentation_template_content.py`.

- [ ] Viết test trước:
  ```python
  from api.v1.ppt.endpoints.presentation_markdown import _parse_template_markdown_text

  def test_markdown_parser_preserves_text_and_bold_runs():
      assert _parse_template_markdown_text("a **b** c") == [
          ("a ", {}), ("b", {"bold": True}), (" c", {}),
      ]
  ```
  Chạy `tests/unit/test_presentation_markdown.py`; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên constants `TEMPLATE_*MARKDOWN_DELIMITERS` và ba function parser vào module mới. Trong module content giữ re-export:
  ```python
  from .presentation_markdown import _parse_template_markdown_text
  ```
  `_template_text_runs_from_markdown` tiếp tục gọi tên cũ; không đổi font merge hoặc UI hydration.
- [ ] Chạy test mới, `test_presentation_template_content.py`, `test_presentation_template_v2_ui.py`, `test_presentation_slide_update_api.py` và `test_presentation_stream_resume.py`; kỳ vọng PASS. Commit `refactor: isolate presentation markdown parsing`.

## Gate toàn đợt

Trong `servers/fastapi` chạy `uv run --locked ruff check .`, `uv run --locked vulture`, `uv run --locked deptry .`, `uv run --locked python tests/run_suite.py -q`. Ở root chạy `npm test` và `node --test tests/*.test.mjs`. Test OpenAPI đợt 0 phải pass. Review diff: không SQL/model/storage/Web thay đổi. Pyright chỉ chốt ở CI Linux `.venv`.
