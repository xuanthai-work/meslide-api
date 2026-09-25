# Backend Chat Tools and Slide UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tách tool definitions, argument normalization và UI patch thuần khỏi `ChatTools` mà giữ tool names/schema/result.
**Architecture:** `ChatTools` vẫn giữ handler map, SQL/Mem0 calls, mutation order và trace/SSE. Các module mới chỉ xây danh sách Tool hoặc biến đổi JSON/Pydantic payload.
**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0, 1, 3; nhánh mới từ kết quả đợt 3.
- Không đổi SQL history, Mem0, tool name/description/schema/strict, retry, trace, stream event.
- Giữ `ChatTools.get_tool_definitions`, `ChatTools._parse_args` và tên helper test/caller đang dùng.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| Tool schema strict/OpenAI đổi hoặc mất alias | `test_chat_llm_tools.py` |
| JSONish, fenced payload và chuỗi "null" nested | Task 2 và existing parse args test |
| Tool lỗi/repair đổi result và trace | `test_chat_trace_codes.py` và `test_chat_endpoint.py` |
| UI patch thay vì merge làm mất font/table/chart | Task 3 và `test_slide_ui_chat_tools.py` |
| SQL/Mem0 hoặc thứ tự mutation thay đổi | `test_chat_conversation_store.py` và `test_chat_service_template_v2.py` |

## File map

| File dưới `servers/fastapi` | Vai trò |
| --- | --- |
| `services/chat/tools.py` | Tool handlers, state, retry, re-export. |
| `services/chat/tool_definitions.py` | Danh sách Tool tĩnh. |
| `services/chat/tool_arguments.py` | Chuẩn hóa argument thuần. |
| `services/chat/slide_ui_helpers.py` | UI transformation hiện có, nhận style merge thuần. |
| `tests/unit/test_chat_tool_arguments.py` | Argument edge cases. |

### Task 1: Tool definitions tĩnh

**Files:** Create `services/chat/tool_definitions.py`; modify `services/chat/tools.py:105-350`; test `tests/unit/test_chat_llm_tools.py`.
**Interfaces:** Produce `build_chat_tool_definitions() -> list[Tool]`; `ChatTools.get_tool_definitions()` delegates to it.

- [ ] Chạy `tests/unit/test_chat_llm_tools.py`; lưu danh sách names/schema của `test_chat_tools_expose_only_v2_tool_names` và `test_chat_tools_emit_openai_strict_compatible_schemas`, kỳ vọng PASS.
- [ ] Chuyển nguyên list body của `get_tool_definitions` sang `build_chat_tool_definitions`; không rút gọn mô tả/prompt. Method cũ chỉ còn:
  ```python
  def get_tool_definitions(self) -> list[Tool]:
      return build_chat_tool_definitions()
  ```
  Chuyển import schema/constant chỉ khi không còn caller khác trong `tools.py`.
- [ ] Chạy lại `test_chat_llm_tools.py` và `test_chat_endpoint.py`; kỳ vọng PASS. Commit `refactor: isolate chat tool definitions`.

### Task 2: Argument normalization

**Files:** Create `services/chat/tool_arguments.py`, `tests/unit/test_chat_tool_arguments.py`; modify `services/chat/tools.py`.
**Interfaces:** Move `_normalize_tool_argument_value(value: Any) -> Any` unchanged; import/re-export name from `tools.py`. `ChatTools._parse_args` stays in class because it calls `ChatTools._loads_jsonish`.

- [ ] Thêm test:
  ```python
  from services.chat.tool_arguments import _normalize_tool_argument_value

  def test_nested_string_null_becomes_none_without_touching_other_values():
      assert _normalize_tool_argument_value(
          {"items": ["null", " NULL ", 0, {"x": "none"}]}
      ) == {"items": [None, None, 0, {"x": "none"}]}
  ```
  Chạy test mới; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên function 50-60 sang module mới; trong `tools.py` import tên cũ. Giữ round-trip `json.loads(json.dumps(parsed, ensure_ascii=False))` trong `_parse_args`.
- [ ] Chạy test mới, `test_chat_llm_tools.py`, `test_slide_ui_chat_tools.py`; kỳ vọng PASS. Commit `refactor: isolate chat argument normalization`.

### Task 3: UI style patch thuần

**Files:** Modify `services/chat/tools.py:970-1010`, `services/chat/slide_ui_helpers.py`; create `tests/unit/test_chat_style_patch.py`.
**Interfaces:** Produce `_merge_dict_patch(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]` in `slide_ui_helpers.py`; keep `ChatTools._merge_dict_patch` as delegating wrapper.

- [ ] Thêm test:
  ```python
  from services.chat.slide_ui_helpers import _merge_dict_patch

  def test_style_patch_deep_merges_without_mutating_input():
      source = {"font": {"size": 12, "family": "A"}}
      assert _merge_dict_patch(source, {"font": {"size": 18}}) == {
          "font": {"size": 18, "family": "A"}
      }
      assert source["font"]["size"] == 12
  ```
  Chạy test mới; kỳ vọng FAIL vì function chưa có.
- [ ] Chuyển nguyên body `ChatTools._merge_dict_patch` vào module helper, thay recursive call bằng `_merge_dict_patch`. Trong class giữ:
  ```python
  @staticmethod
  def _merge_dict_patch(target, patch):
      return _merge_dict_patch(target, patch)
  ```
  Không dời `_update_slide_element`, `_save_slide` hoặc SQL/Mem0 calls.
- [ ] Chạy test mới, `test_slide_ui_chat_tools.py`, `test_chat_trace_codes.py`, `test_chat_endpoint.py`; kỳ vọng PASS. Commit `refactor: isolate chat UI style merge`.

## Gate toàn đợt

Trong `servers/fastapi` chạy Ruff, Vulture, Deptry và toàn `uv run --locked python tests/run_suite.py -q`; ở root chạy `npm test` và `node --test tests/*.test.mjs`. OpenAPI test đợt 0 phải pass. Review diff: tool list JSON và mutation/trace order giữ nguyên; không SQL/Mem0 persistence change.
