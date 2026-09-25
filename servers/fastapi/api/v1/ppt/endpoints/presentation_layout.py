"""Presentation layout resolution and generation-progress helpers.

Extracted from ``api.v1.ppt.endpoints.presentation`` so generation, prepare,
and stream flows share one leaf module instead of living on the router.
The router re-exports these names for existing tests.
"""

from __future__ import annotations

import copy
import logging
import uuid
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from models.generate_presentation_request import GeneratePresentationRequest
from models.presentation_layout import PresentationLayoutModel, SlideLayoutModel
from models.presentation_structure_model import PresentationStructureModel
from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from models.sql.template_v2 import TemplateV2
from services.template_bundles.repository import TemplateBundleRepository
from templates.default_templates import resolve_default_template_id
from templates.v2.schema import get_template_schema
from utils.coded_exception import CodedHTTPException
from utils.icon_weights import DEFAULT_ICON_TYPE, extract_icon_type_from_settings
from utils.generation_font import generation_font_assets, with_generation_font
from api.v1.ppt.endpoints.presentation_template_content import (
    _apply_template_content_to_ui,
    _is_template_layout_payload,
    _template_slide_ui,
)

logger = logging.getLogger(__name__)

CUSTOM_TEMPLATE_PREFIX = "custom-"


def _presentation_task_progress_data(
    created_slides: int,
    remaining_slides: int,
    presentation_id: Optional[uuid.UUID | str] = None,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "created_slides": max(created_slides, 0),
        "remaining_slides": max(remaining_slides, 0),
    }
    # Carried from the moment the task is created, so a polling caller can
    # identify the presentation even while it is still generating -- and,
    # more importantly, after a task ends in error, when there is no
    # response body to learn it from.
    if presentation_id is not None:
        data["presentation_id"] = str(presentation_id)
    return data


def _requested_slide_count(request: GeneratePresentationRequest) -> int:
    if request.slides_markdown:
        return len(request.slides_markdown)
    return request.n_slides or 0


def _template_reference(template_id: str) -> str:
    return template_id


def _extract_template_id(value: Optional[str]) -> Optional[str]:
    if not isinstance(value, str):
        return None

    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith(CUSTOM_TEMPLATE_PREFIX):
        candidate = candidate[len(CUSTOM_TEMPLATE_PREFIX) :].strip()
    return candidate or None


async def _resolve_requested_template(
    template_name: str,
    sql_session: AsyncSession,
) -> Optional[TemplateV2]:
    template_id = _extract_template_id(template_name)
    if not template_id:
        return None

    template = await sql_session.get(TemplateV2, template_id)
    if template:
        return await TemplateBundleRepository().hydrate_template(template)
    if resolve_default_template_id(template_name):
        return None

    raise CodedHTTPException(
        status_code=400,
        detail="Template not found. Please use a valid template.",
        error_code="error.template_not_found",
    )


def _copy_template_layout_payload(
    template: TemplateV2,
) -> dict[str, Any]:
    layout_payload = copy.deepcopy(template.layouts)
    if not isinstance(layout_payload, dict):
        raise CodedHTTPException(
            status_code=400,
            detail="Template layout JSON must be an object",
            error_code="error.template_layout_not_object",
        )

    icon_type = _template_icon_type(template, layout_payload)
    layout_payload["icon_type"] = icon_type
    layout_payload["icon_weight"] = icon_type
    layout_payload["name"] = _template_reference(template.id)
    layout_payload["template_id"] = template.id
    return layout_payload


def _template_icon_type(
    template: TemplateV2,
    layout_payload: dict[str, Any] | None = None,
) -> str:
    for settings in (template.assets, layout_payload, template.layouts):
        if isinstance(settings, dict):
            icon_type = extract_icon_type_from_settings(settings)
            if icon_type != DEFAULT_ICON_TYPE:
                return icon_type
    return DEFAULT_ICON_TYPE


async def _resolve_generation_layout(
    template_name: str,
    sql_session: AsyncSession,
) -> tuple[dict[str, Any], PresentationLayoutModel, Optional[dict[str, str]]]:
    template = await _resolve_requested_template(template_name, sql_session)
    if template is None:
        bundled_template_id = resolve_default_template_id(template_name)
        if not bundled_template_id:
            raise CodedHTTPException(
                status_code=400,
                detail="Template not found. Please use a valid template.",
                error_code="error.template_not_found",
            )
        template = await sql_session.get(TemplateV2, bundled_template_id)
        if not template:
            raise CodedHTTPException(
                status_code=503,
                detail=(
                    f"Bundled template '{template_name}' is not available. "
                    "Restart the service to import bundled templates."
                ),
                error_code="error.bundled_template_unavailable",
                error_params={"template_name": template_name},
            )
        template = await TemplateBundleRepository().hydrate_template(template)
        logger.info(
            "[presentation.generate] resolved bundled template alias "
            "name=%r template_id=%s",
            template_name,
            bundled_template_id,
        )

    layout_payload = with_generation_font(_copy_template_layout_payload(template))
    return (
        layout_payload,
        _build_template_structure_layout(template, layout_payload),
        generation_font_assets(_extract_template_fonts_from_assets(template.assets)),
    )


