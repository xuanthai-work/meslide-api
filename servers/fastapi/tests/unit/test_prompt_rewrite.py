import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from openai import APIError as OpenAIAPIError
from pydantic import ValidationError

from api.v1.auth.context import reset_current_owner_id, set_current_owner_id
from api.v1.ppt.router import API_V1_PPT_ROUTER
from constants.documents import MAX_PROMPT_REWRITE_FILE_PATHS
from constants.presentation import MAX_NUMBER_OF_SLIDES
from models.document_profile import DocumentProfile
from models.prompt_rewrite import PromptRewriteRequest, PromptRewriteResponse
from services.temp_file_service import get_temp_file_service
from utils.coded_exception import (
    CodedHTTPException,
    register_coded_exception_handler,
)
from utils.llm_calls.rewrite_presentation_prompt import (
    build_repair_messages,
    build_rewrite_messages,
    clean_rewritten_prompt,
    format_document_profile,
    get_rewrite_quality_issues,
    rewrite_presentation_prompt,
)


def test_request_accepts_prompt_only():
    request = PromptRewriteRequest(prompt="Design a deck about quarterly results")

    assert request.language is None
    assert request.n_slides is None


def test_request_accepts_optional_language_and_slide_count():
    request = PromptRewriteRequest(
        prompt="Design a deck about quarterly results",
        language="vi",
        n_slides=MAX_NUMBER_OF_SLIDES,
    )

    assert request.language == "vi"
    assert request.n_slides == MAX_NUMBER_OF_SLIDES


def test_request_accepts_minimum_slide_count():
    request = PromptRewriteRequest(
        prompt="Design a deck about quarterly results",
        n_slides=1,
    )

    assert request.n_slides == 1


def test_request_strips_outer_whitespace_without_touching_line_breaks():
    request = PromptRewriteRequest(prompt="  \nDesign a deck about results.\n\n")

    assert request.prompt == "Design a deck about results."


def test_request_preserves_intentional_internal_line_breaks():
    request = PromptRewriteRequest(
        prompt="  Design a deck about results.\nKeep this line.\n"
    )

    assert request.prompt == "Design a deck about results.\nKeep this line."


@pytest.mark.parametrize("prompt", ("", "   ", "\n\t \n"))
def test_request_rejects_empty_prompt(prompt):
    with pytest.raises(ValidationError):
        PromptRewriteRequest(prompt=prompt)


@pytest.mark.parametrize("prompt", ("ab", " a ", "a\n"))
def test_request_rejects_prompt_shorter_than_three_characters(prompt):
    with pytest.raises(ValidationError):
        PromptRewriteRequest(prompt=prompt)


def test_request_rejects_oversized_prompt():
    with pytest.raises(ValidationError):
        PromptRewriteRequest(prompt="a" * 8001)


def test_request_accepts_prompt_at_maximum_length():
    request = PromptRewriteRequest(prompt="a" * 8000)

    assert request.prompt == "a" * 8000


@pytest.mark.parametrize(
    "n_slides",
    (0, -1, MAX_NUMBER_OF_SLIDES + 1),
)
def test_request_rejects_out_of_range_slide_count(n_slides):
    with pytest.raises(ValidationError):
        PromptRewriteRequest(
            prompt="Design a deck about quarterly results", n_slides=n_slides
        )


@pytest.mark.parametrize("n_slides", ("five", 2.5, [10]))
def test_request_rejects_non_integer_slide_count(n_slides):
    with pytest.raises(ValidationError):
        PromptRewriteRequest(
            prompt="Design a deck about quarterly results", n_slides=n_slides
        )


def test_request_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        PromptRewriteRequest(
            prompt="Design a deck about quarterly results",
            unexpected_field="value",
        )


def test_request_file_paths_default_to_none():
    request = PromptRewriteRequest(prompt="Design a deck about quarterly results")

    assert request.file_paths is None


def test_request_empty_file_paths_normalize_to_none():
    request = PromptRewriteRequest(
        prompt="Design a deck about quarterly results",
        file_paths=[],
    )

    assert request.file_paths is None


def test_request_accepts_file_paths_up_to_the_upload_ui_limit():
    paths = [f"/tmp/owner/report-{index}.txt" for index in range(MAX_PROMPT_REWRITE_FILE_PATHS)]

    request = PromptRewriteRequest(
        prompt="Design a deck about quarterly results",
        file_paths=paths,
    )

    assert request.file_paths == paths


def test_request_rejects_more_file_paths_than_the_upload_ui_allows():
    paths = [
        f"/tmp/owner/report-{index}.txt"
        for index in range(MAX_PROMPT_REWRITE_FILE_PATHS + 1)
    ]

    with pytest.raises(ValidationError):
        PromptRewriteRequest(
            prompt="Design a deck about quarterly results",
            file_paths=paths,
        )


@pytest.mark.parametrize("file_paths", ("report.txt", 42, {"a": "b"}, ["ok.txt", 7]))
def test_request_rejects_non_list_or_non_string_file_paths(file_paths):
    with pytest.raises(ValidationError):
        PromptRewriteRequest(
            prompt="Design a deck about quarterly results",
            file_paths=file_paths,
        )


def test_response_holds_rewritten_prompt():
    response = PromptRewriteResponse(rewritten_prompt="A sharper, structured prompt.")

    assert response.rewritten_prompt == "A sharper, structured prompt."


