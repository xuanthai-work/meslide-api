"""Phase 5: chat trace/status events carry stable machine codes.

Every distinct ``trace.message`` emitted by ``services/chat/service.py``
must have a code; the legacy English message stays verbatim.
"""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services.chat import service as service_module
from services.chat.service import ChatStreamStatus, ChatTurnResult, PresentationChatService

# The complete reconciled registry: tool -> (legacy message, trace code).
EXPECTED_TOOL_START_TRACES = {
    "addOutline": ("Adding an outline slide", "chat.trace.adding_outline_slide"),
    "updateOutline": ("Updating the outline slide", "chat.trace.updating_outline_slide"),
    "deleteOutline": ("Deleting the outline slide", "chat.trace.deleting_outline_slide"),
    "addNewSlide": ("Adding a blank slide", "chat.trace.adding_blank_slide"),
    "addNewSlideLayout": ("Adding slide from layout", "chat.trace.adding_slide_from_layout"),
    "getTemplateSummary": ("Reading template summary", "chat.trace.reading_template_summary"),
    "readSourceDocuments": ("Reading source documents", "chat.trace.reading_source_documents"),
    "searchSlide": ("Searching relevant slides", "chat.trace.searching_relevant_slides"),
    "getSlideAtIndex": ("Opening the requested slide", "chat.trace.opening_requested_slide"),
    "getAvailableLayouts": ("Checking available layouts", "chat.trace.checking_available_layouts"),
    "getContentSchemaFromLayoutId": (
        "Reading layout content schema",
        "chat.trace.checking_layout_schema",
    ),
    "generateAssets": ("Generating slide assets", "chat.trace.generating_slide_assets"),
    "saveSlide": ("Saving the slide", "chat.trace.saving_slide"),
    "updateSlide": ("Updating the slide", "chat.trace.updating_slide"),
    "deleteSlide": ("Deleting the slide", "chat.trace.deleting_slide"),
    "addElement": ("Adding slide element", "chat.trace.adding_slide_element"),
    "updateElement": ("Updating slide element", "chat.trace.updating_slide_element"),
    "deleteElement": ("Removing slide element", "chat.trace.removing_slide_element"),
    "addComponent": ("Adding slide component", "chat.trace.adding_slide_component"),
    "createComponent": ("Creating slide component", "chat.trace.creating_slide_component"),
    "updateComponent": ("Updating slide component", "chat.trace.updating_slide_component"),
    "deleteComponent": ("Removing slide component", "chat.trace.removing_slide_component"),
    "getPresentationTheme": ("Checking available themes", "chat.trace.checking_available_themes"),
    "setPresentationTheme": (
        "Applying presentation theme",
        "chat.trace.applying_presentation_theme",
    ),
}


def test_every_tool_start_trace_has_verbatim_message_and_code():
    """The start-trace registry keeps every legacy message verbatim and
    assigns a code to each (guards against silent rewording)."""
    assert service_module.TOOL_START_TRACES == EXPECTED_TOOL_START_TRACES


@pytest.mark.parametrize("tool_name", sorted(EXPECTED_TOOL_START_TRACES))
def test_known_tool_start_trace_returns_message_and_code(tool_name):
    message, code, params = PresentationChatService._tool_start_trace(tool_name)
    assert message == EXPECTED_TOOL_START_TRACES[tool_name][0]
    assert code == EXPECTED_TOOL_START_TRACES[tool_name][1]
    assert params is None


def test_unknown_tool_start_trace_uses_running_tool_code():
    assert PresentationChatService._tool_start_trace("mysteryTool") == (
        "Running mysteryTool",
        "chat.trace.running_tool",
        {"tool": "mysteryTool"},
    )


