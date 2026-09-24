import pytest
from fastapi import HTTPException

from enums.llm_provider import LLMProvider
from utils import llm_utils
from utils.llm_message_payloads import (
    _convert_messages_for_provider,
    normalize_content_parts,
)
from utils.llm_messages import (
    AssistantMessage,
    AssistantToolCall,
    SystemMessage,
    TextContentPart,
    ToolResponseMessage,
    UserMessage,
)


def test_string_content_becomes_one_text_part():
    assert normalize_content_parts("hello") == [TextContentPart(text="hello")]


def test_normalize_content_parts_preserves_existing_parts_and_empty_content():
    part = TextContentPart(text="hello")
    source = [part, "world"]
    assert normalize_content_parts(None) == []
    assert normalize_content_parts(source) == source
    assert normalize_content_parts(source) is not source
    assert normalize_content_parts(part) == [part]


@pytest.mark.parametrize("provider", [LLMProvider.OPENAI, LLMProvider.CUSTOM])
def test_openai_compatible_providers_keep_message_shape(provider):
    messages = [
        SystemMessage(content="instructions"),
        UserMessage(content="hello"),
        AssistantMessage(
            tool_calls=[AssistantToolCall(id="call-1", name="search", arguments='{"q":"x"}')]
        ),
        ToolResponseMessage(id="call-1", content=["found"], name="search"),
    ]

    assert _convert_messages_for_provider(messages, provider) == [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "hello"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q":"x"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "found"},
    ]


def test_google_payload_keeps_system_exclusion_and_tool_response():
    messages = [
        SystemMessage(content="instructions"),
        UserMessage(content="hello"),
        AssistantMessage(
            tool_calls=[AssistantToolCall(id="call-1", name="search", arguments='{"q":"x"}')]
        ),
        ToolResponseMessage(id="call-1", content=["found"], name="search"),
    ]

    assert _convert_messages_for_provider(messages, LLMProvider.GOOGLE) == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {"role": "model", "parts": [{"function_call": {"name": "search", "args": {"q": "x"}}}]},
        {
            "role": "user",
            "parts": [{"function_response": {"name": "search", "response": {"result": "found"}}}],
        },
    ]


def test_wrapper_uses_llm_utils_provider_patch_seam(monkeypatch):
    monkeypatch.setattr(llm_utils, "get_llm_provider", lambda: LLMProvider.GOOGLE)
    assert llm_utils.messages_to_provider_payload([UserMessage(content="hello")]) == [
        {"role": "user", "parts": [{"text": "hello"}]}
    ]


def test_invalid_provider_keeps_http_error():
    with pytest.raises(HTTPException) as exc_info:
        _convert_messages_for_provider([UserMessage(content="hello")], "invalid")

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Invalid LLM provider. Please select one of: openai, google, custom"


def test_llm_utils_keeps_normalizer_export():
    assert llm_utils.normalize_content_parts is normalize_content_parts
    assert "normalize_content_parts" in llm_utils.__all__
    assert "messages_to_provider_payload" in llm_utils.__all__