def test_response_requires_rewritten_prompt():
    with pytest.raises(ValidationError):
        PromptRewriteResponse()


def test_response_rejects_empty_rewritten_prompt():
    with pytest.raises(ValidationError):
        PromptRewriteResponse(rewritten_prompt="")


def test_response_accepts_rewritten_prompt_at_maximum_length():
    response = PromptRewriteResponse(rewritten_prompt="a" * 3000)

    assert response.rewritten_prompt == "a" * 3000


def test_response_rejects_oversized_rewritten_prompt():
    with pytest.raises(ValidationError):
        PromptRewriteResponse(rewritten_prompt="a" * 3001)


# ---------------------------------------------------------------------------
# Prompt builder (pure helper)
# ---------------------------------------------------------------------------

SOURCE_PROMPT = "AI trong giáo dục, covering 2024 enrollment of 15,000 students"
SPARSE_SOURCE_PROMPT = "Tạo giúp tôi 1 bài thuyết trình về 9router"
RICH_BRIEF = (
    "Create a Vietnamese presentation brief that explains AI in education to "
    "decision-makers who need a practical overview. Cover the 2024 enrollment "
    "context of 15,000 students, the problem being addressed, relevant use "
    "cases, implementation considerations, measurable outcomes, limitations, "
    "and a clear conclusion. Build a coherent narrative and require every "
    "external claim to be verified rather than invented."
)


@pytest.fixture(autouse=True)
def _silence_normalized_error_tracebacks(monkeypatch):
    """Keep test output pristine: handle_llm_client_exceptions prints tracebacks."""
    monkeypatch.setattr("traceback.print_exc", lambda: None)


def test_prompt_builder_returns_system_then_user_message():
    messages = build_rewrite_messages(SOURCE_PROMPT)

    assert [message.role for message in messages] == ["system", "user"]


def test_prompt_builder_quotes_exact_source_content():
    content = build_rewrite_messages(SOURCE_PROMPT)[1].content

    assert SOURCE_PROMPT in content
    assert "quoted user data" in content.lower()


def test_prompt_builder_system_prompt_states_rewrite_rules():
    system_content = build_rewrite_messages(SOURCE_PROMPT)[0].content

    assert "presentation-generation brief" in system_content
    assert "do not answer the topic" in system_content.lower()
    assert "do not invent facts" in system_content.lower()
    assert "plain text" in system_content.lower()
    assert "markdown fence" in system_content.lower()


def test_prompt_builder_compiles_sparse_input_into_a_high_signal_brief():
    system_content = build_rewrite_messages(SPARSE_SOURCE_PROMPT)[0].content.lower()

    assert "90-180 words" in system_content
    assert "objective" in system_content
    assert "audience" in system_content
    assert "coverage" in system_content
    assert "narrative" in system_content
    assert "evidence" in system_content
    assert "research questions" in system_content
    assert "slide-by-slide outline" in system_content


def test_prompt_builder_contains_sparse_and_detailed_few_shot_examples():
    system_content = build_rewrite_messages(SPARSE_SOURCE_PROMPT)[0].content

    assert "<examples>" in system_content
    assert "OrbitDB" in system_content
    assert "Detailed source" in system_content


def test_repair_prompt_contains_source_previous_output_and_failed_quality_reasons():
    messages = build_repair_messages(
        SPARSE_SOURCE_PROMPT,
        "Hãy tạo một bài thuyết trình chi tiết về 9router.",
        ["sparse_output_too_short", "missing_protected_identifiers"],
        language="Vietnamese (Tiếng Việt)",
        n_slides=10,
    )
    content = messages[1].content

    assert SPARSE_SOURCE_PROMPT in content
    assert "Hãy tạo một bài thuyết trình chi tiết về 9router." in content
    assert "sparse_output_too_short" in content
    assert "missing_protected_identifiers" in content
    assert "Vietnamese (Tiếng Việt)" in content
    assert "10" in content


def test_prompt_builder_includes_optional_slide_count_only_when_given():
    with_count = build_rewrite_messages(SOURCE_PROMPT, n_slides=10)[1].content
    without_count = build_rewrite_messages(SOURCE_PROMPT)[1].content

    assert "10" in with_count
    assert "slide" not in without_count.lower()


@pytest.mark.parametrize("language", (None, "", "Auto (English)", "auto", "Auto"))
def test_prompt_builder_auto_language_preserves_source_language(language):
    content = build_rewrite_messages(SOURCE_PROMPT, language=language)[1].content

    assert "preserve the source prompt's language" in content.lower()


@pytest.mark.parametrize(
    "language", ("Vietnamese (Tiếng Việt)", "English")
)
def test_prompt_builder_explicit_language_requests_that_language(language):
    content = build_rewrite_messages(SOURCE_PROMPT, language=language)[1].content

    assert language in content
    assert "preserve the source prompt's language" not in content.lower()


# ---------------------------------------------------------------------------
# Completion cleanup (pure helper)
# ---------------------------------------------------------------------------


def test_clean_rewritten_prompt_trims_outer_whitespace():
    assert clean_rewritten_prompt("  \n  A clearer brief.\n\t ") == "A clearer brief."


def test_clean_rewritten_prompt_returns_empty_for_missing_content():
    assert clean_rewritten_prompt(None) == ""
    assert clean_rewritten_prompt("") == ""


