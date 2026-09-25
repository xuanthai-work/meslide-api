# Backend LLM Provider Boundaries Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tách chuyển đổi message, Responses payload và schema feedback khỏi `llm_utils.py`, giữ provider dispatch/retry/timeout.
**Architecture:** Module mới chỉ chuyển đổi dữ liệu; `llm_utils.py` tiếp tục sở hữu coroutine gọi provider, stream/disconnect và retry. Re-export tên cũ trong `__all__`.
**Tech Stack:** Python 3.11, OpenAI/Google SDK wrappers, Pydantic, pytest.
**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Phụ thuộc đợt 0; phối hợp với đợt 2–4 vì cùng gọi `llm_utils`. Nhánh độc lập từ 0 nếu không cherry-pick thay đổi caller của 2–4.
- Không đổi provider selection, settings/overlay, model name, prompt text, retry count/delay, timeout, disconnect, token metrics hay error mapping.
- Không nâng dependency và không gọi provider thật trong test.

## Review Focus

| Input/rủi ro | Test |
| --- | --- |
| Custom provider dùng OpenAI message shape | Task 1 và `test_llm_dispatcher.py` |
| Google system/tool payload mất field | `test_llm_dispatcher.py`, `test_llm_messages_response_format.py` |
| Responses JSON schema bị bọc sai | Task 2 và existing dispatcher test |
| Lỗi disconnect sau chunk chưa được ném | `test_llm_utils_disconnect.py` |
| Schema feedback đổi prompt/repair vòng lặp | Task 3 và `test_llm_utils_and_schema.py` |
| OpenAI chat/custom payload khác nhau hoặc lỗi HTTP bị bọc lại | Task 4–5 |

## File map

| File dưới `servers/fastapi` | Vai trò |
| --- | --- |
| `utils/llm_utils.py` | Dispatch/stream/retry; compatibility imports. |
| `utils/llm_message_payloads.py` | Content part và provider message conversion nhận provider tường minh. |
| `utils/llm_responses_payload.py` | Responses API format/tools conversion. |
| `utils/llm_schema_feedback.py` | Validation feedback prompt thuần. |
| `utils/llm_dispatch_payloads.py` | Chat Completions kwargs dùng chung. |
| `utils/llm_client_error_handler.py` | Error mapping hiện có; chỉ thêm test. |
| `tests/unit/test_llm_payload_boundaries.py` | Regression mới. |

### Task 1: Message payload

**Files:** Create `llm_message_payloads.py`, `test_llm_payload_boundaries.py`; modify `llm_utils.py:161-190`.
**Interfaces:** Move `normalize_content_parts(content: Any) -> list[Any]` and produce `_convert_messages_for_provider(messages: Sequence[Message], provider: LLMProvider) -> list[dict[str, Any]]`. Keep `messages_to_provider_payload(messages)` in `llm_utils.py` as wrapper so patching `llm_utils.get_llm_provider` still works.

- [ ] Thêm test:
  ```python
  from utils.llm_message_payloads import normalize_content_parts
  from utils.llm_messages import TextContentPart

  def test_string_content_becomes_one_text_part():
      assert normalize_content_parts("hello") == [TextContentPart(text="hello")]
  ```
  Chạy `tests/unit/test_llm_payload_boundaries.py`; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển `normalize_content_parts` và thân chuyển provider của `messages_to_provider_payload` sang module mới. Wrapper ở `llm_utils.py` giữ signature:
  ```python
  def messages_to_provider_payload(messages: Sequence[Message]) -> list[dict[str, Any]]:
      return _convert_messages_for_provider(messages, get_llm_provider())
  ```
  Giữ nhánh `OPENAI/CUSTOM/GOOGLE` và lỗi 500 byte-for-byte; giữ `__all__`. Không thay `get_generate_kwargs`.
- [ ] Chạy test mới, `test_llm_messages_response_format.py`, `test_llm_dispatcher.py`, `test_llm_utils_and_schema.py`; kỳ vọng PASS. Commit `refactor: isolate LLM message payload conversion`.

### Task 2: Responses payload

**Files:** Create `llm_responses_payload.py`; extend `test_llm_payload_boundaries.py`; modify `llm_utils.py:304-340`.
**Interfaces:** Move `_to_responses_text_format(response_format: Any) -> Any` and `_tools_for_responses_api(tools: list[dict[str, Any]]) -> list[dict[str, Any]]`; import same names in `llm_utils.py`.

- [ ] Thêm test:
  ```python
  from utils.llm_responses_payload import _to_responses_text_format

  def test_responses_schema_unwrap_preserves_name_strict_and_schema():
      source = {"type": "json_schema", "json_schema": {
          "name": "deck", "schema": {"type": "object"}, "strict": True
      }}
      assert _to_responses_text_format(source) == {
          "type": "json_schema", "name": "deck",
          "schema": {"type": "object"}, "strict": True,
      }
  ```
  Chạy test; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên hai function; trong `_dispatch_chat_completion` giữ cùng lệnh gọi tại vị trí cũ:
  ```python
  if tools:
      responses_kwargs["tools"] = _tools_for_responses_api(tools)
  if response_format is not None:
      responses_kwargs["text"] = {
          "format": _to_responses_text_format(response_format),
      }
  ```
  Chỉ thay import; giữ nhánh điều kiện hiện tại và mọi kwargs khác.