def _hydrate_template_slide_ui(
    slide: SlideModel,
    layout_payload: Any = None,
) -> None:
    if not _is_template_layout_payload(layout_payload):
        return

    ui = slide.ui
    if not isinstance(ui, dict):
        ui = _template_slide_ui(layout_payload, slide.layout)
    slide.ui = _apply_template_content_to_ui(ui, slide.content)


def _coerce_presentation_font_map(value: Any) -> Optional[dict[str, str]]:
    if not isinstance(value, dict):
        return None

    fonts = {
        name.strip(): url.strip()
        for name, url in value.items()
        if isinstance(name, str)
        and isinstance(url, str)
        and name.strip()
        and url.strip()
    }
    return fonts or None


def _extract_template_fonts_from_assets(assets: Any) -> Optional[dict[str, str]]:
    if not isinstance(assets, dict):
        return None
    return _coerce_presentation_font_map(assets.get("fonts"))


def _insert_toc_layouts(
    structure: PresentationStructureModel,
    n_toc_slides: int,
    include_title_slide: bool,
    toc_slide_layout_index: int,
):
    if n_toc_slides <= 0 or toc_slide_layout_index == -1:
        return

    insertion_index = 1 if include_title_slide else 0
    structure.slides.insert(insertion_index, toc_slide_layout_index)


def _layout_count(layout_payload: Any) -> int:
    if isinstance(layout_payload, dict):
        layouts = layout_payload.get("layouts", layout_payload.get("slides"))
        return len(layouts) if isinstance(layouts, list) else 0
    if isinstance(layout_payload, list):
        return len(layout_payload)
    return 0


def _build_template_layout_model(
    layout_payload: dict[str, Any],
    *,
    layout_name: str,
) -> PresentationLayoutModel:
    try:
        template_schema = get_template_schema(layout_payload)
    except ValueError as exc:
        raise CodedHTTPException(
            status_code=400,
            detail=f"Invalid template layout JSON: {exc}",
            error_code="error.template_layout_invalid_json",
            error_params={"reason": str(exc)},
        ) from exc

    source_layouts = layout_payload.get("layouts")
    if not isinstance(source_layouts, list):
        source_layouts = []

    slides: list[SlideLayoutModel] = []
    for index, schema_layout in enumerate(template_schema["layouts"]):
        if not isinstance(schema_layout, dict):
            continue

        source_layout = (
            source_layouts[index]
            if index < len(source_layouts) and isinstance(source_layouts[index], dict)
            else {}
        )
        layout_id = (
            schema_layout.get("layout_id")
            or source_layout.get("id")
            or f"layout_{index + 1}"
        )
        layout_schema = schema_layout.get("schema")
        if not isinstance(layout_schema, dict):
            layout_schema = {
                "title": str(layout_id),
                "description": source_layout.get("description"),
            }

        slides.append(
            SlideLayoutModel(
                id=str(layout_id),
                name=source_layout.get("name") or layout_schema.get("title"),
                description=source_layout.get("description")
                or layout_schema.get("description"),
                json_schema=layout_schema,
            )
        )

    if not slides:
        raise CodedHTTPException(
            status_code=400,
            detail="Template layout JSON must contain at least one layout",
            error_code="error.template_layout_min_one",
        )

    return PresentationLayoutModel(
        name=layout_name,
        ordered=False,
        icon_type=extract_icon_type_from_settings(layout_payload),
        slides=slides,
    )


def _build_template_structure_layout(
    template: TemplateV2,
    layout_payload: dict[str, Any],
) -> PresentationLayoutModel:
    return _build_template_layout_model(
        layout_payload,
        layout_name=_template_reference(template.id),
    )


def _get_presentation_stream_layout(
    presentation: PresentationModel,
) -> PresentationLayoutModel:
    if _is_template_layout_payload(presentation.layout):
        layout_name = str(presentation.layout.get("name") or "template")
        return _build_template_layout_model(
            presentation.layout,
            layout_name=layout_name,
        )

    return presentation.get_layout()


async def _resolve_prepare_layout(
    layout: str,
    sql_session: AsyncSession,
) -> tuple[dict[str, Any], PresentationLayoutModel, Optional[dict[str, str]]]:
    template_id = _extract_template_id(layout)
    if not template_id:
        raise CodedHTTPException(
            status_code=400,
            detail="Template id is required",
            error_code="error.template_id_required",
        )

    template = await sql_session.get(TemplateV2, template_id)
    if not template:
        raise CodedHTTPException(
            status_code=404,
            detail="Template layout not found",
            error_code="error.template_layout_not_found",
        )
    template = await TemplateBundleRepository().hydrate_template(template)

    layout_payload = with_generation_font(_copy_template_layout_payload(template))

    structure_layout = _build_template_structure_layout(template, layout_payload)
    return (
        layout_payload,
        structure_layout,
        generation_font_assets(_extract_template_fonts_from_assets(template.assets)),
    )