def test_clean_rewritten_prompt_removes_outer_markdown_fence():
    assert (
        clean_rewritten_prompt("```\nA clearer brief.\n```") == "A clearer brief."
    )
    assert (
        clean_rewritten_prompt("```text\nA clearer brief.\n```")
        == "A clearer brief."
    )


def test_clean_rewritten_prompt_removes_single_generic_prefix():
    assert (
        clean_rewritten_prompt("Rewritten prompt: A clearer brief.")
        == "A clearer brief."
    )
    assert (
        clean_rewritten_prompt("here's the rewritten prompt: A clearer brief.")
        == "A clearer brief."
    )


def test_clean_rewritten_prompt_removes_prefix_then_fence():
    assert (
        clean_rewritten_prompt("Rewritten prompt:\n```\nA clearer brief.\n```")
        == "A clearer brief."
    )


def test_clean_rewritten_prompt_keeps_prompt_body_untouched():
    body = "A brief: cover Q3 2026 revenue of $4.2M and the 12% growth claim."

    assert clean_rewritten_prompt(f"```\n{body}\n```") == body
    assert clean_rewritten_prompt(body) == body


# ---------------------------------------------------------------------------
# Deterministic quality gate
# ---------------------------------------------------------------------------


def test_quality_gate_rejects_the_reported_shallow_paraphrase():
    issues = get_rewrite_quality_issues(
        SPARSE_SOURCE_PROMPT,
        "Hãy tạo một bài thuyết trình chi tiết về 9router.",
    )

    assert "sparse_output_too_short" in issues


def test_quality_gate_accepts_a_high_signal_sparse_brief():
    assert get_rewrite_quality_issues(SOURCE_PROMPT, RICH_BRIEF) == []


def test_quality_gate_requires_important_numeric_and_alphanumeric_identifiers():
    issues = get_rewrite_quality_issues(
        "Giới thiệu 9router với số liệu năm 2024",
        (
            "Tạo một bài thuyết trình chuyên sâu cho người mới, giải thích chủ đề, "
            "bối cảnh, vấn đề cần giải quyết, nhóm người dùng, các năng lực chính, "
            "quy trình sử dụng, ví dụ ứng dụng, lợi ích, hạn chế, khác biệt và kết "
            "luận. Xác minh mọi dữ kiện bên ngoài và không tự tạo số liệu."
        ),
    )

    assert "missing_protected_identifiers" in issues


def test_quality_gate_does_not_mechanically_expand_an_already_detailed_source():
    detailed_source = (
        "Create an executive presentation for regional sales leaders explaining "
        "the 2025 retention decline, its three validated causes, the approved "
        "recovery plan, owners, milestones, risks, and the decision required at "
        "the end of the meeting."
    )
    concise_rewrite = (
        "Create an executive presentation for regional sales leaders about the "
        "2025 retention decline. Preserve the three validated causes and explain "
        "the approved recovery plan, accountable owners, milestones, risks, and "
        "the final decision required from the meeting."
    )

    assert get_rewrite_quality_issues(detailed_source, concise_rewrite) == []


# ---------------------------------------------------------------------------
# Provider-independent rewrite call
# ---------------------------------------------------------------------------


def _openai_chat_client(content):
    """Fake OpenAI / custom OpenAI-compatible client (Chat Completions shape)."""
    captured = []

    def create(**kwargs):
        captured.append(kwargs)
        if isinstance(content, Exception):
            raise content
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=None,
        )

    completions = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, captured


def _google_client(text):
    """Fake google-genai client (candidates/parts response shape)."""
    captured = []

    def generate_content(**kwargs):
        captured.append(kwargs)
        if isinstance(text, Exception):
            raise text
        return SimpleNamespace(
            candidates=[
                SimpleNamespace(
                    content=SimpleNamespace(parts=[SimpleNamespace(text=text)])
                )
            ]
        )

    return SimpleNamespace(models=SimpleNamespace(generate_content=generate_content)), captured


def _sequenced_openai_client(contents):
    """Fake client returning one response per completion request."""
    captured = []
    remaining = list(contents)

    def create(**kwargs):
        captured.append(kwargs)
        content = remaining.pop(0) if remaining else contents[-1]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=None,
        )

    completions = SimpleNamespace(create=create)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return client, captured


def _patch_llm(monkeypatch, client, provider, model="test-model"):
    monkeypatch.setenv("LLM", provider)
    monkeypatch.setattr(
        "utils.llm_calls.rewrite_presentation_prompt.get_llm_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "utils.llm_calls.rewrite_presentation_prompt.get_model",
        lambda: model,
    )


def test_rewrite_uses_openai_chat_completion_shape(monkeypatch):
    client, captured = _openai_chat_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "openai")

    result = asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert result == RICH_BRIEF
    assert len(captured) == 1
    kwargs = captured[0]
    assert kwargs["model"] == "test-model"
    assert kwargs["stream"] is False
    assert kwargs["max_tokens"] == 512
    assert "tools" not in kwargs
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["messages"][1]["role"] == "user"
    assert SOURCE_PROMPT in kwargs["messages"][1]["content"]


def test_rewrite_uses_google_response_shape(monkeypatch):
    client, captured = _google_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "google")

    result = asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert result == RICH_BRIEF
    kwargs = captured[0]
    assert kwargs["config"]["max_output_tokens"] == 512
    assert "tools" not in kwargs["config"]
    assert kwargs["config"]["system_instruction"]


