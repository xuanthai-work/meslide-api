"""Phase 5: SSE frames gain optional status_code/error_code fields.

Legacy frames (no code fields) must serialize byte-identically to the
pre-Phase-5 output; frames with codes must include the new fields (with
None fields omitted).
"""
import json

from models.sse_response import (
    SSECompleteResponse,
    SSEErrorResponse,
    SSEHeartbeatResponse,
    SSEStatusResponse,
    SSETraceResponse,
)


def _payload(frame: str) -> dict:
    lines = frame.strip().split("\n")
    assert lines[0] == "event: response"
    assert lines[1].startswith("data: ")
    return json.loads(lines[1][len("data: ") :])


def test_status_frame_without_code_is_byte_identical_to_legacy():
    assert SSEStatusResponse(status="ok").to_string() == (
        'event: response\ndata: {"type": "status", "status": "ok"}\n\n'
    )


def test_status_frame_with_code_only_omits_params():
    frame = SSEStatusResponse(
        status="Drafting your presentation outline",
        status_code="outline.drafting",
    ).to_string()
    assert _payload(frame) == {
        "type": "status",
        "status": "Drafting your presentation outline",
        "status_code": "outline.drafting",
    }


def test_status_frame_with_code_and_params_serializes_both():
    frame = SSEStatusResponse(
        status="Searching with SearXNG: current market",
        status_code="outline.searching_provider",
        status_params={"provider": "SearXNG", "query": "current market"},
    ).to_string()
    assert _payload(frame) == {
        "type": "status",
        "status": "Searching with SearXNG: current market",
        "status_code": "outline.searching_provider",
        "status_params": {"provider": "SearXNG", "query": "current market"},
    }


def test_error_frame_without_code_is_byte_identical_to_legacy():
    assert SSEErrorResponse(detail="bad").to_string() == (
        'event: response\ndata: {"type": "error", "detail": "bad"}\n\n'
    )


def test_error_frame_with_code_only_omits_params():
    frame = SSEErrorResponse(
        detail="Number of slides cannot be greater than 20",
        error_code="error.slides_too_many",
    ).to_string()
    assert _payload(frame) == {
        "type": "error",
        "detail": "Number of slides cannot be greater than 20",
        "error_code": "error.slides_too_many",
    }


def test_error_frame_with_code_and_params_serializes_both():
    frame = SSEErrorResponse(
        detail="Failed to generate presentation outlines. Please try again. boom",
        error_code="error.outline_parse_failed",
        error_params={"reason": "boom"},
    ).to_string()
    assert _payload(frame) == {
        "type": "error",
        "detail": "Failed to generate presentation outlines. Please try again. boom",
        "error_code": "error.outline_parse_failed",
        "error_params": {"reason": "boom"},
    }


def test_trace_frame_passes_code_and_params_keys_through():
    trace = {
        "kind": "tool_plan",
        "round": 1,
        "tools": ["saveSlide"],
        "message": "Using tools: saveSlide",
        "code": "chat.trace.using_tools",
        "params": {"tools": "saveSlide"},
    }
    frame = SSETraceResponse(trace=trace).to_string()
    assert _payload(frame) == {"type": "trace", "trace": trace}


def test_heartbeat_and_complete_frames_unchanged():
    assert SSEHeartbeatResponse().to_string() == (
        'event: response\ndata: {"type": "heartbeat"}\n\n'
    )
    frame = SSECompleteResponse(key="chat", value={"conversation_id": "x"}).to_string()
    assert _payload(frame) == {"type": "complete", "chat": {"conversation_id": "x"}}