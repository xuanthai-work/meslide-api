import json
import logging
import re
from typing import Any, Awaitable, Callable

import dirtyjson  # type: ignore[import-untyped]
from utils.llm_messages import AssistantToolCall, Tool

from services.chat.schemas import (
    AddElementInput,
    AddNewSlideInput,
    AddNewSlideLayoutInput,
    AddSlideComponentInput,
    DeleteSlideComponentInput,
    DeleteSlideElementInput,
    DeleteSlideInput,
    GenerateAssetsInput,
    GetAvailableBlocksInput,
    GetContentSchemaFromLayoutIdInput,
    GetSlideAtIndexInput,
    NoArgsInput,
    ReadSourceDocumentsInput,
    SaveSlideInput,
    SearchSlidesInput,
    SetPresentationThemeInput,
    UpdateComponentInput,
    UpdateSlideInput,
    UpdateSlideComponentInput,
    UpdateSlideElementInput,
)
from services.chat.presentation_context_store import PresentationContextStore
from services.chat.tools_repair import (
    CHART_INSERT_TOOL_FIELDS,
    ChatToolsRepairMixin,
    JSON_OBJECT_STRING_FIELDS,
)

LOGGER = logging.getLogger(__name__)

ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
BLOCK_PRIORITIZED_INSERT_TYPES = {"chart", "table"}
MAX_TOOL_REPAIR_RETRIES = 1