def test_rewrite_uses_custom_openai_compatible_shape(monkeypatch):
    client, captured = _openai_chat_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "custom")

    result = asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert result == RICH_BRIEF
    kwargs = captured[0]
    assert kwargs["stream"] is False
    assert kwargs["max_tokens"] == 512
    assert kwargs["model"] == "test-model"


def test_rewrite_sends_requested_language_and_slide_count(monkeypatch):
    client, captured = _openai_chat_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "openai")

    asyncio.run(
        rewrite_presentation_prompt(
            SOURCE_PROMPT, language="Vietnamese (Tiếng Việt)", n_slides=10
        )
    )

    user_content = captured[0]["messages"][1]["content"]
    assert "Vietnamese (Tiếng Việt)" in user_content
    assert "10" in user_content


@pytest.mark.parametrize("content", (None, "", "   ", "\n\t \n"))
def test_rewrite_empty_or_whitespace_completion_raises_coded_error(
    monkeypatch, content
):
    client, _ = _openai_chat_client(content)
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert excinfo.value.status_code == 502
    assert excinfo.value.error_code == "error.prompt_rewrite.empty_completion"


def test_rewrite_oversized_completion_is_rejected(monkeypatch):
    client, _ = _openai_chat_client("a" * 3001)
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert excinfo.value.status_code == 502
    assert excinfo.value.error_code == "error.prompt_rewrite.completion_too_long"


def test_rewrite_accepts_completion_at_maximum_length(monkeypatch):
    client, _ = _openai_chat_client("a" * 3000)
    _patch_llm(monkeypatch, client, "openai")
    detailed_source = " ".join("requirement" for _ in range(25))

    result = asyncio.run(rewrite_presentation_prompt(detailed_source))

    assert result == "a" * 3000


def test_rewrite_strips_fence_from_real_completion(monkeypatch):
    client, _ = _openai_chat_client(f"```\n{RICH_BRIEF}\n```")
    _patch_llm(monkeypatch, client, "openai")

    result = asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert result == RICH_BRIEF


def test_rewrite_repairs_a_shallow_first_completion_once(monkeypatch):
    shallow = "Hãy tạo một bài thuyết trình chi tiết về 9router."
    repaired = (
        "Tạo một bài thuyết trình bằng tiếng Việt giúp người mới hiểu rõ 9router. "
        "Nội dung cần xác định 9router là gì từ ngữ cảnh hoặc nguồn đáng tin cậy, "
        "sau đó trình bày vấn đề liên quan, đối tượng sử dụng, các khả năng chính, "
        "quy trình hoạt động, trường hợp ứng dụng, lợi ích, hạn chế và điểm khác "
        "biệt. Xây dựng mạch nội dung từ bối cảnh đến đánh giá và kết luận thực "
        "tiễn. Chỉ sử dụng dữ kiện có thể kiểm chứng và không tự suy đoán."
    )
    client, captured = _sequenced_openai_client([shallow, repaired])
    _patch_llm(monkeypatch, client, "openai")

    result = asyncio.run(rewrite_presentation_prompt(SPARSE_SOURCE_PROMPT))

    assert result == repaired
    assert len(captured) == 2
    assert shallow in captured[1]["messages"][1]["content"]


def test_rewrite_rejects_a_second_low_quality_completion(monkeypatch):
    shallow = "Hãy tạo một bài thuyết trình chi tiết về 9router."
    client, captured = _sequenced_openai_client([shallow, shallow])
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(rewrite_presentation_prompt(SPARSE_SOURCE_PROMPT))

    assert len(captured) == 2
    assert excinfo.value.status_code == 502
    assert excinfo.value.error_code == "error.prompt_rewrite.quality_too_low"


def test_rewrite_does_not_repair_an_acceptable_completion(monkeypatch):
    client, captured = _sequenced_openai_client([RICH_BRIEF])
    _patch_llm(monkeypatch, client, "openai")

    result = asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert result == RICH_BRIEF
    assert len(captured) == 1


def test_rewrite_normalizes_generic_provider_exception(monkeypatch):
    client, _ = _openai_chat_client(ValueError("boom"))
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert excinfo.value.status_code == 500
    assert "LLM API error" in excinfo.value.detail


def test_rewrite_normalizes_openai_rate_limit(monkeypatch):
    client, _ = _openai_chat_client(
        OpenAIAPIError(
            message="Error code: 429 - Rate limit reached",
            request=httpx.Request("POST", "https://x"),
            body={"error": {"code": "rate_limit_exceeded"}},
        )
    )
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert excinfo.value.error_code == "error.provider.rate_limited"
    assert "rate limited" in excinfo.value.detail


def test_rewrite_logs_metadata_but_never_raw_prompt_text(monkeypatch, caplog):
    client, _ = _openai_chat_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "openai")
    secret_source = "SECRET-MARKER-ZETA about AI in education"

    with caplog.at_level(
        logging.INFO, logger="utils.llm_calls.rewrite_presentation_prompt"
    ):
        result = asyncio.run(rewrite_presentation_prompt(secret_source))

    assert result == RICH_BRIEF
    assert "SECRET-MARKER-ZETA" not in caplog.text
    assert "input_length=" in caplog.text
    assert "output_length=" in caplog.text


# ---------------------------------------------------------------------------
# HTTP endpoint: POST /api/v1/ppt/prompt/rewrite
# ---------------------------------------------------------------------------


