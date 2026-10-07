import json
import logging
import os
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any, Literal

import dirtyjson  # type: ignore[import-untyped]
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from models.chat import ChatAttachment
from models.sql.presentation import PresentationModel
from services.chat.conversation_store import ChatConversationStore
from services.chat.presentation_context_store import PresentationContextStore
from services.chat.prompts import build_system_prompt
from services.chat.llm_tools import build_chat_llm_tools
from services.chat.tools import ChatTools
from services.documents_loader import DocumentsLoader
from services.mem0_presentation_memory_service import MEM0_PRESENTATION_MEMORY_SERVICE
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.llm_client_error_handler import handle_llm_client_exceptions
from utils.llm_messages import (
    AssistantMessage,
    AssistantToolCall,
    Message,
    SystemMessage,
    TextContentPart,
    ToolResponseMessage,
    UserMessage,
)
from utils.llm_provider import get_llm_client, get_model
from utils.llm_utils import (
    extract_text,
    get_generate_kwargs,
    stream_generate_events,
)

LOGGER = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 40
MAX_IDENTICAL_POSITION_CALLS = 3
POSITION_MUTATION_TOOLS = {"updateComponent", "updateElement"}
MAX_CHAT_ATTACHMENT_CONTEXT_CHARS = 6000
MAX_CHAT_ATTACHMENT_FILE_CHARS = 3000
DOCUMENT_CONTENT_INTENT_PATTERN = re.compile(
    r"\b(read|extract|parse|analy[sz]e|summari[sz]e|review|reference|cite|quote|"
    r"compare|content|from|data|numbers?|metrics?|table|chart|outline|"
    r"presentation|create|generate|build|draft|write|convert)\b|"
    r"\bbased\s+on\b|\baccording\s+to\b|"
    r"\buse\s+(?:the\s+)?(?:attached|provided|this)\s+"
    r"(?:document|pdf|file|attachment)\b",
    re.IGNORECASE,
)
DIRECT_FILE_PLACEMENT_PATTERN = re.compile(
    r"\b(place|put|insert|attach|display|show|move|resize|position|replace|add)\b",
    re.IGNORECASE,
)

# Tool-call start traces: tool name -> (legacy message, stable trace code).
# The message text stays verbatim for legacy clients; the code lets
# localized clients map the trace without matching English prose.
TOOL_START_TRACES: dict[str, tuple[str, str]] = {
    "addNewSlide": ("Adding a blank slide", "chat.trace.adding_blank_slide"),
    "addNewSlideLayout": ("Adding slide from layout", "chat.trace.adding_slide_from_layout"),
    "getTemplateSummary": ("Reading template summary", "chat.trace.reading_template_summary"),
    "readSourceDocuments": ("Reading source documents", "chat.trace.reading_source_documents"),
    "searchSlide": ("Searching relevant slides", "chat.trace.searching_relevant_slides"),
    "getSlideAtIndex": ("Opening the requested slide", "chat.trace.opening_requested_slide"),
    "getAvailableLayouts": (
        "Checking available layouts",
        "chat.trace.checking_available_layouts",
    ),
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
    "getPresentationTheme": (
        "Checking available themes",
        "chat.trace.checking_available_themes",
    ),
    "setPresentationTheme": (
        "Applying presentation theme",
        "chat.trace.applying_presentation_theme",
    ),
}


@dataclass(frozen=True)
class ChatTurnResult:
    conversation_id: uuid.UUID
    response_text: str
    tool_calls: list[str]


@dataclass(frozen=True)
class ChatStreamStatus:
    """A chat status event with its stable machine code.

    ``message`` is the legacy English text (kept verbatim); ``code`` lets
    localized clients map the status without matching prose.
    """

    message: str
    code: str


ChatStreamEventType = Literal["chunk", "complete", "status", "trace"]
ChatStreamEventValue = str | ChatTurnResult | ChatStreamStatus | dict[str, Any]