@pytest.mark.parametrize(
    ("tool_result", "expected"),
    [
        (
            {
                "ok": False,
                "error": "boom",
                "recovery": {"guidance": ["retry with a valid index"]},
            },
            (
                "saveSlide failed: boom Recovery: retry with a valid index",
                "chat.trace.tool_failed",
                {
                    "tool": "saveSlide",
                    "reason": "boom",
                    "recovery": "retry with a valid index",
                },
            ),
        ),
        (
            {"ok": False, "error": "boom"},
            (
                "saveSlide failed: boom",
                "chat.trace.tool_failed",
                {"tool": "saveSlide", "reason": "boom"},
            ),
        ),
        (
            {"ok": False},
            ("saveSlide failed.", "chat.trace.tool_failed", {"tool": "saveSlide"}),
        ),
        (
            {"ok": True, "result": {"message": "New slide saved at index 0."}},
            (
                "New slide saved at index 0.",
                "chat.trace.tool_result",
                {"tool": "saveSlide"},
            ),
        ),
        (
            {"ok": True, "result": {"note": "Read 2 source document(s)."}},
            ("Read 2 source document(s).", "chat.trace.tool_result", {"tool": "saveSlide"}),
        ),
        (
            {"ok": True, "result": {"count": 3}},
            (
                "saveSlide returned 3 result(s).",
                "chat.trace.tool_returned_results",
                {"tool": "saveSlide", "count": 3},
            ),
        ),
        (
            {"ok": True, "result": {"found": True}},
            (
                "saveSlide found requested data.",
                "chat.trace.found_requested_data",
                {"tool": "saveSlide"},
            ),
        ),
        (
            {"ok": True, "result": {"found": False}},
            (
                "saveSlide did not find matching data.",
                "chat.trace.no_matching_data",
                {"tool": "saveSlide"},
            ),
        ),
        (
            {"ok": True},
            ("saveSlide completed.", "chat.trace.tool_completed", {"tool": "saveSlide"}),
        ),
    ],
)
def test_tool_result_trace_assigns_code_and_params(tool_result, expected):
    assert (
        PresentationChatService._tool_result_trace("saveSlide", tool_result) == expected
    )


def test_stream_reply_emits_status_and_trace_codes(
    fake_async_session, monkeypatch
):
    service = PresentationChatService(
        sql_session=fake_async_session,
        presentation_id=uuid.uuid4(),
        conversation_id=None,
    )
    conversation_id = uuid.uuid4()

    async def fake_prepare_turn_context(_self, user_message, attachments):
        return conversation_id, [], "hello"

    async def fake_persist_turn(_self, **_kwargs):
        return ChatTurnResult(
            conversation_id=conversation_id,
            response_text="Saved.",
            tool_calls=["saveSlide"],
        )

    monkeypatch.setattr(
        PresentationChatService, "_prepare_turn_context", fake_prepare_turn_context
    )
    monkeypatch.setattr(PresentationChatService, "_persist_turn", fake_persist_turn)
    monkeypatch.setattr(service_module, "get_llm_client", lambda: object())
    monkeypatch.setattr(service_module, "get_model", lambda: "fake-model")
    monkeypatch.setattr(service_module, "build_chat_llm_tools", lambda tools: [])
    monkeypatch.setattr(service_module, "get_generate_kwargs", lambda **kwargs: {})

    round_counter = {"n": 0}

    def fake_stream_generate_events(_client, **_kwargs):
        round_index = round_counter["n"]
        round_counter["n"] += 1

        async def gen():
            if round_index == 0:
                yield SimpleNamespace(
                    type="completion",
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call-1", name="saveSlide", arguments="{}"
                        )
                    ],
                )
            else:
                yield SimpleNamespace(type="content", chunk="Saved.")
                yield SimpleNamespace(
                    type="completion", content=None, tool_calls=[]
                )

        return gen()

    monkeypatch.setattr(
        service_module, "stream_generate_events", fake_stream_generate_events
    )
    monkeypatch.setattr(
        service._tools,
        "execute_tool_call",
        AsyncMock(
            return_value={"ok": True, "result": {"message": "New slide saved at index 0."}}
        ),
    )

    async def runner():
        return [event async for event in service.stream_reply("hello")]

    events = asyncio.run(runner())

    assert events[0] == (
        "status",
        ChatStreamStatus(
            message="Reading deck context", code="chat.reading_deck_context"
        ),
    )

    traces = [value for kind, value in events if kind == "trace"]
    assert traces[0]["kind"] == "tool_plan"
    assert traces[0]["message"] == "Using tools: saveSlide"
    assert traces[0]["code"] == "chat.trace.using_tools"
    assert traces[0]["params"] == {"tools": "saveSlide"}

    assert traces[1]["kind"] == "tool_call"
    assert traces[1]["status"] == "start"
    assert traces[1]["message"] == "Saving the slide"
    assert traces[1]["code"] == "chat.trace.saving_slide"
    assert "params" not in traces[1]

    assert traces[2]["kind"] == "tool_call"
    assert traces[2]["status"] == "success"
    assert traces[2]["message"] == "New slide saved at index 0."
    assert traces[2]["code"] == "chat.trace.tool_result"
    assert traces[2]["params"] == {"tool": "saveSlide"}

    statuses = [value for kind, value in events if kind == "status"]
    assert statuses[-1] == ChatStreamStatus(
        message="Saving chat", code="chat.saving_chat"
    )

    assert ("chunk", "Saved.") in events
    assert events[-1][0] == "complete"
    assert isinstance(events[-1][1], ChatTurnResult)