def _build_endpoint_client(owner_id=None) -> TestClient:
    """Test app exposing the real PPT router tree, coded-error handler included.

    When ``owner_id`` is given, the request runs with that owner pinned (the
    same ContextVar ``SessionAuthMiddleware`` sets in production).
    """
    app = FastAPI()
    if owner_id is not None:
        app.add_middleware(_OwnerContextMiddleware, owner_id=owner_id)
    app.include_router(API_V1_PPT_ROUTER)
    register_coded_exception_handler(app)
    return TestClient(app)


def _patch_endpoint_rewrite(monkeypatch, result=None, exc=None):
    """Replace the helper used by the endpoint, recording every call."""
    calls = []

    async def fake_rewrite(
        prompt,
        language=None,
        n_slides=None,
        file_paths=None,
        disconnect_checker=None,
    ):
        calls.append(
            {
                "prompt": prompt,
                "language": language,
                "n_slides": n_slides,
                "file_paths": file_paths,
                "disconnect_checker": disconnect_checker,
            }
        )
        if exc is not None:
            raise exc
        return result

    monkeypatch.setattr(
        "api.v1.ppt.endpoints.prompt.rewrite_presentation_prompt", fake_rewrite
    )
    return calls


def test_prompt_rewrite_route_is_registered_in_ppt_router():
    client = _build_endpoint_client()

    matching = [
        route
        for route in client.app.routes
        if getattr(route, "path", None) == "/api/v1/ppt/prompt/rewrite"
    ]

    assert len(matching) == 1
    assert "POST" in matching[0].methods


def test_endpoint_valid_request_returns_rewritten_prompt(monkeypatch):
    calls = _patch_endpoint_rewrite(
        monkeypatch, result="A clearer brief about AI in education."
    )
    client = _build_endpoint_client()

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={
            "prompt": SOURCE_PROMPT,
            "language": "Vietnamese (Tiếng Việt)",
            "n_slides": 10,
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "rewritten_prompt": "A clearer brief about AI in education."
    }
    assert len(calls) == 1
    assert calls[0]["prompt"] == SOURCE_PROMPT
    assert calls[0]["language"] == "Vietnamese (Tiếng Việt)"
    assert calls[0]["n_slides"] == 10
    # Request.is_disconnected must be wired into the LLM layer. It is the
    # request's own coroutine factory: calling it yields an awaitable.
    disconnect_checker = calls[0]["disconnect_checker"]
    assert callable(disconnect_checker)
    assert disconnect_checker.__name__ == "is_disconnected"
    pending_disconnect = disconnect_checker()
    assert asyncio.iscoroutine(pending_disconnect)
    pending_disconnect.close()


def test_endpoint_forwards_minimal_request(monkeypatch):
    calls = _patch_endpoint_rewrite(monkeypatch, result="A clearer brief.")
    client = _build_endpoint_client()

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT},
    )

    assert response.status_code == 200
    assert response.json() == {"rewritten_prompt": "A clearer brief."}
    assert calls[0]["language"] is None
    assert calls[0]["n_slides"] is None
    assert calls[0]["file_paths"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"prompt": "ab"},
        {"prompt": "x" * 8001},
        {"prompt": SOURCE_PROMPT, "n_slides": 0},
        {"prompt": SOURCE_PROMPT, "n_slides": MAX_NUMBER_OF_SLIDES + 1},
        {"prompt": SOURCE_PROMPT, "n_slides": "ten"},
        {"prompt": SOURCE_PROMPT, "unexpected_field": True},
    ],
)
def test_endpoint_invalid_request_returns_422_and_never_invokes_helper(
    monkeypatch, payload
):
    calls = _patch_endpoint_rewrite(monkeypatch, result="never returned")
    client = _build_endpoint_client()

    response = client.post("/api/v1/ppt/prompt/rewrite", json=payload)

    assert response.status_code == 422
    assert calls == []


def test_endpoint_preserves_coded_provider_error_status_and_detail(monkeypatch):
    _patch_endpoint_rewrite(
        monkeypatch,
        exc=CodedHTTPException(
            status_code=502,
            detail="The AI model did not return a rewritten prompt. Please try again.",
            error_code="error.prompt_rewrite.empty_completion",
        ),
    )
    client = _build_endpoint_client()

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT},
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "The AI model did not return a rewritten prompt. Please try again.",
        "error_code": "error.prompt_rewrite.empty_completion",
    }


def test_endpoint_preserves_normalized_provider_error_status_and_detail(monkeypatch):
    _patch_endpoint_rewrite(
        monkeypatch,
        exc=HTTPException(
            status_code=500,
            detail="LLM API error. Please try again later.",
        ),
    )
    client = _build_endpoint_client()

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "LLM API error. Please try again later."}


def test_endpoint_performs_no_database_writes(monkeypatch):
    """A successful rewrite must not touch the database at all."""
    calls = _patch_endpoint_rewrite(monkeypatch, result="A clearer brief.")

    def no_engine(_reason="unused"):
        raise AssertionError(
            "The prompt rewrite endpoint must not open a database connection"
        )

    monkeypatch.setattr("services.database.get_sql_engine", no_engine)
    client = _build_endpoint_client()

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT},
    )

    assert response.status_code == 200
    assert response.json() == {"rewritten_prompt": "A clearer brief."}
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Document context: file_paths validation and owner-scoped resolution
# ---------------------------------------------------------------------------


