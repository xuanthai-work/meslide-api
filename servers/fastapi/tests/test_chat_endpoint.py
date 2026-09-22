import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from api.v1.ppt.endpoints.chat import (
    chat_message_stream,
    delete_chat_conversation,
    get_chat_history,
)
from services.chat import ChatStreamStatus, ChatTurnResult


def test_chat_message_stream_wraps_response_with_safe_sse_stream():
    payload = SimpleNamespace(
        presentation_id=uuid.uuid4(),
        conversation_id=None,
        message="Update the table",
        attachments=[],
    )
    sql_session = MagicMock()

    def passthrough(stream, *, logger, error_detail, error_code=None):
        assert logger.name == "api.v1.ppt.endpoints.chat"
        assert error_detail == "Chat stream failed"
        assert error_code == "error.chat_stream_failed"
        return stream

    with (
        patch("api.v1.ppt.endpoints.chat.PresentationChatService"),
        patch(
            "api.v1.ppt.endpoints.chat.safe_sse_stream",
            side_effect=passthrough,
        ) as safe_stream,
    ):
        response = asyncio.run(
            chat_message_stream(payload=payload, sql_session=sql_session)
        )

    safe_stream.assert_called_once()
    assert response.media_type == "text/event-stream"


def test_chat_message_stream_emits_status_and_trace_codes():
    payload = SimpleNamespace(
        presentation_id=uuid.uuid4(),
        conversation_id=None,
        message="Update the table",
        attachments=[],
    )
    sql_session = MagicMock()
    conversation_id = uuid.uuid4()

    async def fake_stream_reply(message, attachments=None):
        yield (
            "status",
            ChatStreamStatus(
                message="Reading deck context",
                code="chat.reading_deck_context",
            ),
        )
        yield "trace", {
            "kind": "tool_plan",
            "round": 1,
            "tools": ["saveSlide"],
            "message": "Using tools: saveSlide",
            "code": "chat.trace.using_tools",
            "params": {"tools": "saveSlide"},
        }
        yield "chunk", "Saved."
        yield "complete", ChatTurnResult(
            conversation_id=conversation_id,
            response_text="Saved.",
            tool_calls=["saveSlide"],
        )

    with patch(
        "api.v1.ppt.endpoints.chat.PresentationChatService"
    ) as service_cls:
        service_cls.return_value.stream_reply = fake_stream_reply
        response = asyncio.run(
            chat_message_stream(payload=payload, sql_session=sql_session)
        )

    async def collect():
        frames = []
        async for frame in response.body_iterator:
            frames.append(frame)
        return frames

    frames = asyncio.run(collect())
    payloads = [
        json.loads(frame.strip().split("\n")[1][len("data: ") :]) for frame in frames
    ]

    assert payloads[0] == {
        "type": "status",
        "status": "Reading deck context",
        "status_code": "chat.reading_deck_context",
    }
    assert payloads[1] == {
        "type": "trace",
        "trace": {
            "kind": "tool_plan",
            "round": 1,
            "tools": ["saveSlide"],
            "message": "Using tools: saveSlide",
            "code": "chat.trace.using_tools",
            "params": {"tools": "saveSlide"},
        },
    }
    assert payloads[2] == {"type": "chunk", "chunk": "Saved."}
    assert payloads[3]["type"] == "complete"
    assert payloads[3]["chat"]["conversation_id"] == str(conversation_id)


def test_delete_chat_conversation_deletes_sql_thread_and_commits():
    presentation_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    sql_session = MagicMock()
    sql_session.commit = AsyncMock()

    with patch(
        "api.v1.ppt.endpoints.chat.sql_chat_history.delete_conversation",
        new=AsyncMock(),
    ) as delete_conversation:
        asyncio.run(
            delete_chat_conversation(
                presentation_id=presentation_id,
                conversation_id=conversation_id,
                sql_session=sql_session,
            )
        )

    delete_conversation.assert_awaited_once_with(
        sql_session,
        presentation_id=presentation_id,
        conversation_id=conversation_id,
    )
    sql_session.commit.assert_awaited_once()


def test_get_chat_history_returns_existing_sql_messages_without_migration():
    presentation_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    sql_session = MagicMock()
    sql_session.commit = AsyncMock()
    rows = [
        {
            "role": "user",
            "content": "Make the title shorter",
            "created_at": "2026-07-15T10:00:00+00:00",
        },
        {
            "role": "assistant",
            "content": "The title has been shortened.",
            "created_at": "2026-07-15T10:00:01+00:00",
        },
    ]

    with (
        patch(
            "api.v1.ppt.endpoints.chat.sql_chat_history.load_messages_with_meta",
            new=AsyncMock(return_value=rows),
        ) as load_messages,
        patch(
            "api.v1.ppt.endpoints.chat.ChatConversationStore.load_history",
            new=AsyncMock(),
        ) as load_legacy,
    ):
        response = asyncio.run(
            get_chat_history(
                presentation_id=presentation_id,
                conversation_id=conversation_id,
                sql_session=sql_session,
            )
        )

    assert [message.content for message in response.messages] == [
        "Make the title shorter",
        "The title has been shortened.",
    ]
    load_messages.assert_awaited_once()
    load_legacy.assert_not_awaited()
    sql_session.commit.assert_not_awaited()


def test_get_chat_history_migrates_and_returns_legacy_messages():
    presentation_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    sql_session = MagicMock()
    sql_session.commit = AsyncMock()
    migrated_rows = [
        {"role": "user", "content": "Use a blue theme"},
        {"role": "assistant", "content": "Applied the blue theme."},
    ]

    with (
        patch(
            "api.v1.ppt.endpoints.chat.sql_chat_history.load_messages_with_meta",
            new=AsyncMock(side_effect=[[], migrated_rows]),
        ) as load_messages,
        patch(
            "api.v1.ppt.endpoints.chat.ChatConversationStore.load_history",
            new=AsyncMock(return_value=migrated_rows),
        ) as load_legacy,
    ):
        response = asyncio.run(
            get_chat_history(
                presentation_id=presentation_id,
                conversation_id=conversation_id,
                sql_session=sql_session,
            )
        )

    assert [message.content for message in response.messages] == [
        "Use a blue theme",
        "Applied the blue theme.",
    ]
    load_legacy.assert_awaited_once_with(
        presentation_id=presentation_id,
        conversation_id=conversation_id,
    )
    assert load_messages.await_count == 2
    sql_session.commit.assert_awaited_once()