def _normalize_tool_argument_value(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() == "null":
        return None
    if isinstance(value, list):
        return [_normalize_tool_argument_value(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _normalize_tool_argument_value(item)
            for key, item in value.items()
        }
    return value


class ChatTools(ChatToolsRepairMixin):
    def __init__(
        self,
        memory: PresentationContextStore,
    ):
        self._memory = memory
        self._turn_user_message = ""
        self._generated_assets: list[dict[str, Any]] = []
        self._tool_handlers: dict[str, ToolHandler] = {
            "addNewSlide": self._add_new_slide,
            "addNewSlideLayout": self._add_new_slide_layout,
            "getTemplateSummary": self._get_template_summary,
            "readSourceDocuments": self._read_source_documents,
            "searchSlide": self._search_slides,
            "getSlideAtIndex": self._get_slide_at_index,
            "getAvailableLayouts": self._get_available_layouts,
            "getAvailableBlocks": self._get_available_blocks,
            "getContentSchemaFromLayoutId": self._get_content_schema_from_layout_id,
            "generateAssets": self._generate_assets,
            "saveSlide": self._save_slide,
            "updateSlide": self._update_slide,
            "deleteSlide": self._delete_slide,
            "addElement": self._add_element,
            "updateElement": self._update_slide_element,
            "deleteElement": self._delete_slide_element,
            "addComponent": self._add_slide_component,
            "createComponent": self._add_slide_component,
            "updateComponent": self._update_component,
            "deleteComponent": self._delete_slide_component,
            "getPresentationTheme": self._get_presentation_theme_catalog,
            "setPresentationTheme": self._set_presentation_theme,
        }

    def set_turn_context(self, user_message: str) -> None:
        self._turn_user_message = user_message or ""
        self._generated_assets = []

    def get_tool_definitions(self) -> list[Tool]:
        return [
            Tool(
                name="addNewSlide",
                description=(
                    "Add a blank slide to the current presentation at a zero-based index "
                    "or append when index is null."
                ),
                schema=AddNewSlideInput,
                strict=False,
            ),
            Tool(
                name="addNewSlideLayout",
                description=(
                    "Add a new slide from an available layout. Use getAvailableLayouts "
                    "first, then pass content as a JSON-serialized object matching the layout."
                ),
                schema=AddNewSlideLayoutInput,
                strict=False,
            ),
            Tool(
                name="getAvailableLayouts",
                description="List available slide layout ids, names, and summaries.",
                schema=NoArgsInput,
                strict=False,
            ),
            Tool(
                name="getAvailableBlocks",
                description=(
                    "Search reusable template component blocks without fetching a whole layout. "
                    "Use this before addComponent/createComponent when the user asks to add "
                    "a styled block such as a title/header/subtitle, table, chart, card, "
                    "callout, metric, image panel, or repeated content item. For title "
                    "blocks use elementType=text with title/header/heading query terms. "
                    "Set includeFullContent=true only when exact component JSON is needed."
                ),
                schema=GetAvailableBlocksInput,
                strict=False,
            ),
            Tool(
                name="getContentSchemaFromLayoutId",
                description=(
                    "Return the exact JSON content schema for one layout id. "
                    "Use this before addNewSlideLayout or updateSlide when composing "
                    "a full slide content payload."
                ),
                schema=GetContentSchemaFromLayoutIdInput,
                strict=False,
            ),
            Tool(
                name="getTemplateSummary",
                description=(
                    "Read a compact summary of the current presentation template, "
                    "layouts, current slides, and theme. Use before choosing where/how to edit."
                ),
                schema=NoArgsInput,
                strict=False,
            ),
            Tool(
                name="readSourceDocuments",
                description=(
                    "Read parsed text from the source document(s) uploaded for this "
                    "presentation, including PDFs. Use when the user refers to an "
                    "uploaded/source PDF, document, file, or the document used to "
                    "generate the deck. Use before summarizing, quoting, extracting "
                    "data, or creating slide content from uploaded documents."
                ),
                schema=ReadSourceDocumentsInput,
                strict=False,
            ),
            Tool(
                name="searchSlide",
                description=(
                    "Search current slides for text/topics and return slide indices and snippets."
                ),
                schema=SearchSlidesInput,
                strict=False,
            ),
            Tool(
                name="getSlideAtIndex",
                description=(
                    "Live SQL: one slide by index—authoritative for exact current content. "
                    "Set includeFullContent=true when you need full JSON (before saveSlide or precise edits). "
                    "If user says slide N, use zero-based index N-1."
                ),
                schema=GetSlideAtIndexInput,
                strict=False,
            ),
            Tool(
                name="saveSlide",
                description=(
                    "Save full slide content for a layout. Use for complete slide payloads; "
                    "visible element/component edits should use element/component tools."
                ),
                schema=SaveSlideInput,
                strict=False,
            ),
            Tool(
                name="updateSlide",
                description="Replace an existing slide's layout/content by zero-based index.",
                schema=UpdateSlideInput,
                strict=False,
            ),
            Tool(
                name="deleteSlide",
                description="Delete an existing slide by zero-based index and reindex the rest.",
                schema=DeleteSlideInput,
                strict=False,
            ),
            Tool(
                name="addElement",
                description=(
                    "Add one rendered UI element to a slide, either inside a componentId "
                    "or as a new free component when componentId is null. Do not use this "
                    "for new table/chart requests when a reusable block exists; use "
                    "getAvailableBlocks and addComponent/createComponent instead. Chart "
                    "elements must include numeric data as categories plus series.values, "
                    "or legacy data rows with label/value. Image elements must include "
                    "data set to a URL returned by generateAssets. Geometry must use the "
                    "current vector type; line/rectangle/ellipse/circle/polygon element "
                    "types were removed. Infographics use nested data with type, "
                    "min_value, max_value, and value."
                ),
                schema=AddElementInput,
                strict=False,
            ),
            Tool(
                name="updateElement",
                description=(
                    "Update visible element content or geometry using an elementPath returned "
                    "by getSlideAtIndex. Supports text, lists, table, chart, vector, "
                    "infographic, image data, position, size, and toolbar-style font, color, fill, stroke, "
                    "alignment, opacity, and element property patches. For "
                    "charts, use the chart field for chartType, categories, "
                    "series.values, colors, axes, dataLabels placement, and legend. "
                    "Use vector for vector points/shape/curve and infographic for "
                    "nested data and colors."
                ),
                schema=UpdateSlideElementInput,
                strict=False,
            ),
            Tool(
                name="deleteElement",
                description="Delete one rendered UI element by elementPath.",
                schema=DeleteSlideElementInput,
                strict=False,
            ),
            Tool(
                name="addComponent",
                description=(
                    "Add an existing/new rendered UI component block to a slide. Component "
                    "JSON must include id, description, position, and elements; do not include "
                    "component size because bounds are inferred from child element bounds. "
                    "Chart elements must include numeric data as categories plus series.values, "
                    "or legacy data rows with label/value. Image elements must include data "
                    "set to a URL returned by generateAssets. For styled title/header, "
                    "table/chart, card, metric, or callout additions, adapt a component "
                    "returned by getAvailableBlocks and pass sourceBlockId. Use type=vector "
                    "for lines and shapes, and nested data for infographics."
                ),
                schema=AddSlideComponentInput,
                strict=False,
            ),
            Tool(
                name="createComponent",
                description=(
                    "Create a grouped rendered UI component from provided component JSON "
                    "and add it to a slide. Chart elements must include numeric data as "
                    "categories plus series.values, or legacy data rows with label/value. "
                    "Image elements must include data set to a URL returned by generateAssets. "
                    "For styled title/header, table/chart, card, metric, or callout additions "
                    "adapt a component returned by getAvailableBlocks and pass sourceBlockId. "
                    "Use type=vector for lines and shapes, and nested data for infographics."
                ),
                schema=AddSlideComponentInput,
                strict=False,
            ),
            Tool(
                name="updateComponent",
                description=(
                    "Move, resize, replace, duplicate, reorder, group, or ungroup rendered "
                    "UI components by componentId."
                ),
                schema=UpdateComponentInput,
                strict=False,
            ),
            Tool(
                name="deleteComponent",
                description=(
                    "Remove one whole component (a block such as a numbered point, card, "
                    "or callout) from a rendered slide by componentId."
                ),
                schema=DeleteSlideComponentInput,
                strict=False,
            ),
            Tool(
                name="getPresentationTheme",
                description="Read the current presentation theme and available themes.",
                schema=NoArgsInput,
                strict=False,
            ),
            Tool(
                name="setPresentationTheme",
                description=(
                    "Change the deck theme by theme name/id/query or customTheme payload."
                ),
                schema=SetPresentationThemeInput,
                strict=False,
            ),
            Tool(
                name="generateAssets",
                description="Generate one or more image/icon assets for slide edits.",
                schema=GenerateAssetsInput,
                strict=False,
            ),
        ]

    async def execute_tool_call(self, tool_call: AssistantToolCall) -> dict[str, Any]:
        handler = self._tool_handlers.get(tool_call.name)
        if not handler:
            return {
                "ok": False,
                "tool": tool_call.name,
                "error": f"Unsupported tool: {tool_call.name}",
                "recovery": {
                    "retryable": False,
                    "message": "Use one of the available chat tools.",
                    "guidance": ["Choose a tool from the tool definitions."],
                },
            }

        parsed_args: dict[str, Any] | None = None
        repair_notes: list[str] = []
        try:
            parsed_args = self._parse_args(tool_call.arguments)
            parsed_args, repair_notes = self._repair_tool_args(
                tool_call.name,
                parsed_args,
            )
            LOGGER.info("Executing chat tool %s", tool_call.name)
            try:
                result = await handler(parsed_args)
            except Exception as first_exc:
                retried = False
                for _attempt in range(MAX_TOOL_REPAIR_RETRIES):
                    retry_args, retry_notes = self._repair_tool_args(
                        tool_call.name,
                        parsed_args,
                        error=str(first_exc),
                    )
                    if not retry_notes or self._args_equivalent(parsed_args, retry_args):
                        break
                    repair_notes.extend(retry_notes)
                    parsed_args = retry_args
                    LOGGER.info(
                        "Retrying chat tool %s after argument repair",
                        tool_call.name,
                    )
                    result = await handler(parsed_args)
                    retried = True
                    break
                if not retried:
                    raise first_exc

            if tool_call.name == "generateAssets":
                self._remember_generated_assets(result)

            response = {"ok": True, "tool": tool_call.name, "result": result}
            if repair_notes:
                response["repair"] = {
                    "applied": True,
                    "notes": repair_notes,
                }
            return response
        except Exception as exc:
            LOGGER.exception("Chat tool failed: %s", tool_call.name)
            return {
                "ok": False,
                "tool": tool_call.name,
                "error": str(exc),
                "repair": {
                    "attempted": bool(repair_notes),
                    "notes": repair_notes,
                },
                "recovery": self._build_tool_recovery(
                    tool_name=tool_call.name,
                    args=parsed_args,
                    error=str(exc),
                ),
            }

    async def _get_presentation_outline(self, _: dict[str, Any]) -> dict[str, Any]:
        outline = await self._memory.get("presentation_outline")
        if not isinstance(outline, dict):
            return {
                "found": False,
                "message": "Presentation outline is not available in memory yet.",
                "sections": [],
            }

        slides = outline.get("slides")
        if not isinstance(slides, list) or not slides:
            return {
                "found": False,
                "message": "Presentation outline exists but has no slides.",
                "sections": [],
            }

        sections: list[dict[str, Any]] = []
        for position, slide in enumerate(slides):
            index = position
            content = ""
            if isinstance(slide, dict):
                raw_index = slide.get("index")
                if isinstance(raw_index, int):
                    index = raw_index
                raw_content = slide.get("content")
                if isinstance(raw_content, str):
                    content = raw_content
                elif raw_content is not None:
                    try:
                        content = json.dumps(raw_content, ensure_ascii=False)
                    except Exception:
                        content = str(raw_content)
            elif isinstance(slide, str):
                content = slide

            title = self._extract_title(content) or f"Slide {index + 1}"
            sections.append(
                {
                    "index": index,
                    "slide_number": index + 1,
                    "title": title,
                }
            )

        return {
            "found": True,
            "slide_count": len(sections),
            "sections": sections,
            "source": outline.get("source", "memory"),
        }

    async def _search_slides(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = SearchSlidesInput(**args)
        results = await self._memory.search(payload.query, payload.limit)
        return {
            "query": payload.query,
            "count": len(results),
            "results": results,
        }

    async def _get_slide_at_index(self, args: dict[str, Any]) -> dict[str, Any]:
        normalized_args = dict(args)
        normalized_args.setdefault("includeFullContent", False)
        payload = GetSlideAtIndexInput(**normalized_args)
        slide = await self._memory.get_slide_at_index(
            payload.index,
            include_full_content=payload.include_full_content,
        )
        if not slide and payload.index > 0:
            # Users often refer to slides as 1-based; allow a safe fallback.
            fallback_index = payload.index - 1
            fallback_slide = await self._memory.get_slide_at_index(
                fallback_index,
                include_full_content=payload.include_full_content,
            )
            if fallback_slide:
                return {
                    "found": True,
                    "slide": fallback_slide,
                    "requested_index": payload.index,
                    "resolved_index": fallback_index,
                    "note": (
                        "No slide found at requested index; returned one-based fallback "
                        f"at index {fallback_index}."
                    ),
                }
        if not slide:
            return {
                "found": False,
                "message": f"No slide found at index {payload.index}.",
            }
        return {
            "found": True,
            "slide": slide,
        }

    async def _add_new_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = AddNewSlideInput(**args)
        return await self._memory.add_blank_slide(index=payload.index)

    async def _add_new_slide_layout(self, args: dict[str, Any]) -> dict[str, Any]:
        payload_args = json.loads(json.dumps(dict(args), ensure_ascii=False))
        raw_content = payload_args.get("content")
        if isinstance(raw_content, dict):
            payload_args["content"] = json.dumps(raw_content, ensure_ascii=False)
        payload = AddNewSlideLayoutInput(**payload_args)
        return await self._save_slide(
            {
                "content": payload.content,
                "layoutId": payload.layout_id,
                "index": payload.index,
                "replaceOldSlideAtIndex": False,
            }
        )

    async def _update_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        payload_args = json.loads(json.dumps(dict(args), ensure_ascii=False))
        raw_content = payload_args.get("content")
        if isinstance(raw_content, dict):
            payload_args["content"] = json.dumps(raw_content, ensure_ascii=False)
        payload = UpdateSlideInput(**payload_args)
        return await self._save_slide(
            {
                "content": payload.content,
                "layoutId": payload.layout_id,
                "index": payload.index,
                "replaceOldSlideAtIndex": True,
            }
        )

    async def _get_available_layouts(self, _: dict[str, Any]) -> dict[str, Any]:
        layouts = await self._memory.get_available_layouts()
        return {
            "count": len(layouts),
            "layouts": layouts,
        }

    async def _get_available_blocks(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = GetAvailableBlocksInput(**args)
        max_results = payload.max_results if payload.max_results is not None else 20
        return await self._memory.get_available_blocks(
            query=payload.query,
            layout_id=payload.layout_id,
            element_type=payload.element_type,
            block_id=payload.block_id,
            include_full_content=bool(payload.include_full_content),
            max_results=max_results,
        )

    async def _get_template_summary(self, _: dict[str, Any]) -> dict[str, Any]:
        outline = await self._get_presentation_outline({})
        layouts = await self._get_available_layouts({})
        theme = await self._get_presentation_theme_catalog({})
        return {
            "outline": outline,
            "available_layouts": layouts,
            "theme": theme,
            "message": "Template summary fetched successfully.",
        }

    async def _read_source_documents(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = ReadSourceDocumentsInput(**args)
        return await self._memory.read_source_documents(
            query=payload.query,
            max_chars=payload.max_chars,
        )

    async def _get_presentation_theme_catalog(
        self, _: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._memory.get_presentation_theme_catalog()

    async def _get_content_schema_from_layout_id(
        self, args: dict[str, Any]
    ) -> dict[str, Any]:
        payload = GetContentSchemaFromLayoutIdInput(**args)
        schema = await self._memory.get_content_schema_from_layout_id(payload.layout_id)
        if schema is None:
            return {
                "found": False,
                "layout_id": payload.layout_id,
                "message": "Layout schema not found for the provided layout id.",
            }
        return {
            "found": True,
            "layout_id": payload.layout_id,
            "content_schema": schema,
        }

    async def _generate_assets(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = GenerateAssetsInput(**args)
        generated_assets: list[dict[str, Any]] = []

        for index, asset in enumerate(payload.assets):
            if asset.kind == "image":
                url = await self._memory.generate_image(asset.prompt)
            else:
                url = await self._memory.generate_icon(asset.prompt)

            generated_assets.append(
                {
                    "index": index,
                    "kind": asset.kind,
                    "prompt": asset.prompt,
                    "url": url,
                }
            )

        return {
            "count": len(generated_assets),
            "assets": generated_assets,
            "message": f"Generated {len(generated_assets)} asset(s).",
        }

    async def _save_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        payload_args = json.loads(json.dumps(dict(args), ensure_ascii=False))
        raw_content = payload_args.get("content")
        if isinstance(raw_content, dict):
            payload_args["content"] = json.dumps(raw_content, ensure_ascii=False)

        payload = SaveSlideInput(**payload_args)
        try:
            content_parsed: Any = dirtyjson.loads(payload.content)
        except Exception:
            content_parsed = json.loads(payload.content)

        if not isinstance(content_parsed, dict):
            raise ValueError("'content' must be a JSON object.")

        content_payload = json.loads(json.dumps(content_parsed, ensure_ascii=False))
        return await self._memory.save_slide(
            content=content_payload,
            layout_id=payload.layout_id,
            index=payload.index,
            replace_old_slide_at_index=payload.replace_old_slide_at_index,
        )

    async def _delete_slide(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = DeleteSlideInput(**args)
        return await self._memory.delete_slide(index=payload.index)

    async def _get_slide_elements(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = GetSlideAtIndexInput(
            index=int(args.get("index") or 0),
            includeFullContent=bool(args.get("includeFullJson")),
        )
        return await self._memory.get_slide_ui_elements(
            index=payload.index,
            include_full_json=payload.include_full_content,
        )

    async def _add_element(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = AddElementInput(**args)
        try:
            parsed: Any = dirtyjson.loads(payload.element)
        except Exception:
            parsed = json.loads(payload.element)
        element = json.loads(json.dumps(parsed, ensure_ascii=False))
        if not isinstance(element, dict):
            raise ValueError("'element' must be a JSON object.")
        await self._require_reusable_block_first(
            tree=element,
            source_block_id=None,
            primitive_tool="addElement",
        )
        return await self._memory.add_slide_ui_element(
            index=payload.index,
            element=element,
            component_id=payload.component_id,
            insert_index=payload.insert_index,
        )

    async def _update_slide_element(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = UpdateSlideElementInput(**args)
        element_patch: dict[str, Any] | None = None
        if payload.element is not None:
            try:
                parsed: Any = dirtyjson.loads(payload.element)
            except Exception:
                parsed = json.loads(payload.element)
            element_patch = json.loads(json.dumps(parsed, ensure_ascii=False))
            if not isinstance(element_patch, dict):
                raise ValueError("'element' must be a JSON object.")
        style_patch = self._element_style_patch_from_update_payload(payload)
        if style_patch:
            element_patch = self._merge_dict_patch(element_patch or {}, style_patch)
        return await self._memory.update_slide_ui_element(
            index=payload.index,
            element_path=payload.element_path,
            text=payload.text,
            items=payload.items,
            table_cell=(
                payload.table_cell.model_dump(by_alias=False)
                if payload.table_cell is not None
                else None
            ),
            table=(
                payload.table.model_dump()
                if payload.table is not None
                else None
            ),
            chart=(
                payload.chart.model_dump(exclude_none=True)
                if payload.chart is not None
                else None
            ),
            vector=(
                payload.vector.model_dump(exclude_none=True)
                if payload.vector is not None
                else None
            ),
            infographic=(
                payload.infographic.model_dump(exclude_none=True)
                if payload.infographic is not None
                else None
            ),
            element_patch=element_patch,
            position=(
                payload.position.model_dump()
                if payload.position is not None
                else None
            ),
            size=payload.size.model_dump() if payload.size is not None else None,
        )

    async def _update_slide_component(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = UpdateSlideComponentInput(**args)
        return await self._memory.update_slide_ui_component(
            index=payload.index,
            component_id=payload.component_id,
            position=(
                payload.position.model_dump()
                if payload.position is not None
                else None
            ),
            size=payload.size.model_dump() if payload.size is not None else None,
        )

    async def _update_component(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = UpdateComponentInput(**args)
        replacement_component: dict[str, Any] | None = None
        if payload.component is not None:
            try:
                parsed: Any = dirtyjson.loads(payload.component)
            except Exception:
                parsed = json.loads(payload.component)
            replacement_component = json.loads(json.dumps(parsed, ensure_ascii=False))
            if not isinstance(replacement_component, dict):
                raise ValueError("'component' must be a JSON object.")
        return await self._memory.update_slide_ui_component(
            index=payload.index,
            component_id=payload.component_id,
            action=payload.action or "update",
            component_ids=payload.component_ids,
            position=(
                payload.position.model_dump()
                if payload.position is not None
                else None
            ),
            size=payload.size.model_dump() if payload.size is not None else None,
            replacement_component=replacement_component,
        )

    async def _delete_slide_component(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = DeleteSlideComponentInput(**args)
        return await self._memory.delete_slide_ui_component(
            index=payload.index,
            component_id=payload.component_id,
        )

    async def _delete_slide_element(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = DeleteSlideElementInput(**args)
        return await self._memory.delete_slide_ui_element(
            index=payload.index,
            element_path=payload.element_path,
        )

    async def _add_slide_component(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = AddSlideComponentInput(**args)
        try:
            component_parsed: Any = dirtyjson.loads(payload.component)
        except Exception:
            component_parsed = json.loads(payload.component)
        if not isinstance(component_parsed, dict):
            raise ValueError("'component' must be a JSON object.")
        component_payload = json.loads(json.dumps(component_parsed, ensure_ascii=False))
        await self._require_reusable_block_first(
            tree=component_payload,
            source_block_id=payload.source_block_id,
            primitive_tool="addComponent",
        )
        return await self._memory.add_slide_ui_component(
            index=payload.index,
            component=component_payload,
            insert_index=payload.insert_index,
        )

    async def _set_presentation_theme(self, args: dict[str, Any]) -> dict[str, Any]:
        payload = SetPresentationThemeInput(**args)
        return await self._memory.set_presentation_theme(
            theme_query=payload.theme,
            custom_theme=(
                payload.custom_theme.model_dump(exclude_none=True)
                if payload.custom_theme is not None
                else None
            ),
            save_custom_theme=bool(payload.save_custom_theme),
        )

    @staticmethod
    def _parse_args(arguments: str | None) -> dict[str, Any]:
        if not arguments:
            return {}

        parsed = ChatTools._loads_jsonish(arguments)

        normalized = _normalize_tool_argument_value(
            json.loads(json.dumps(parsed, ensure_ascii=False))
        )
        if isinstance(normalized, dict):
            return normalized

        raise ValueError("Tool arguments must be a JSON object.")

    async def _require_reusable_block_first(
        self,
        *,
        tree: dict[str, Any],
        source_block_id: str | None,
        primitive_tool: str,
    ) -> None:
        requested_types = self._block_prioritized_element_types(tree)
        if not requested_types:
            return

        if source_block_id:
            block_result = await self._memory.get_available_blocks(
                block_id=source_block_id,
                include_full_content=False,
                max_results=1,
            )
            blocks = (
                block_result.get("blocks")
                if isinstance(block_result, dict)
                else None
            )
            block = blocks[0] if isinstance(blocks, list) and blocks else None
            if not isinstance(block, dict):
                raise ValueError(
                    "sourceBlockId was provided but no matching reusable block was found. "
                    "Call getAvailableBlocks again and use a returned block_id."
                )
            block_types = {
                str(item).lower()
                for item in block.get("element_types", [])
                if item is not None
            }
            if requested_types.isdisjoint(block_types):
                raise ValueError(
                    "sourceBlockId does not match the table/chart type being inserted. "
                    "Use a block_id whose element_types include the requested type."
                )
            return

        reusable = await self._first_available_reusable_block(requested_types)
        if reusable is None:
            return

        element_type, block = reusable
        block_id = str(block.get("block_id") or "")
        component_id = str(block.get("component_id") or "")
        layout_id = str(block.get("layout_id") or "")
        raise ValueError(
            f"Reusable block available for {element_type} insertion "
            f"(block_id='{block_id}', component_id='{component_id}', layout_id='{layout_id}'). "
            f"Do not use {primitive_tool} to create this as a primitive. "
            "Call getAvailableBlocks with that blockId and includeFullContent=true, "
            "adapt the returned component JSON with the requested content, then call "
            f"addComponent/createComponent with sourceBlockId='{block_id}'."
        )

    async def _first_available_reusable_block(
        self,
        requested_types: set[str],
    ) -> tuple[str, dict[str, Any]] | None:
        for element_type in sorted(requested_types):
            block_result = await self._memory.get_available_blocks(
                element_type=element_type,
                include_full_content=False,
                max_results=1,
            )
            blocks = (
                block_result.get("blocks")
                if isinstance(block_result, dict)
                else None
            )
            block = blocks[0] if isinstance(blocks, list) and blocks else None
            if isinstance(block, dict):
                return element_type, block
        return None

    @staticmethod
    def _block_prioritized_element_types(tree: Any) -> set[str]:
        found: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, dict):
                element_type = value.get("type")
                if isinstance(element_type, str):
                    normalized = element_type.strip().lower()
                    if normalized in BLOCK_PRIORITIZED_INSERT_TYPES:
                        found.add(normalized)
                for nested in value.values():
                    visit(nested)
            elif isinstance(value, list):
                for nested in value:
                    visit(nested)

        visit(tree)
        return found

    @staticmethod
    def _element_style_patch_from_update_payload(
        payload: UpdateSlideElementInput,
    ) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if payload.font is not None:
            font = payload.font.model_dump(exclude_none=True)
            if font:
                patch["font"] = font
        if payload.alignment is not None:
            alignment = payload.alignment.model_dump(exclude_none=True)
            if alignment:
                patch["alignment"] = alignment
        if payload.fill is not None:
            fill = payload.fill.model_dump(exclude_none=True)
            if fill:
                patch["fill"] = fill
        if payload.stroke is not None:
            stroke = payload.stroke.model_dump(exclude_none=True)
            if stroke:
                patch["stroke"] = stroke
        if payload.color is not None:
            patch["color"] = payload.color
        if payload.opacity is not None:
            patch["opacity"] = payload.opacity
        return patch

    @staticmethod
    def _merge_dict_patch(
        target: dict[str, Any],
        patch: dict[str, Any],
    ) -> dict[str, Any]:
        merged = json.loads(json.dumps(target, ensure_ascii=False))
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = ChatTools._merge_dict_patch(merged[key], value)
            else:
                merged[key] = json.loads(json.dumps(value, ensure_ascii=False))
        return merged


    @staticmethod
    def _args_equivalent(left: dict[str, Any], right: dict[str, Any]) -> bool:
        return json.dumps(
            left,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        ) == json.dumps(
            right,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def _build_tool_recovery(
        cls,
        *,
        tool_name: str,
        args: dict[str, Any] | None,
        error: str,
    ) -> dict[str, Any]:
        normalized_error = (error or "").lower()
        guidance: list[str] = []
        expected: dict[str, Any] = {}
        retryable = True

        if (
            tool_name not in CHART_INSERT_TOOL_FIELDS
            and tool_name not in JSON_OBJECT_STRING_FIELDS
        ):
            guidance.append("Review the tool schema and retry with corrected arguments.")

        if "reusable block available" in normalized_error:
            guidance.append(
                "Use getAvailableBlocks with includeFullContent=true for the "
                "returned block_id, adapt that component JSON, then retry with "
                "addComponent/createComponent and sourceBlockId."
            )
            expected["block_workflow"] = {
                "discovery": {
                    "tool": "getAvailableBlocks",
                    "arguments": {
                        "blockId": "returned block_id",
                        "includeFullContent": True,
                    },
                },
                "insert": {
                    "tool": "addComponent",
                    "arguments": {
                        "component": "JSON string adapted from returned block.component",
                        "sourceBlockId": "returned block_id",
                    },
                },
            }

        json_fields = JSON_OBJECT_STRING_FIELDS.get(tool_name, ())
        if json_fields:
            fields = ", ".join(json_fields)
            guidance.append(
                f"Ensure {fields} is a JSON-serialized object string, not prose."
            )
            expected["json_object_string_fields"] = list(json_fields)

        if "chart elements must include numeric data" in normalized_error:
            guidance.append(
                "For chart elements include categories and series with numeric values before retrying."
            )
            expected["chart"] = {
                "type": "chart",
                "chart_type": "bar",
                "categories": ["Label A", "Label B"],
                "series": [{"name": "Series name", "values": [1, 2]}],
            }

        if "table elements must include" in normalized_error:
            guidance.append(
                "For table elements include columns or headers plus rows before retrying."
            )
            expected["table"] = {
                "type": "table",
                "columns": ["Name", "Age", "Department"],
                "rows": [["Ghanshyam", "30", "QA"], ["Sudeep", "33", "AI"]],
            }

        if "image elements must include" in normalized_error:
            guidance.append(
                "For image elements call generateAssets first, then include the returned URL as data before retrying."
            )
            expected["image"] = {
                "type": "image",
                "data": "/app_data/images/generated.png",
                "is_icon": False,
            }

        if "validation error" in normalized_error:
            guidance.append(
                "Use the field names and aliases from the tool schema, include required nullable fields as null, and remove unsupported keys."
            )

        if "json" in normalized_error or "expecting value" in normalized_error:
            guidance.append(
                "Return valid JSON only for tool arguments; avoid Markdown fences in tool-call arguments."
            )

        if "no slide found" in normalized_error or "was not found" in normalized_error:
            guidance.append(
                "Inspect the deck with getSlideAtIndex or searchSlide and retry with the returned slide index, componentId, or elementPath."
            )

        if "unsupported tool" in normalized_error:
            retryable = False
            guidance = ["Choose one of the available tool names from the current tool definitions."]

        if not guidance:
            guidance.append("Fix the arguments based on the error and retry once.")

        recovery: dict[str, Any] = {
            "retryable": retryable,
            "message": "Repair the tool arguments before retrying." if retryable else "Do not retry this exact tool call.",
            "guidance": guidance,
        }
        if expected:
            recovery["expected"] = expected
        if args is not None:
            recovery["received_keys"] = sorted(str(key) for key in args.keys())
        return recovery

    @staticmethod
    def _extract_title(markdown_content: str) -> str:
        for line in markdown_content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            heading_match = re.match(r"^#{1,6}\s*(.+?)\s*$", stripped)
            if heading_match:
                return heading_match.group(1).strip()
            return stripped[:120]
        return ""

    @staticmethod
    def _truncate(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return f"{value[:limit]}..."