class _OwnerContextMiddleware:
    """Pure ASGI middleware pinning the request owner ContextVar.

    Mirrors what ``SessionAuthMiddleware`` does in production so the
    owner-scoped temp-file resolution can be exercised through the endpoint.
    """

    def __init__(self, app, owner_id):
        self.app = app
        self.owner_id = owner_id

    async def __call__(self, scope, receive, send):
        token = set_current_owner_id(self.owner_id)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_current_owner_id(token)


@pytest.fixture
def fresh_temp_service(monkeypatch, tmp_path):
    """A TempFileService rooted at a per-test temp directory.

    The endpoint resolves through the process-wide lazy singleton, so the
    singleton is reset and rebuilt from a monkeypatched TEMP_DIRECTORY.
    """
    monkeypatch.setenv("TEMP_DIRECTORY", str(tmp_path / "temp-root"))
    monkeypatch.setattr(
        "services.temp_file_service._temp_file_service_instance", None
    )
    return get_temp_file_service()


def _owner_file_path(service, owner_id, name="report.txt"):
    """Create one temp file under ``owner_id``'s scoped directory."""
    token = set_current_owner_id(owner_id)
    try:
        return service.create_temp_file(name, "extracted document text")
    finally:
        reset_current_owner_id(token)


def test_endpoint_accepts_resolvable_owner_scoped_file_paths(
    monkeypatch, fresh_temp_service
):
    """Valid file_paths resolve within the owner boundary and do not block the rewrite."""
    calls = _patch_endpoint_rewrite(monkeypatch, result="A clearer brief.")
    owner_id = uuid.uuid4()
    file_path = _owner_file_path(fresh_temp_service, owner_id)
    client = _build_endpoint_client(owner_id=owner_id)

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": [file_path]},
    )

    assert response.status_code == 200
    assert response.json() == {"rewritten_prompt": "A clearer brief."}
    assert len(calls) == 1


def test_endpoint_empty_file_paths_behave_like_prompt_only(monkeypatch):
    calls = _patch_endpoint_rewrite(monkeypatch, result="A clearer brief.")
    client = _build_endpoint_client()

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": []},
    )

    assert response.status_code == 200
    assert response.json() == {"rewritten_prompt": "A clearer brief."}
    assert len(calls) == 1


def test_endpoint_rejects_more_than_eight_file_paths_without_invoking_helper(
    monkeypatch,
):
    calls = _patch_endpoint_rewrite(monkeypatch, result="never returned")
    client = _build_endpoint_client()
    paths = [f"/tmp/owner/report-{index}.txt" for index in range(9)]

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": paths},
    )

    assert response.status_code == 422
    assert calls == []


def test_endpoint_missing_temp_file_returns_404_without_invoking_helper(
    monkeypatch, fresh_temp_service
):
    calls = _patch_endpoint_rewrite(monkeypatch, result="never returned")
    owner_id = uuid.uuid4()
    missing_path = str(
        Path(fresh_temp_service.base_dir) / str(owner_id) / "missing.txt"
    )
    client = _build_endpoint_client(owner_id=owner_id)

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": [missing_path]},
    )

    assert response.status_code == 404
    assert response.json()["error_code"] == (
        "error.prompt_rewrite.document_not_found"
    )
    assert calls == []


def test_endpoint_rejects_paths_outside_the_temp_root_without_invoking_helper(
    monkeypatch, tmp_path
):
    calls = _patch_endpoint_rewrite(monkeypatch, result="never returned")
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    client = _build_endpoint_client(owner_id=uuid.uuid4())

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": [str(outside)]},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "error.prompt_rewrite.document_invalid"
    assert calls == []


def test_endpoint_rejects_another_owners_temp_file_without_invoking_helper(
    monkeypatch, fresh_temp_service
):
    calls = _patch_endpoint_rewrite(monkeypatch, result="never returned")
    other_owner = uuid.uuid4()
    other_owner_path = _owner_file_path(fresh_temp_service, other_owner)
    client = _build_endpoint_client(owner_id=uuid.uuid4())

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": [other_owner_path]},
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "error.prompt_rewrite.document_invalid"
    assert calls == []


# ---------------------------------------------------------------------------
# Document-aware composition (plan Task 3)
# ---------------------------------------------------------------------------

DOCUMENT_PROFILE_PAYLOAD = {
    "source_names": ["Document 1"],
    "primary_topic": "Q3 2026 retention decline",
    "stated_purpose": "Explain the retention decline and the approved recovery plan",
    "stated_audience": "Finance leaders",
    "sections": [{"title": "Decline", "summary": "Retention fell 12% year over year"}],
    "entities": ["OrbitDB", "Retention Team"],
    "claims": [
        {
            "text": "Retention fell 12% in Q3 2026",
            "source_name": "Document 1",
        }
    ],
    "numbers_and_dates": [
        {"value": "12%", "context": "Year-over-year retention decline", "source_name": "Document 1"}
    ],
    "constraints": ["Use only verified figures from the report"],
    "conflicts": ["Document 1 states a 12% decline; Document 2 states 9%"],
    "ambiguity": ["The report does not define which cohort the 12% covers"],
    "missing_information": ["Baseline cohort size"],
    "anchor_terms": ["Q3 2026", "OrbitDB"],
}
DOCUMENT_PROFILE = DocumentProfile(**DOCUMENT_PROFILE_PAYLOAD)