class PresentationChatService:
    def __init__(
        self,
        sql_session: AsyncSession,
        presentation_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
    ):
        self._sql_session = sql_session
        self._presentation_id = presentation_id
        self._conversation_id = conversation_id
        self._conversation_store = ChatConversationStore(sql_session)
        self._memory = PresentationContextStore(
            sql_session,
            presentation_id,
        )
        self._tools = ChatTools(self._memory)

    async def generate_reply(
        self,
        user_message: str,
        attachments: list[ChatAttachment] | None = None,
    ) -> ChatTurnResult:
        self._tools.set_turn_context(user_message)
        conversation_id, messages, persisted_user_message = await self._prepare_turn_context(
            user_message,
            attachments or [],
        )
        response_text, tool_calls = await self._run_llm_with_tools(messages)
        return await self._persist_turn(
            conversation_id=conversation_id,
            user_message=persisted_user_message,
            response_text=response_text,
            tool_calls=tool_calls,
        )

    async def stream_reply(
        self,
        user_message: str,
        attachments: list[ChatAttachment] | None = None,
    ) -> AsyncGenerator[tuple[ChatStreamEventType, ChatStreamEventValue], None]:
        self._tools.set_turn_context(user_message)
        yield "status", ChatStreamStatus(
            message="Reading deck context",
            code="chat.reading_deck_context",
        )
        conversation_id, messages, persisted_user_message = await self._prepare_turn_context(
            user_message,
            attachments or [],
        )

        client = get_llm_client()
        model = get_model()
        tools = build_chat_llm_tools(self._tools.get_tool_definitions())

        called_tools: list[str] = []
        last_tool_results: list[dict[str, Any]] = []
        response_text: str | None = None
        recent_position_signatures: list[str] = []

        for round_index in range(MAX_TOOL_ROUNDS):
            completion_chunk: Any | None = None
            round_content_chunks: list[str] = []
            thinking_chunks: list[str] = []

            try:
                async for event in stream_generate_events(
                    client,
                    **get_generate_kwargs(
                        model=model,
                        messages=messages,
                        tools=tools,
                        stream=True,
                    ),
                ):
                    event_type = getattr(event, "type", None)
                    if event_type == "content":
                        chunk = getattr(event, "chunk", None)
                        if chunk:
                            round_content_chunks.append(chunk)
                            yield "chunk", chunk
                    elif event_type == "thinking":
                        thinking_text = self._event_text(event)
                        if thinking_text:
                            thinking_chunks.append(thinking_text)
                    elif event_type == "completion":
                        completion_chunk = event
            except Exception as exc:
                raise handle_llm_client_exceptions(exc)

            thinking_summary = self._summarize_model_note(thinking_chunks)
            if thinking_summary:
                yield "trace", {
                    "kind": "model_note",
                    "round": round_index + 1,
                    "status": "info",
                    "message": thinking_summary,
                    "code": "chat.trace.model_note",
                }

            completion_tool_calls = list(
                getattr(completion_chunk, "tool_calls", []) or []
            )
            if completion_tool_calls:
                tool_names = [tool_call.name for tool_call in completion_tool_calls]
                called_tools.extend(tool_names)
                yield "trace", {
                    "kind": "tool_plan",
                    "round": round_index + 1,
                    "tools": tool_names,
                    "message": f"Using tools: {', '.join(tool_names)}",
                    "code": "chat.trace.using_tools",
                    "params": {"tools": ", ".join(tool_names)},
                }
                messages = self._append_sanitized_assistant_tool_turn(
                    messages,
                    content=getattr(completion_chunk, "content", None),
                    tool_calls=completion_tool_calls,
                )

                last_tool_results = []
                for tool_call in completion_tool_calls:
                    tool_focus = self._tool_focus_from_arguments(
                        tool_name=tool_call.name,
                        arguments=tool_call.arguments,
                    )
                    start_message, start_code, start_params = self._tool_start_trace(
                        tool_call.name
                    )
                    start_trace: dict[str, Any] = {
                        "kind": "tool_call",
                        "round": round_index + 1,
                        "tool": tool_call.name,
                        "status": "start",
                        "message": start_message,
                        "code": start_code,
                    }
                    if start_params is not None:
                        start_trace["params"] = start_params
                    if tool_focus:
                        start_trace.update(tool_focus)
                    yield "trace", start_trace
                    tool_result = await self._tools.execute_tool_call(tool_call)
                    last_tool_results.append(tool_result)
                    resolved_tool_focus = self._tool_focus_from_result(
                        tool_name=tool_call.name,
                        tool_result=tool_result,
                    )
                    (
                        result_message,
                        result_code,
                        result_params,
                    ) = self._tool_result_trace(tool_call.name, tool_result)
                    complete_trace: dict[str, Any] = {
                        "kind": "tool_call",
                        "round": round_index + 1,
                        "tool": tool_call.name,
                        "status": "success" if tool_result.get("ok") else "error",
                        "message": result_message,
                        "code": result_code,
                    }
                    if result_params is not None:
                        complete_trace["params"] = result_params
                    if resolved_tool_focus:
                        complete_trace.update(resolved_tool_focus)
                    yield "trace", complete_trace
                    tool_response_content = json.dumps(tool_result, ensure_ascii=False)
                    messages.append(
                        ToolResponseMessage(
                            id=tool_call.id,
                            content=[TextContentPart(text=tool_response_content)],
                        )
                    )
                if self._record_position_calls(
                    completion_tool_calls, recent_position_signatures
                ):
                    LOGGER.warning(
                        "Repeated position tool call detected; stopping early"
                    )
                    yield "trace", {
                        "kind": "limit",
                        "message": (
                            "Stopped a repeated position update to avoid a loop."
                        ),
                        "code": "chat.trace.tool_loop_guard",
                    }
                    response_text = await self._try_final_response_without_tools(
                        client=client,
                        model=model,
                        messages=messages,
                    )
                    if not response_text:
                        response_text = self._build_tool_limit_fallback(
                            last_tool_results
                        )
                    yield "chunk", response_text
                    break
                continue

            response_text = "".join(round_content_chunks)
            if not response_text and completion_chunk:
                response_text = extract_text(getattr(completion_chunk, "content", None))
            if not response_text:
                response_text = "I could not generate a response for that request."

            if not round_content_chunks:
                yield "chunk", response_text
            break
        else:
            LOGGER.warning("Max tool rounds reached in chat stream flow")
            yield "trace", {
                "kind": "limit",
                "message": (
                    "Reached tool-call limit before final answer; "
                    "attempting best-effort summary."
                ),
                "code": "chat.trace.tool_limit_reached",
            }
            yield "status", ChatStreamStatus(
                message="Finalizing response",
                code="chat.finalizing_response",
            )
            response_text = await self._try_final_response_without_tools(
                client=client,
                model=model,
                messages=messages,
            )
            if not response_text:
                response_text = self._build_tool_limit_fallback(last_tool_results)
            yield "chunk", response_text

        final_response_text = response_text or "I could not generate a response for that request."
        if response_text is None:
            yield "chunk", final_response_text

        yield "status", ChatStreamStatus(
            message="Saving chat",
            code="chat.saving_chat",
        )
        result = await self._persist_turn(
            conversation_id=conversation_id,
            user_message=persisted_user_message,
            response_text=final_response_text,
            tool_calls=called_tools,
        )
        yield "complete", result

    async def _prepare_turn_context(
        self,
        user_message: str,
        attachments: list[ChatAttachment] | None = None,
    ) -> tuple[uuid.UUID, list[Message], str]:
        if not (user_message or "").strip():
            raise HTTPException(status_code=400, detail="Message is required")

        presentation = await self._sql_session.get(PresentationModel, self._presentation_id)
        if not presentation:
            raise HTTPException(status_code=404, detail="Presentation not found")
        attachment_context, attachment_memory = await self._build_attachment_context(
            user_message=user_message,
            attachments=attachments or [],
            presentation_language=getattr(presentation, "language", None),
        )

        conversation_id = await self._conversation_store.ensure_conversation_id(
            self._conversation_id
        )
        history = await self._conversation_store.load_history(
            presentation_id=self._presentation_id,
            conversation_id=conversation_id,
        )
        history_messages = self._convert_history_to_messages(history)

        normalized_user_message = self._strip_ui_context_prefix(user_message)
        attachment_names = " ".join(
            self._attachment_display_name(attachment) for attachment in attachments or []
        )
        memory_query = " ".join(
            part.strip()
            for part in (normalized_user_message or user_message, attachment_names)
            if part and part.strip()
        )
        presentation_memory = await self._memory.retrieve_context(memory_query)
        chat_memory = await self._conversation_store.retrieve_semantic_context(
            presentation_id=self._presentation_id,
            conversation_id=conversation_id,
            query=memory_query,
        )
        if attachment_memory:
            await MEM0_PRESENTATION_MEMORY_SERVICE.store_generation_context(
                presentation_id=self._presentation_id,
                system_prompt=None,
                user_prompt=None,
                extracted_document_text=attachment_memory,
                source_content=None,
                instructions=None,
            )
        model_user_message = self._compose_user_message_for_model(
            user_message,
            attachment_context,
        )
        messages: list[Message] = [
            SystemMessage(
                content=build_system_prompt(
                    presentation_memory_context=presentation_memory,
                    chat_memory_context=chat_memory,
                )
            ),
            *history_messages,
            UserMessage(content=model_user_message),
        ]
        return conversation_id, messages, self._persisted_user_message(
            user_message,
            attachments or [],
        )

    async def _persist_turn(
        self,
        *,
        conversation_id: uuid.UUID,
        user_message: str,
        response_text: str,
        tool_calls: list[str],
    ) -> ChatTurnResult:
        await self._conversation_store.append_turn(
            presentation_id=self._presentation_id,
            conversation_id=conversation_id,
            user_message=self._strip_ui_context_prefix(user_message) or user_message,
            assistant_message=response_text,
            tool_calls=tool_calls,
        )
        await self._sql_session.commit()

        return ChatTurnResult(
            conversation_id=conversation_id,
            response_text=response_text,
            tool_calls=tool_calls,
        )

    async def _run_llm_with_tools(self, messages: list[Message]) -> tuple[str, list[str]]:
        client = get_llm_client()
        model = get_model()
        tools = build_chat_llm_tools(self._tools.get_tool_definitions())

        called_tools: list[str] = []
        last_tool_results: list[dict[str, Any]] = []
        recent_position_signatures: list[str] = []

        for _ in range(MAX_TOOL_ROUNDS):
            completion: Any | None = None
            try:
                async for event in stream_generate_events(
                    client,
                    **get_generate_kwargs(
                        model=model,
                        messages=messages,
                        tools=tools,
                        stream=True,
                    ),
                ):
                    if getattr(event, "type", None) == "completion":
                        completion = event
            except Exception as exc:
                raise handle_llm_client_exceptions(exc)

            tool_calls: list[AssistantToolCall] = list(
                getattr(completion, "tool_calls", []) or []
            )
            if not tool_calls:
                response_text = extract_text(
                    getattr(completion, "content", None)
                ) or ("I could not generate a response for that request.")
                return response_text, called_tools

            called_tools.extend([tool_call.name for tool_call in tool_calls])
            messages = self._append_sanitized_assistant_tool_turn(
                messages,
                content=getattr(completion, "content", None),
                tool_calls=tool_calls,
            )

            last_tool_results = []
            for tool_call in tool_calls:
                tool_result = await self._tools.execute_tool_call(tool_call)
                last_tool_results.append(tool_result)
                tool_response_content = json.dumps(tool_result, ensure_ascii=False)
                messages.append(
                    ToolResponseMessage(
                        id=tool_call.id,
                        content=[TextContentPart(text=tool_response_content)],
                    )
                )

            if self._record_position_calls(tool_calls, recent_position_signatures):
                LOGGER.warning("Repeated position tool call detected; stopping early")
                final_response = await self._try_final_response_without_tools(
                    client=client,
                    model=model,
                    messages=messages,
                )
                if final_response:
                    return final_response, called_tools
                return self._build_tool_limit_fallback(last_tool_results), called_tools

        LOGGER.warning("Max tool rounds reached in chat flow")
        final_response = await self._try_final_response_without_tools(
            client=client,
            model=model,
            messages=messages,
        )
        if final_response:
            return final_response, called_tools

        return self._build_tool_limit_fallback(last_tool_results), called_tools

    async def _try_final_response_without_tools(
        self,
        *,
        client: Any,
        model: str,
        messages: list[Message],
    ) -> str | None:
        try:
            async for event in stream_generate_events(
                client,
                **get_generate_kwargs(
                    model=model,
                    messages=messages,
                    stream=True,
                ),
            ):
                if getattr(event, "type", None) == "completion":
                    return extract_text(getattr(event, "content", None))
        except Exception:
            LOGGER.warning("Final no-tool synthesis call failed", exc_info=True)
            return None
        return None

    @staticmethod
    def _summarize_model_note(chunks: list[str]) -> str:
        text = "".join(chunks).strip()
        if not text or text in {"{}", "[]"}:
            return ""

        compact = " ".join(text.split())
        if compact.lower() in {"start", "end"}:
            return ""
        if len(compact) > 600:
            return f"{compact[:600].rstrip()}..."
        return compact

    @staticmethod
    def _event_text(event: Any) -> str:
        for attr in ("chunk", "delta", "text", "content"):
            value = getattr(event, attr, None)
            if isinstance(value, str):
                return value
        return ""

    @staticmethod
    def _append_sanitized_assistant_tool_turn(
        messages: list[Message],
        *,
        content: Any,
        tool_calls: list[AssistantToolCall],
    ) -> list[Message]:
        response_text = extract_text(content)
        return [
            *messages,
            AssistantMessage(
                content=[response_text] if response_text else None,
                tool_calls=list(tool_calls),
            ),
        ]

    @staticmethod
    def _strip_ui_context_prefix(user_message: str) -> str:
        marker = "\nUser message:"
        if not user_message.startswith("UI context:"):
            return user_message
        marker_index = user_message.find(marker)
        if marker_index == -1:
            return user_message
        return user_message[marker_index + len(marker) :].lstrip()

    @staticmethod
    def _attachment_display_name(attachment: ChatAttachment) -> str:
        name = (attachment.name or "").strip()
        if name:
            return name
        return os.path.basename(attachment.file_path or "").strip() or "attachment"

    @staticmethod
    def _trim_attachment_text(text: str, limit: int) -> str:
        value = (text or "").strip()
        if len(value) <= limit:
            return value
        return f"{value[:limit].rstrip()}\n[Attachment truncated]"

    @classmethod
    def _should_parse_attachments(
        cls,
        user_message: str,
        attachments: list[ChatAttachment],
    ) -> bool:
        if not attachments:
            return False

        user_text = cls._strip_ui_context_prefix(user_message).strip()
        if not user_text:
            return True
        if DOCUMENT_CONTENT_INTENT_PATTERN.search(user_text):
            return True
        if DIRECT_FILE_PLACEMENT_PATTERN.search(user_text):
            return False
        return True

    @classmethod
    def _compose_user_message_for_model(
        cls,
        user_message: str,
        attachment_context: str,
    ) -> str:
        if not attachment_context:
            return user_message

        marker = "\nUser message:"
        if user_message.startswith("UI context:"):
            marker_index = user_message.find(marker)
            if marker_index != -1:
                return (
                    f"{user_message[:marker_index].rstrip()}\n"
                    f"{attachment_context}\n"
                    f"{user_message[marker_index:].lstrip()}"
                )

        return f"{attachment_context}\n\nUser message: {user_message}"

    @classmethod
    def _persisted_user_message(
        cls,
        user_message: str,
        attachments: list[ChatAttachment],
    ) -> str:
        display_message = cls._strip_ui_context_prefix(user_message) or user_message
        if not attachments:
            return display_message

        attachment_list = ", ".join(
            cls._attachment_display_name(attachment) for attachment in attachments
        )
        return f"{display_message}\n\nAttached files: {attachment_list}"

    async def _build_attachment_context(
        self,
        *,
        user_message: str,
        attachments: list[ChatAttachment],
        presentation_language: str | None,
    ) -> tuple[str, str]:
        if not attachments:
            return "", ""

        should_parse = self._should_parse_attachments(user_message, attachments)
        names = [
            f"Document {index + 1}: {self._attachment_display_name(attachment)}"
            for index, attachment in enumerate(attachments)
        ]

        if not should_parse:
            context = "\n".join(
                [
                    (
                        "UI context: the user attached document file(s), but this "
                        "request appears to place or reference the file rather than "
                        "read its contents. Use only the file names unless the user "
                        "asks to read, extract, summarize, or build from them."
                    ),
                    *names,
                ]
            )
            return context, ""

        temp_dir = TEMP_FILE_SERVICE.create_temp_dir(str(uuid.uuid4()))
        loader = DocumentsLoader(
            file_paths=[attachment.file_path for attachment in attachments],
            presentation_language=presentation_language,
        )
        await loader.load_documents(temp_dir=temp_dir)

        context_lines = [
            (
                "UI context: the user attached parsed document(s) to this chat "
                "request. Use this content when the user asks to read, extract, "
                "summarize, or build slide/chart/data content from attachments."
            )
        ]
        memory_lines: list[str] = []
        remaining_chars = MAX_CHAT_ATTACHMENT_CONTEXT_CHARS

        for index, attachment in enumerate(attachments):
            if remaining_chars <= 0:
                context_lines.append("[Additional attachment content omitted]")
                break

            name = self._attachment_display_name(attachment)
            parsed = loader.documents[index] if index < len(loader.documents) else ""
            file_limit = min(MAX_CHAT_ATTACHMENT_FILE_CHARS, remaining_chars)
            trimmed = self._trim_attachment_text(parsed, file_limit)
            context_lines.append(f"Document {index + 1} ({name}):\n{trimmed}")
            memory_lines.append(f"Document {index + 1} ({name}):\n{trimmed}")
            remaining_chars -= len(trimmed)

        return "\n".join(context_lines), "\n\n".join(memory_lines).strip()

    @staticmethod
    def _tool_focus_from_arguments(
        *,
        tool_name: str,
        arguments: str | None,
    ) -> dict[str, Any] | None:
        if tool_name not in {
            "getSlideAtIndex",
            "addNewSlide",
            "addNewSlideLayout",
            "saveSlide",
            "updateSlide",
            "deleteSlide",
            "addElement",
            "updateElement",
            "deleteElement",
            "addComponent",
            "createComponent",
            "updateComponent",
            "deleteComponent",
        }:
            return None

        parsed_args: dict[str, Any]
        try:
            parsed_args = dirtyjson.loads(arguments or "{}")
        except Exception:
            try:
                parsed_args = json.loads(arguments or "{}")
            except Exception:
                return None
        if not isinstance(parsed_args, dict):
            return None

        focus_payload: dict[str, Any] = {}
        index = parsed_args.get("index")
        if isinstance(index, int):
            normalized_index = max(0, index)
            focus_payload["slide_index"] = normalized_index
            focus_payload["slide_number"] = normalized_index + 1

        component_id = parsed_args.get("componentId") or parsed_args.get("component_id")
        if isinstance(component_id, str) and component_id:
            focus_payload["component_id"] = component_id
        element_path = parsed_args.get("elementPath") or parsed_args.get("element_path")
        if isinstance(element_path, str) and element_path:
            focus_payload["element_path"] = element_path

        target_slide_indices = PresentationChatService._extract_target_slide_indices(
            parsed_args
        )
        if target_slide_indices:
            focus_payload["target_slide_indices"] = target_slide_indices
            focus_payload["target_slide_numbers"] = [
                index + 1 for index in target_slide_indices
            ]

        return focus_payload or None

    @staticmethod
    def _tool_focus_from_result(
        *,
        tool_name: str,
        tool_result: dict[str, Any],
    ) -> dict[str, Any] | None:
        if tool_name not in {
            "getSlideAtIndex",
            "addNewSlide",
            "addNewSlideLayout",
            "saveSlide",
            "updateSlide",
            "deleteSlide",
            "addElement",
            "updateElement",
            "deleteElement",
            "addComponent",
            "createComponent",
            "updateComponent",
            "deleteComponent",
        }:
            return None
        if not tool_result.get("ok"):
            return None

        result = tool_result.get("result")
        if not isinstance(result, dict):
            return None

        focus_payload: dict[str, Any] = {}
        index: int | None = None
        resolved_index = result.get("resolved_index")
        if isinstance(resolved_index, int):
            index = resolved_index
        else:
            direct_index = result.get("index")
            if isinstance(direct_index, int):
                index = direct_index
            else:
                slide = result.get("slide")
                if isinstance(slide, dict) and isinstance(slide.get("index"), int):
                    index = slide["index"]

        if index is not None:
            normalized_index = max(0, index)
            focus_payload["slide_index"] = normalized_index
            focus_payload["slide_number"] = normalized_index + 1

        component_id = result.get("component_id")
        if isinstance(component_id, str) and component_id:
            focus_payload["component_id"] = component_id
        element_path = result.get("element_path")
        if isinstance(element_path, str) and element_path:
            focus_payload["element_path"] = element_path

        target_slide_indices = PresentationChatService._extract_target_slide_indices(
            result
        )
        if target_slide_indices:
            focus_payload["target_slide_indices"] = target_slide_indices
            focus_payload["target_slide_numbers"] = [
                index + 1 for index in target_slide_indices
            ]

        return focus_payload or None

    @staticmethod
    def _extract_target_slide_indices(payload: dict[str, Any]) -> list[int]:
        raw_candidates = []
        for key in (
            "target_slide_indices",
            "targetSlideIndices",
            "target_indices",
            "targetIndices",
            "slide_indices",
            "slideIndices",
            "indices",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                raw_candidates.extend(value)

        normalized_indices: list[int] = []
        seen_indices: set[int] = set()
        for candidate in raw_candidates:
            if not isinstance(candidate, int):
                continue
            normalized_index = max(0, candidate)
            if normalized_index in seen_indices:
                continue
            seen_indices.add(normalized_index)
            normalized_indices.append(normalized_index)
        return normalized_indices

    @staticmethod
    def _tool_start_trace(
        tool_name: str,
    ) -> tuple[str, str, dict[str, str] | None]:
        trace = TOOL_START_TRACES.get(tool_name)
        if trace is None:
            return f"Running {tool_name}", "chat.trace.running_tool", {"tool": tool_name}
        return trace[0], trace[1], None

    @staticmethod
    def _build_tool_limit_fallback(last_tool_results: list[dict[str, Any]]) -> str:
        for entry in reversed(last_tool_results):
            if not isinstance(entry, dict):
                continue
            if not entry.get("ok"):
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            message = result.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()

        return (
            "Tôi đã thực hiện các bước căn chỉnh cần thiết nhưng cần thêm thông tin "
            "để hoàn thiện tối ưu. Bạn hãy kiểm tra lại bố cục hiện tại trên slide "
            "và cho tôi biết có cần tinh chỉnh thêm phần nào không nhé."
        )

    @staticmethod
    def _position_call_signature(tool_call: AssistantToolCall) -> str | None:
        if tool_call.name not in POSITION_MUTATION_TOOLS:
            return None
        try:
            arguments = json.loads(tool_call.arguments or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(arguments, dict):
            return None
        if arguments.get("position") is None and arguments.get("size") is None:
            return None
        target = arguments.get("componentId") or arguments.get("elementPath") or ""
        payload = json.dumps(
            {
                "position": arguments.get("position"),
                "size": arguments.get("size"),
                "action": arguments.get("action"),
            },
            sort_keys=True,
            default=str,
        )
        return f"{tool_call.name}|{arguments.get('index')}|{target}|{payload}"

    @classmethod
    def _record_position_calls(
        cls,
        tool_calls: list[AssistantToolCall],
        recent_signatures: list[str],
    ) -> bool:
        """Return True when the same position mutation repeats too many times."""
        signatures = [
            signature
            for tool_call in tool_calls
            if (signature := cls._position_call_signature(tool_call)) is not None
        ]
        if not signatures:
            recent_signatures.clear()
            return False
        recent_signatures.extend(signatures)
        if len(recent_signatures) > MAX_IDENTICAL_POSITION_CALLS:
            del recent_signatures[:-MAX_IDENTICAL_POSITION_CALLS]
        if len(recent_signatures) < MAX_IDENTICAL_POSITION_CALLS:
            return False
        return len(set(recent_signatures)) == 1

    @staticmethod
    def _tool_result_trace(
        tool_name: str, tool_result: dict[str, Any]
    ) -> tuple[str, str, dict[str, Any] | None]:
        """Summarize a tool result into (message, code, params).

        The message text stays verbatim for legacy clients; the code lets
        localized clients map the trace without matching English prose.
        """
        if not tool_result.get("ok"):
            error = tool_result.get("error")
            if isinstance(error, str) and error.strip():
                params: dict[str, Any] = {"tool": tool_name, "reason": error.strip()}
                recovery = tool_result.get("recovery")
                if isinstance(recovery, dict):
                    guidance = recovery.get("guidance")
                    if isinstance(guidance, list) and guidance:
                        first_guidance = str(guidance[0]).strip()
                        if first_guidance:
                            params["recovery"] = first_guidance
                            return (
                                f"{tool_name} failed: {error.strip()} "
                                f"Recovery: {first_guidance}",
                                "chat.trace.tool_failed",
                                params,
                            )
                return (
                    f"{tool_name} failed: {error.strip()}",
                    "chat.trace.tool_failed",
                    params,
                )
            return (
                f"{tool_name} failed.",
                "chat.trace.tool_failed",
                {"tool": tool_name},
            )

        result = tool_result.get("result")
        if isinstance(result, dict):
            message = result.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip(), "chat.trace.tool_result", {"tool": tool_name}

            note = result.get("note")
            if isinstance(note, str) and note.strip():
                return note.strip(), "chat.trace.tool_result", {"tool": tool_name}

            count = result.get("count")
            if isinstance(count, int):
                return (
                    f"{tool_name} returned {count} result(s).",
                    "chat.trace.tool_returned_results",
                    {"tool": tool_name, "count": count},
                )

            found = result.get("found")
            if isinstance(found, bool):
                if found:
                    return (
                        f"{tool_name} found requested data.",
                        "chat.trace.found_requested_data",
                        {"tool": tool_name},
                    )
                return (
                    f"{tool_name} did not find matching data.",
                    "chat.trace.no_matching_data",
                    {"tool": tool_name},
                )

        return (
            f"{tool_name} completed.",
            "chat.trace.tool_completed",
            {"tool": tool_name},
        )

    @staticmethod
    def _convert_history_to_messages(history: list[dict[str, str]]) -> list[Message]:
        messages: list[Message] = []
        for item in history:
            role = item.get("role")
            content = item.get("content")
            if not content:
                continue
            if role == "user":
                content = PresentationChatService._strip_ui_context_prefix(content)
                messages.append(UserMessage(content=content))
            elif role == "assistant":
                messages.append(AssistantMessage(content=[content]))
        return messages
