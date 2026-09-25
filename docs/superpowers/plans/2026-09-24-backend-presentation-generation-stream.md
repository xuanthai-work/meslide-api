# Backend Presentation Generation and Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Đặt tên các pha generation/stream và gom format SSE lặp lại, giữ thứ tự frame và side effect.
**Architecture:** Route/coroutine vẫn sở hữu session, task scheduling, commit và rollback. Tách validation và serializer thuần; giữ riêng hai vòng asset event vì chúng có lịch commit khác nhau.
**Tech Stack:** Python 3.11, FastAPI SSE, asyncio, pytest.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0 và 1; nhánh mới từ kết quả đợt 1.
- Giữ `event: response`, JSON dump mặc định, frame đầu/cuối, image status/warning/complete.
- Không dời `create_task`, `await`, `commit`, `rollback`, `CancelledError`; không sửa SQL/session/task persistence/storage.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| Resume bỏ qua saved slide hoặc sai thứ tự | `test_presentation_stream_resume.py` |
| Disconnect còn gọi LLM/tạo asset | `test_presentation_stream_disconnect.py` |
| Image status trùng hoặc frame asset chen sai | generation flow và Task 2 |
| LLM lỗi sau slide đầu mất commit | `test_stream_commits_a_finished_slide_before_the_next_llm_call` |
| Async progress đổi payload/transition | `test_presentation_generation_api.py` |

## File map

| File dưới `servers/fastapi` | Vai trò |
| --- | --- |
| `api/v1/ppt/endpoints/presentation.py` | Route stream và vị trí side effect. |
| `api/v1/ppt/endpoints/presentation_stream_frames.py` | Frame SSE thuần. |
| `api/v1/ppt/endpoints/presentation_generate.py` | Generation request và task. |
| `api/v1/ppt/endpoints/presentation_stream_slides.py` | Slide model từ generated content thuần. |
| `tests/unit/test_presentation_stream_frames.py` | Wire format. |
| `tests/unit/test_presentation_stream_resume.py` | Resume và lỗi trước khi mở stream. |

### Task 1: Validation trước stream

**Files:** Modify `presentation.py:279-337`, `tests/unit/test_presentation_stream_resume.py`.
**Interfaces:** Produce `_validated_stream_inputs(presentation: PresentationModel) -> tuple[PresentationStructureModel, PresentationLayoutModel, PresentationOutlineModel]`.

- [ ] Thêm route tests vào cuối `test_presentation_stream_resume.py`, dùng `_presentation` và `_SessionWithSlides` đã có:
  ```python
  import asyncio
  import uuid
  import pytest
  from api.v1.ppt.endpoints import presentation as endpoint
  from utils.coded_exception import CodedHTTPException

  @pytest.mark.parametrize("field,code", [
      ("structure", "error.not_prepared_for_stream"),
      ("outlines", "error.outlines_empty"),
  ])
  def test_stream_rejects_missing_prepared_input(field, code):
      value = _presentation(uuid.uuid4())
      setattr(value, field, None)
      with pytest.raises(CodedHTTPException) as error:
          asyncio.run(endpoint.stream_presentation(
              id=value.id, sql_session=_SessionWithSlides(value, [])
          ))
      assert error.value.status_code == 400
      assert error.value.error_code == code
  ```
  Chạy hai test; kỳ vọng PASS trên baseline (characterization).
- [ ] Chuyển nguyên chuỗi check từ `if not presentation.structure` tới `invalid_layout_index` vào helper ở cùng file; giữ thứ tự check và `try/except Exception`. Route gọi:
  ```python
  structure, layout, outline = _validated_stream_inputs(presentation)
  ```
  Không chuyển `ImageGenerationService` hay side effect vào helper.
- [ ] Chạy test mới, `test_presentation_stream_resume.py`, `test_presentation_stream_disconnect.py`; kỳ vọng PASS. Commit `refactor: name presentation stream validation phase`.