DOC_GROUNDED_BRIEF = (
    "Create a Vietnamese presentation brief that explains the verified 2024 "
    "enrollment context of 15,000 students and the 12% retention decline for "
    "decision-makers. Ground it in the attached OrbitDB report for Q3 2026 "
    "as the primary evidence base, keep conflicts explicit, and require "
    "external claims to be verified rather than invented."
)


def test_system_prompt_states_document_source_priority():
    normalized = " ".join(
        build_rewrite_messages(SOURCE_PROMPT, document_profile=DOCUMENT_PROFILE)[0]
        .content.lower()
        .split()
    )

    assert "source priority" in normalized
    assert "1. the explicit user prompt." in normalized
    assert "2. explicit settings such as language and slide count." in normalized
    assert "3. claims and structure stated in the uploaded documents." in normalized
    assert "4. safe presentation defaults." in normalized
    assert "primary evidence base" in normalized
    assert "keep conflicts between sources explicit" in normalized


def test_prompt_builder_embeds_delimited_document_profile_block():
    content = build_rewrite_messages(
        SOURCE_PROMPT, document_profile=DOCUMENT_PROFILE
    )[1].content

    assert "<<<DOCUMENT_PROFILE" in content
    assert "DOCUMENT_PROFILE>>>" in content
    assert "quoted untrusted data" in content.lower()
    assert "Q3 2026" in content
    assert "OrbitDB" in content
    assert "Document 1 states a 12% decline; Document 2 states 9%" in content
    # The source prompt block is unchanged and still present.
    assert "<<<SOURCE_PROMPT" in content
    assert SOURCE_PROMPT in content


def test_prompt_builder_without_profile_has_no_profile_block():
    content = build_rewrite_messages(SOURCE_PROMPT)[1].content

    assert "DOCUMENT_PROFILE" not in content


def test_repair_builder_embeds_delimited_document_profile_block():
    content = build_repair_messages(
        SOURCE_PROMPT,
        "A shallow rewrite.",
        ["missing_document_anchors"],
        language="Vietnamese (Tiếng Việt)",
        n_slides=10,
        document_profile=DOCUMENT_PROFILE,
    )[1].content

    assert "<<<DOCUMENT_PROFILE" in content
    assert "OrbitDB" in content
    assert "missing_document_anchors" in content
    assert "SOURCE_PROMPT" in content


def test_format_document_profile_renders_provenance_and_conflicts():
    rendered = format_document_profile(DOCUMENT_PROFILE)

    assert "[Document 1] Retention fell 12% in Q3 2026" in rendered
    assert "Conflicts (keep explicit; do not resolve):" in rendered
    assert "Anchor terms (preserve exact spelling): Q3 2026, OrbitDB" in rendered
    assert "Stated claims (document-stated, not verified facts):" in rendered


def test_quality_gate_requires_a_document_anchor_term():
    grounded_issues = get_rewrite_quality_issues(
        SOURCE_PROMPT, DOC_GROUNDED_BRIEF, DOCUMENT_PROFILE
    )
    ungrounded_issues = get_rewrite_quality_issues(
        SOURCE_PROMPT, RICH_BRIEF, DOCUMENT_PROFILE
    )

    assert grounded_issues == []
    assert "missing_document_anchors" in ungrounded_issues


def test_quality_gate_requires_profile_protected_numbers():
    brief_without_profile_numbers = DOC_GROUNDED_BRIEF.replace("12% ", "")

    issues = get_rewrite_quality_issues(
        SOURCE_PROMPT, brief_without_profile_numbers, DOCUMENT_PROFILE
    )

    assert "missing_document_identifiers" in issues


def test_quality_gate_bounds_required_profile_identifiers():
    payload = {
        **DOCUMENT_PROFILE_PAYLOAD,
        "numbers_and_dates": [
            {
                "value": value,
                "context": "Reported metric",
                "source_name": "Document 1",
            }
            for value in ("11%", "22%", "33%", "44%", "55%", "66%")
        ],
    }
    profile = DocumentProfile(**payload)
    grounded_brief = (
        "Create a focused presentation for decision-makers using the attached "
        "OrbitDB report as the primary evidence base. Preserve the priority "
        "figures 11%, 22%, and 33%, explain their business context, organize "
        "the narrative from findings to implications and recommended next "
        "steps, keep uncertainty explicit, and require every external claim "
        "to be checked against reliable sources before it is included."
    )

    issues = get_rewrite_quality_issues(SOURCE_PROMPT, grounded_brief, profile)

    assert "missing_document_identifiers" not in issues


def test_quality_gate_does_not_require_contact_metadata_from_document():
    payload = {
        **DOCUMENT_PROFILE_PAYLOAD,
        "numbers_and_dates": [
            {
                "value": "0905516651",
                "context": "Zalo phone number for the instructor",
                "source_name": "Document 1",
            }
        ],
    }
    profile = DocumentProfile(**payload)
    rendered_profile = format_document_profile(profile)

    issues = get_rewrite_quality_issues(
        "Create a presentation about Topic 10",
        (
            "Create a focused presentation about Topic 10 for students new to "
            "OrbitDB. Use the uploaded document as the primary evidence base, "
            "explain the relevant concepts and claims in a logical sequence, "
            "make uncertainty explicit, and verify every external claim before "
            "including it. Finish with a concise synthesis of the topic's main "
            "implications and do not add unsupported facts or figures."
        ),
        profile,
    )

    assert "0905516651" not in rendered_profile
    assert "missing_document_identifiers" not in issues


