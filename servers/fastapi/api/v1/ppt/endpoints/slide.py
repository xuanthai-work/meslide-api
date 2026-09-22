import logging
from typing import Annotated
from fastapi import APIRouter, Body, Depends
from sqlalchemy.ext.asyncio import AsyncSession
import uuid

from models.sql.presentation import PresentationModel
from models.sql.slide import SlideModel
from services.database import get_async_session
from services.image_generation_service import ImageGenerationService
from services.mem0_presentation_memory_service import (
    MEM0_PRESENTATION_MEMORY_SERVICE,
)
from utils.asset_directory_utils import get_images_directory
from utils.coded_exception import CodedHTTPException
from utils.llm_calls.edit_slide import get_edited_slide_content
from utils.llm_calls.select_slide_type_on_edit import get_slide_layout_from_prompt
from utils.process_slides import process_old_and_new_slides_and_fetch_assets


SLIDE_ROUTER = APIRouter(prefix="/slide", tags=["Slide"])
LOGGER = logging.getLogger(__name__)


def _is_template_layout_payload(layout: object) -> bool:
    return isinstance(layout, dict) and isinstance(layout.get("layouts"), list)


@SLIDE_ROUTER.post("/edit")
async def edit_slide(
    id: Annotated[uuid.UUID, Body()],
    prompt: Annotated[str, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    slide = await sql_session.get(SlideModel, id)
    if not slide:
        raise CodedHTTPException(
            status_code=404, detail="Slide not found", error_code="error.slide_not_found"
        )
    presentation = await sql_session.get(PresentationModel, slide.presentation)
    if not presentation:
        raise CodedHTTPException(
            status_code=404,
            detail="Presentation not found",
            error_code="error.presentation_not_found",
        )

    memory_context = await MEM0_PRESENTATION_MEMORY_SERVICE.retrieve_context(
        presentation.id,
        prompt,
    )

    presentation_layout = presentation.get_layout()
    slide_layout = await get_slide_layout_from_prompt(
        prompt,
        presentation_layout,
        slide,
        memory_context,
    )

    edited_slide_content = await get_edited_slide_content(
        prompt,
        slide,
        presentation.language,
        slide_layout,
        presentation.tone,
        presentation.verbosity,
        presentation.instructions,
        memory_context,
    )

    image_generation_service = ImageGenerationService(get_images_directory())

    # This will mutate edited_slide_content
    image_warnings: list[dict] = []
    new_assets = await process_old_and_new_slides_and_fetch_assets(
        image_generation_service,
        slide.content,
        edited_slide_content,
        icon_weight=presentation.get_layout().icon_weight,
        use_template_asset_fields=(
            _is_template_layout_payload(presentation.layout)
            or isinstance(slide.ui, dict)
        ),
        allow_image_fallback=True,
        image_warnings=image_warnings,
        web_search=presentation.web_search,
    )
    for warning in image_warnings:
        LOGGER.warning(
            "Slide edit image generation warning: presentation_id=%s slide_id=%s detail=%s",
            presentation.id,
            slide.id,
            warning.get("detail"),
        )

    # Always assign a new unique id to the slide
    slide.id = uuid.uuid4()

    sql_session.add(slide)
    slide.content = edited_slide_content
    slide.layout = slide_layout.id
    slide.speaker_note = edited_slide_content.get("__speaker_note__", "")
    sql_session.add_all(new_assets)
    await sql_session.commit()

    await MEM0_PRESENTATION_MEMORY_SERVICE.store_slide_edit(
        presentation_id=presentation.id,
        slide_index=slide.index,
        edit_prompt=prompt,
        edited_slide_content=edited_slide_content,
    )

    return slide