### Task 2: Dựng SSE frame một chỗ

**Files:** Create `presentation_stream_frames.py`, `tests/unit/test_presentation_stream_frames.py`; modify `presentation.py`.
**Interfaces:** Produce `_chunk_frame(chunk: str) -> str`, `_slide_assets_frame(index: int, slide: SlideModel, warnings: list[dict]) -> str`.

- [ ] Thêm test trước:
  ```python
  import json
  from api.v1.ppt.endpoints.presentation_stream_frames import _chunk_frame

  def test_chunk_frame_keeps_wire_bytes():
      chunk = '{ "slides": [ '
      assert _chunk_frame(chunk) == (
          "event: response\n" + "data: "
          + json.dumps({"type": "chunk", "chunk": chunk}) + "\n\n"
      )
  ```
  Chạy test mới; kỳ vọng FAIL vì module chưa có.
- [ ] Tạo hai helper; phần format phải giữ:
  ```python
  def _chunk_frame(chunk: str) -> str:
      return SSEResponse(
          event="response", data=json.dumps({"type": "chunk", "chunk": chunk})
      ).to_string()

  def _slide_assets_frame(index: int, slide: SlideModel, warnings: list[dict]) -> str:
      return SSEResponse(
          event="response",
          data=json.dumps({
              "type": "slide_assets", "slide_index": index,
              "slide": slide.model_dump(mode="json"), "warnings": warnings,
          }),
      ).to_string()
  ```
  Thay block chunk và hai block `slide_assets` trong stream, ví dụ:
  ```python
  yield _chunk_frame(slide.model_dump_json())
  yield _slide_assets_frame(
      done_idx, slides[done_idx], asset_warnings_by_slide.get(done_idx, [])
  )
  ```
  Giữ `SSEStatusResponse`, `SSEErrorResponse`, `SSECompleteResponse` và hai vòng queue tại vị trí cũ.
- [ ] Chạy test mới, stream resume/disconnect và `tests/integration/test_presentation_generation_flow.py`; kỳ vọng PASS. Commit `refactor: centralize presentation stream frames`.

### Task 3: Pha chuẩn hóa generation request

**Files:** Modify `presentation_generate.py:100-135`; create `tests/unit/test_presentation_generation_input.py`.
**Interfaces:** Produce `_generation_slide_mode(request: GeneratePresentationRequest) -> tuple[bool, str | None]`.

- [ ] Thêm test helper trước:
  ```python
  import pytest
  from constants.presentation import MAX_NUMBER_OF_SLIDES
  from models.generate_presentation_request import GeneratePresentationRequest
  from api.v1.ppt.endpoints.presentation_generate import _generation_slide_mode
  from utils.coded_exception import CodedHTTPException

  def test_markdown_mode_sets_slide_count_and_language():
      value = GeneratePresentationRequest(
          content="deck", slides_markdown=["a", "b"], language=" Vietnamese "
      )
      assert _generation_slide_mode(value) == (True, "Vietnamese")
      assert value.n_slides == 2

  def test_markdown_mode_rejects_too_many_slides():
      value = GeneratePresentationRequest(
          content="deck", slides_markdown=["x"] * (MAX_NUMBER_OF_SLIDES + 1)
      )
      with pytest.raises(CodedHTTPException) as error:
          _generation_slide_mode(value)
      assert error.value.status_code == 400
      assert error.value.error_code == "error.slides_too_many"
  ```
  Chạy test mới; kỳ vọng FAIL vì helper chưa có.
- [ ] Chuyển nguyên block tính `using_slides_markdown`, `language_to_use`, giới hạn và mutation `request.n_slides` vào helper. Handler gọi:
  ```python
  using_slides_markdown, language_to_use = _generation_slide_mode(request)
  additional_context = ""
  ```
  Không dời async status, Mem0, outline parsing hoặc commit.