- [ ] Chạy test mới, `test_llm_dispatcher.py`, `test_llm_utils_sync_stream.py`; kỳ vọng PASS. Commit `refactor: isolate Responses API payload shaping`.

### Task 3: Schema repair feedback

**Files:** Create `llm_schema_feedback.py`; extend `test_llm_payload_boundaries.py`; modify `llm_utils.py:972-1009`.
**Interfaces:** Move `structured_validation_feedback_user_message(content: dict, validation_errors: list[str]) -> UserMessage`; re-export from `llm_utils.py`.

- [ ] Thêm test:
  ```python
  from utils.llm_schema_feedback import structured_validation_feedback_user_message

  def test_feedback_limits_errors_and_previous_json():
      feedback = structured_validation_feedback_user_message(
          {"body": "x" * 7000}, [f"e{i}" for i in range(11)]
      )
      assert "- e0" in feedback.content
      assert "- e10" not in feedback.content
      assert "...and 1 more validation errors." in feedback.content
      assert "... (truncated)" in feedback.content
  ```
  Chạy test; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên function, giữ `ensure_ascii=False`, limit 10/6000 và prompt byte-for-byte. `generate_structured_with_schema_retries` tiếp tục gọi tên cũ qua import; không chuyển retry loop.
  ```python
  from utils.llm_schema_feedback import structured_validation_feedback_user_message
  ```
- [ ] Chạy test mới, `test_llm_utils_and_schema.py`, `test_llm_client_timeout.py`, `test_llm_utils_disconnect.py`; kỳ vọng PASS. Commit `refactor: isolate LLM schema feedback`.

### Task 4: Chat Completions dispatch payload

**Files:** Create `utils/llm_dispatch_payloads.py`; extend `tests/unit/test_llm_payload_boundaries.py`; modify `llm_utils.py:385-456`.
**Interfaces:** Produce `chat_completion_kwargs(model, messages, stream, tools, response_format, max_tokens, extra_body) -> dict[str, Any]`. OpenAI Chat Completions và CUSTOM branches consume the same dict.

- [ ] Thêm test:
  ```python
  from utils.llm_dispatch_payloads import chat_completion_kwargs

  def test_chat_kwargs_omit_unset_optional_fields():
      assert chat_completion_kwargs("m", [{"role": "user", "content": "a"}],
                                    False, None, None, None, None) == {
          "model": "m", "messages": [{"role": "user", "content": "a"}],
          "stream": False,
      }
  ```
  Chạy test; kỳ vọng FAIL vì module chưa có.
- [ ] Chuyển nguyên block dựng `chat_kwargs` ở hai nhánh OPENAI non-Responses và CUSTOM vào helper, giữ điều kiện `if tools`, `if response_format is not None`, `if max_tokens is not None`, `if extra_body`. Hai nhánh vẫn gọi `client.chat.completions.create(**chat_kwargs)` tại vị trí cũ. Không đổi nhánh Responses hoặc Google, không thêm `await`.
- [ ] Chạy test mới, `test_llm_dispatcher.py`, `test_llm_utils_sync_stream.py` và `test_llm_utils_disconnect.py`; kỳ vọng PASS. Commit `refactor: share OpenAI compatible dispatch payload`.

### Task 5: Error mapping contract

**Files:** Create `tests/unit/test_llm_client_error_mapping.py`; read `utils/llm_client_error_handler.py`.
**Interfaces:** Keep `handle_llm_client_exceptions(e: Exception) -> HTTPException` unchanged.

- [ ] Thêm characterization test:
  ```python
  from fastapi import HTTPException
  from utils.llm_client_error_handler import handle_llm_client_exceptions

  def test_http_exception_keeps_identity_and_status():
      original = HTTPException(status_code=429, detail="slow down")
      assert handle_llm_client_exceptions(original) is original
  ```
  Chạy test; kỳ vọng PASS trên baseline.
- [ ] Review `OpenAIAPIError`, `GoogleAPIError` và generic branches trong file; giữ nguyên `status_code`, `detail`, `error_code`, `error_params` và safe message classification. Chạy `test_llm_client_error_mapping.py` và toàn `test_llm_utils_and_schema.py`; kỳ vọng PASS. Không tạo commit rỗng nếu mapping không đổi.

## Gate toàn đợt

Ruff, Vulture, Deptry, toàn `uv run --locked python tests/run_suite.py -q` trong `servers/fastapi`; `npm test` và `node --test tests/*.test.mjs` ở root. Review diff của `utils/llm_provider.py` và `utils/llm_client_error_handler.py` phải rỗng; prompt, retries và stream event giữ nguyên.