def _patch_profile_service(monkeypatch, profile=None, error=None):
    """Replace the profile service used by the rewrite call, recording calls."""
    calls = []

    class FakeProfileService:
        async def get_or_build_profile(
            self,
            file_paths,
            language=None,
            display_names=None,
            disconnect_checker=None,
        ):
            calls.append(
                {
                    "file_paths": list(file_paths),
                    "language": language,
                    "disconnect_checker": disconnect_checker,
                }
            )
            if error is not None:
                raise error
            return profile

    monkeypatch.setattr(
        "utils.llm_calls.rewrite_presentation_prompt.DocumentProfileService",
        FakeProfileService,
    )
    return calls


def test_rewrite_builds_profile_from_file_paths_and_grounds_the_brief(monkeypatch):
    profile_calls = _patch_profile_service(monkeypatch, profile=DOCUMENT_PROFILE)
    client, captured = _openai_chat_client(DOC_GROUNDED_BRIEF)
    _patch_llm(monkeypatch, client, "openai")

    async def noop_disconnect():
        return False

    result = asyncio.run(
        rewrite_presentation_prompt(
            SOURCE_PROMPT,
            language="Vietnamese (Tiếng Việt)",
            n_slides=10,
            file_paths=["/temp/owner/report.txt"],
            disconnect_checker=noop_disconnect,
        )
    )

    assert result == DOC_GROUNDED_BRIEF
    assert len(profile_calls) == 1
    assert profile_calls[0]["file_paths"] == ["/temp/owner/report.txt"]
    assert profile_calls[0]["language"] == "Vietnamese (Tiếng Việt)"
    assert profile_calls[0]["disconnect_checker"] is noop_disconnect
    user_content = captured[0]["messages"][1]["content"]
    assert "<<<DOCUMENT_PROFILE" in user_content
    assert "OrbitDB" in user_content


def test_rewrite_without_file_paths_never_builds_a_profile(monkeypatch):
    profile_calls = _patch_profile_service(
        monkeypatch,
        error=AssertionError("profile must not be built without file_paths"),
    )
    client, captured = _openai_chat_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "openai")

    result = asyncio.run(rewrite_presentation_prompt(SOURCE_PROMPT))

    assert result == RICH_BRIEF
    assert profile_calls == []
    assert "DOCUMENT_PROFILE" not in captured[0]["messages"][1]["content"]


def test_rewrite_profile_failure_propagates_without_fallback(monkeypatch):
    _patch_profile_service(
        monkeypatch,
        error=CodedHTTPException(
            status_code=502,
            detail=(
                "The document analysis did not match the expected format. "
                "Please try again."
            ),
            error_code="error.document_profile.build_failed",
        ),
    )
    client, captured = _openai_chat_client(RICH_BRIEF)
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(
            rewrite_presentation_prompt(
                SOURCE_PROMPT, file_paths=["/temp/owner/report.txt"]
            )
        )

    assert excinfo.value.status_code == 502
    assert excinfo.value.error_code == "error.document_profile.build_failed"
    # No completion is attempted: there is no silent without-documents rewrite.
    assert captured == []


def test_rewrite_repairs_missing_document_anchors_once(monkeypatch):
    _patch_profile_service(monkeypatch, profile=DOCUMENT_PROFILE)
    ungrounded = RICH_BRIEF
    client, captured = _sequenced_openai_client([ungrounded, DOC_GROUNDED_BRIEF])
    _patch_llm(monkeypatch, client, "openai")

    result = asyncio.run(
        rewrite_presentation_prompt(
            SOURCE_PROMPT, file_paths=["/temp/owner/report.txt"]
        )
    )

    assert result == DOC_GROUNDED_BRIEF
    assert len(captured) == 2
    repair_content = captured[1]["messages"][1]["content"]
    assert "missing_document_anchors" in repair_content
    assert "<<<DOCUMENT_PROFILE" in repair_content


def test_rewrite_rejects_a_completion_still_missing_document_anchors(monkeypatch):
    _patch_profile_service(monkeypatch, profile=DOCUMENT_PROFILE)
    client, captured = _sequenced_openai_client([RICH_BRIEF, RICH_BRIEF])
    _patch_llm(monkeypatch, client, "openai")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(
            rewrite_presentation_prompt(
                SOURCE_PROMPT, file_paths=["/temp/owner/report.txt"]
            )
        )

    assert len(captured) == 2
    assert excinfo.value.status_code == 502
    assert excinfo.value.error_code == "error.prompt_rewrite.quality_too_low"


def test_endpoint_passes_resolved_file_paths_to_the_helper(
    monkeypatch, fresh_temp_service
):
    calls = _patch_endpoint_rewrite(monkeypatch, result="A document-aware brief.")
    owner_id = uuid.uuid4()
    file_path = _owner_file_path(fresh_temp_service, owner_id)
    client = _build_endpoint_client(owner_id=owner_id)

    response = client.post(
        "/api/v1/ppt/prompt/rewrite",
        json={"prompt": SOURCE_PROMPT, "file_paths": [file_path]},
    )

    assert response.status_code == 200
    assert calls[0]["file_paths"] == [file_path]