- [ ] Chạy `test_generate_presentation_request.py`, `tests/test_presentation_generation_api.py` và generation flow; kỳ vọng PASS. Commit `refactor: name generation request phase`.

### Task 4: Pha parse generated outline

**Files:** Modify `presentation_generate.py:200-240`; create `tests/unit/test_presentation_outline_parse.py`.
**Interfaces:** Produce `_parse_generated_outlines(text: str, requested_count: int | None) -> PresentationOutlineModel`. Handler calls it after collecting all LLM chunks, before Mem0 write.

- [ ] Thêm test:
  ```python
  import pytest
  from api.v1.ppt.endpoints.presentation_generate import _parse_generated_outlines
  from utils.coded_exception import CodedHTTPException

  def test_invalid_outline_json_keeps_coded_400():
      with pytest.raises(CodedHTTPException) as error:
          _parse_generated_outlines("{bad", None)
      assert error.value.status_code == 400
      assert error.value.error_code == "error.outline_generation_failed"
  ```
  Chạy test mới; kỳ vọng FAIL vì helper chưa có.
- [ ] Chuyển nguyên block `dirtyjson.loads`, `normalize_outline_payload`, kiểm tra thiếu slide và truncate phần dư vào helper cùng module. Giữ `traceback.print_exc`, `describe_json_parse_failure`, detail/error_code và không đổi phần ghép text từ LLM. Handler gọi:
  ```python
  presentation_outlines = _parse_generated_outlines(
      presentation_outlines_text, n_slides_to_generate
  )
  total_outlines = len(presentation_outlines.slides)
  ```
- [ ] Chạy test mới, `test_generate_presentation_outlines.py`, `test_outline_generation_snapshots.py` và generation flow; kỳ vọng PASS. Commit `refactor: name presentation outline parse phase`.

### Task 5: Pha dựng slide content trước asset scheduling

**Files:** Create `presentation_stream_slides.py`, `tests/unit/test_presentation_stream_slides.py`; modify `presentation.py:499-526`.
**Interfaces:** Produce `_new_slide_from_generated_content(presentation_id, layout_group, layout_id, index, content, template_payload) -> SlideModel`. Caller keeps placeholder assets, UI hydrate, DB commit and `schedule_slide_assets` in original order.

- [ ] Thêm test:
  ```python
  import uuid
  from api.v1.ppt.endpoints.presentation_stream_slides import (
      _new_slide_from_generated_content,
  )

  def test_generated_slide_keeps_note_and_index():
      pid = uuid.uuid4()
      slide = _new_slide_from_generated_content(
          pid, "group", "layout-1", 1,
          {"hero": {"headline": "B"}, "__speaker_note__": "note"}, {},
      )
      assert slide.presentation == pid
      assert (slide.index, slide.layout, slide.speaker_note) == (1, "layout-1", "note")
  ```
  Chạy test mới; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên constructor `SlideModel(...)` từ stream sang helper, giữ `_template_slide_ui(template_payload, layout_id)`. Sau call site, giữ đúng thứ tự:
  ```python
  slides.append(slide)
  process_slide_add_placeholder_assets(slide)
  slide.ui = _apply_template_content_to_ui(slide.ui, slide.content)
  sql_session.add(slide)
  await sql_session.commit()
  schedule_slide_assets(i, slide)
  ```
  Không gom hai vòng asset event vì nhánh saved/new có lịch commit khác nhau.
- [ ] Chạy test mới, stream resume/disconnect và generation flow; kỳ vọng PASS. Commit `refactor: isolate generated slide construction`.

## Gate toàn đợt

Trong `servers/fastapi` chạy Ruff, Vulture, Deptry và `uv run --locked python tests/run_suite.py -q`; ở root chạy `npm test` và `node --test tests/*.test.mjs`. Test OpenAPI đợt 0 phải pass. Review từng `await`, `yield`, `commit` trong diff; số lượng/thứ tự giữ nguyên.
