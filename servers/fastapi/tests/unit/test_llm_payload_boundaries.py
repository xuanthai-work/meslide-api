import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from enums.llm_provider import LLMProvider
from utils import llm_utils
from utils.llm_dispatch_payloads import chat_completion_kwargs
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
            tool_calls=[
                AssistantToolCall(id="call-1", name="search", arguments='{"q":"x"}')
            ]
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
            tool_calls=[
                AssistantToolCall(id="call-1", name="search", arguments='{"q":"x"}')
            ]
        ),
        ToolResponseMessage(id="call-1", content=["found"], name="search"),
    ]

    assert _convert_messages_for_provider(messages, LLMProvider.GOOGLE) == [
        {"role": "user", "parts": [{"text": "hello"}]},
        {
            "role": "model",
            "parts": [{"function_call": {"name": "search", "args": {"q": "x"}}}],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": "search",
                        "response": {"result": "found"},
                    }
                }
            ],
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
    assert (
        exc_info.value.detail
        == "Invalid LLM provider. Please select one of: openai, google, custom"
    )


def test_llm_utils_keeps_normalizer_export():
    assert llm_utils.normalize_content_parts is normalize_content_parts
    assert "normalize_content_parts" in llm_utils.__all__
    assert "messages_to_provider_payload" in llm_utils.__all__


def test_responses_schema_unwrap_preserves_name_strict_and_schema():
    from utils.llm_responses_payload import _to_responses_text_format

    schema = {"type": "object", "properties": {"title": {"type": "string"}}}
    source = {
        "type": "json_schema",
        "json_schema": {
            "name": "deck",
            "schema": schema,
            "strict": True,
        },
    }

    assert _to_responses_text_format(source) == {
        "type": "json_schema",
        "name": "deck",
        "schema": schema,
        "strict": True,
    }
    assert source["json_schema"]["schema"] == schema
    assert llm_utils._to_responses_text_format is _to_responses_text_format


def test_responses_schema_defaults_strict_and_passes_through_other_formats():
    from utils.llm_responses_payload import _to_responses_text_format

    source = {
        "type": "json_schema",
        "json_schema": {"name": "deck", "schema": {"type": "object"}},
    }
    assert _to_responses_text_format(source) == {
        "type": "json_schema",
        "name": "deck",
        "schema": {"type": "object"},
        "strict": False,
    }
    other = {"type": "json_object"}
    assert _to_responses_text_format(other) is other
    assert _to_responses_text_format(None) is None


def test_responses_tools_flatten_functions_and_keep_other_tools():
    from utils.llm_responses_payload import _tools_for_responses_api

    tools = [
        {
            "type": "function",
            "function": {
                "name": "search",
                "description": "Find slides",
                "parameters": {
                    "type": "object",
                    "properties": {"q": {"type": "string"}},
                },
                "strict": True,
            },
        },
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        },
        {"type": "web_search"},
    ]

    assert _tools_for_responses_api(tools) == [
        {
            "type": "function",
            "name": "search",
            "description": "Find slides",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            "strict": True,
        },
        {
            "type": "function",
            "name": "lookup",
            "description": None,
            "parameters": {"type": "object"},
            "strict": False,
        },
        {"type": "web_search"},
    ]
    assert tools[0]["function"]["name"] == "search"
    assert llm_utils._tools_for_responses_api is _tools_for_responses_api


def test_feedback_limits_errors_and_previous_json():
    from utils.llm_schema_feedback import structured_validation_feedback_user_message

    feedback = structured_validation_feedback_user_message(
        {"body": "é" * 7000}, [f"e{i}" for i in range(11)]
    )

    assert feedback.content.startswith(
        "The previous JSON response did not match the required response schema.\n\n"
        "Validation errors:\n"
    )
    assert "- e0\n" in feedback.content
    assert "- e9\n" in feedback.content
    assert "- e10" not in feedback.content
    assert "...and 1 more validation errors." in feedback.content
    assert feedback.content.count("\n- ") == 11
    previous_json = feedback.content.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert len(previous_json.split("\n... (truncated)", 1)[0]) == 6000
    assert previous_json.endswith("\n... (truncated)")
    assert "é" in previous_json
    assert "\\u00e9" not in previous_json
    assert feedback.content.endswith(
        "Return corrected JSON only. Make sure it fully matches the required schema."
    )
    assert llm_utils.structured_validation_feedback_user_message is (
        structured_validation_feedback_user_message
    )
    assert "structured_validation_feedback_user_message" in llm_utils.__all__


def test_feedback_keeps_exact_prompt_and_serializes_non_json_values():
    from utils.llm_schema_feedback import structured_validation_feedback_user_message

    feedback = structured_validation_feedback_user_message(
        {"price": Decimal("1.25")}, ["price: expected string"]
    )

    assert feedback.content == (
        "The previous JSON response did not match the required response schema.\n\n"
        "Validation errors:\n"
        "- price: expected string\n\n"
        "Previous invalid JSON:\n"
        '```json\n{\n  "price": "1.25"\n}\n```\n\n'
        "Return corrected JSON only. Make sure it fully matches the required schema."
    )


def test_chat_kwargs_omit_unset_and_empty_optional_fields():
    messages = [{"role": "user", "content": "a"}]
    expected = {"model": "m", "messages": messages, "stream": False}

    assert (
        chat_completion_kwargs("m", messages, False, None, None, None, None) == expected
    )
    assert chat_completion_kwargs("m", messages, False, [], None, None, {}) == expected


def test_chat_kwargs_preserve_present_options_and_falsey_non_none_values():
    messages = [{"role": "user", "content": "a"}]
    tools = [{"type": "function", "function": {"name": "search"}}]
    response_format = {"type": "json_object"}
    extra_body = {"metadata": {"source": "test"}}

    kwargs = chat_completion_kwargs(
        "m", messages, True, tools, response_format, 0, extra_body
    )
    assert kwargs == {
        "model": "m",
        "messages": messages,
        "stream": True,
        "tools": tools,
        "response_format": response_format,
        "max_tokens": 0,
        "extra_body": extra_body,
    }
    assert kwargs["messages"] is messages
    assert kwargs["tools"] is tools
    assert kwargs["response_format"] is response_format
    assert kwargs["extra_body"] is extra_body
    assert (
        chat_completion_kwargs("m", messages, False, None, {}, None, None)[
            "response_format"
        ]
        == {}
    )


@pytest.mark.parametrize("provider", [LLMProvider.OPENAI, LLMProvider.CUSTOM])
def test_chat_provider_branches_use_shared_kwargs(monkeypatch, provider):
    monkeypatch.setattr(llm_utils, "get_llm_provider", lambda: provider)
    monkeypatch.setattr(llm_utils, "use_responses_api", lambda *args, **kwargs: False)
    messages = [{"role": "user", "content": "a"}]
    calls = []
    sentinel = {"shared": "payload"}

    def build_kwargs(*args):
        calls.append(args)
        return sentinel

    monkeypatch.setattr(llm_utils, "chat_completion_kwargs", build_kwargs)

    class Completions:
        def create(self, **kwargs):
            assert kwargs == sentinel
            return sentinel

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    result = asyncio.run(
        llm_utils._dispatch_chat_completion(
            client,
            model="m",
            messages=messages,
            stream=True,
            tools=[{"type": "function"}],
            response_format={},
            max_tokens=0,
            extra_body={"metadata": "x"},
        )
    )
    assert result is sentinel
    assert calls == [
        (
            "m",
            messages,
            True,
            [{"type": "function"}],
            {},
            0,
            {"metadata": "x"},
        )
    ]
