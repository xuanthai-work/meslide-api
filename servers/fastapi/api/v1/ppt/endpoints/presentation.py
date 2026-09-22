import asyncio
from datetime import datetime
import json
import logging
import random
import traceback
from typing import Annotated, List, Optional, Tuple
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Body,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
)
from pydantic import BaseModel
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from constants.presentation import MAX_NUMBER_OF_SLIDES
from enums.async_task_status import AsyncTaskStatus
from models.generate_presentation_request import GeneratePresentationRequest
from models.presentation_and_path import PresentationPathAndEditPath
from models.presentation_outline_model import (
    PresentationOutlineModel,
    SlideOutlineModel,
)
from models.presentation_structure_model import PresentationStructureModel
from models.presentation_with_slides import (
    PresentationWithSlides,
)
from services.image_generation_service import ImageGenerationService
from services.mem0_presentation_memory_service import (
    MEM0_PRESENTATION_MEMORY_SERVICE,
)
from models.sql.slide import SlideModel
from models.sse_response import (
    SSECompleteResponse,
    SSEErrorResponse,
    SSEResponse,
    SSEStatusResponse,
)

from services.database import get_async_session
from services.database import async_session_maker
from services.image_search.models import DeckResolutionContext
from services.image_search.constants import (
    IMAGE_STATUS_TEXT,
)
from models.sql.presentation import PresentationModel
from models.sql.async_task import AsyncTaskModel
from utils.asset_directory_utils import (
    get_images_directory,
)
from utils.llm_calls.generate_presentation_structure import (
    generate_presentation_structure,
)
from utils.llm_calls.generate_slide_content import (
    get_slide_content_from_type_and_outline,
)
from utils.ppt_utils import (
    select_toc_or_list_slide_layout_index,
)
from utils.outline_utils import (
    get_images_for_slides_from_outline,
    get_no_of_toc_required_for_n_outlines,
    get_presentation_outline_model_with_toc,
    strip_generated_toc_outlines,
)
from utils.process_slides import (
    process_slide_add_placeholder_assets,
    process_slide_and_fetch_assets,
)
from utils.coded_exception import CodedHTTPException
from utils.sse import safe_sse_stream, sse_streaming_response
from api.v1.auth.context import get_current_owner_id
from templates.default_templates import resolve_default_template_id
import uuid

from api.v1.ppt.endpoints.presentation_template_content import (
    _apply_template_content_to_ui,
    _template_slide_ui,
)
from api.v1.ppt.endpoints.presentation_responses import _presentation_response_data
from api.v1.ppt.endpoints.presentation_generate import generate_presentation_handler
from api.v1.ppt.endpoints.presentation_layout import (
    _get_presentation_stream_layout,
    _insert_toc_layouts,
    _layout_count,
    _presentation_task_progress_data,
    _requested_slide_count,
    _resolve_prepare_layout,
    _resolve_requested_template,
)
from api.v1.ppt.endpoints import presentation_crud as _presentation_crud

PRESENTATION_CRUD_ROUTER = _presentation_crud.PRESENTATION_CRUD_ROUTER
BLANK_PRESENTATION_LAYOUT_GROUP = _presentation_crud.BLANK_PRESENTATION_LAYOUT_GROUP
BLANK_PRESENTATION_LAYOUT_ID = _presentation_crud.BLANK_PRESENTATION_LAYOUT_ID
BLANK_PRESENTATION_SLIDE_UI = _presentation_crud.BLANK_PRESENTATION_SLIDE_UI
_blank_presentation_slide_ui = _presentation_crud._blank_presentation_slide_ui
_build_export_cookie_header = _presentation_crud._build_export_cookie_header
create_blank_presentation = _presentation_crud.create_blank_presentation
create_presentation = _presentation_crud.create_presentation
delete_presentation = _presentation_crud.delete_presentation
derive_presentation_from_existing_one = (
    _presentation_crud.derive_presentation_from_existing_one
)
duplicate_presentation = _presentation_crud.duplicate_presentation
edit_presentation_with_new_content = (
    _presentation_crud.edit_presentation_with_new_content
)
export_existing_presentation = _presentation_crud.export_existing_presentation
get_all_presentations = _presentation_crud.get_all_presentations
get_presentation = _presentation_crud.get_presentation
update_presentation = _presentation_crud.update_presentation
update_presentation_slide = _presentation_crud.update_presentation_slide

logger = logging.getLogger(__name__)


PRESENTATION_ROUTER = APIRouter(prefix="/presentation", tags=["Presentation"])
ASYNC_TASK_TYPE_PRESENTATION_GENERATE = "presentation.generate"
PRESENTATION_ROUTER.include_router(PRESENTATION_CRUD_ROUTER)


class PresentationPrepareResponse(BaseModel):
    presentation_id: uuid.UUID


@PRESENTATION_ROUTER.post("/prepare", response_model=PresentationPrepareResponse)
async def prepare_presentation(
    presentation_id: Annotated[uuid.UUID, Body()],
    outlines: Annotated[List[SlideOutlineModel], Body()],
    layout: Annotated[str, Body()],
    title: Annotated[Optional[str], Body()] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    if not outlines:
        raise CodedHTTPException(
            status_code=400, detail="Outlines are required", error_code="error.outlines_required"
        )
    if len(outlines) > MAX_NUMBER_OF_SLIDES:
        raise CodedHTTPException(
            status_code=400,
            detail=f"Number of outlines cannot be greater than {MAX_NUMBER_OF_SLIDES}",
            error_code="error.outlines_too_many",
            error_params={"max": MAX_NUMBER_OF_SLIDES},
        )

    presentation = await sql_session.get(PresentationModel, presentation_id)
    if not presentation:
        raise CodedHTTPException(
        status_code=404,
        detail="Presentation not found",
        error_code="error.presentation_not_found",
    )

    presentation_outline_model = PresentationOutlineModel(slides=outlines)
    if presentation.include_table_of_contents:
        presentation_outline_model = strip_generated_toc_outlines(
            presentation_outline_model,
            title_slide=presentation.include_title_slide,
        )

    layout_payload, structure_layout, template_fonts = await _resolve_prepare_layout(
        layout, sql_session
    )
    total_slide_layouts = _layout_count(layout_payload)
    if total_slide_layouts < 1:
        raise CodedHTTPException(
            status_code=400,
            detail="Layout must contain at least one slide layout",
            error_code="error.layout_min_one",
        )
    total_outlines = len(presentation_outline_model.slides)

    if structure_layout.ordered:
        presentation_structure = structure_layout.to_presentation_structure()
    else:
        presentation_structure: PresentationStructureModel = (
            await generate_presentation_structure(
                presentation_outline=presentation_outline_model,
                presentation_layout=structure_layout,
                instructions=presentation.instructions,
                source_content=presentation.content,
            )
        )

    presentation_structure.slides = presentation_structure.slides[:total_outlines]
    for index in range(total_outlines):
        random_slide_index = random.randint(0, total_slide_layouts - 1)
        if index >= total_outlines:
            presentation_structure.slides.append(random_slide_index)
            continue
        if presentation_structure.slides[index] >= total_slide_layouts:
            presentation_structure.slides[index] = random_slide_index

    if presentation.include_table_of_contents:
        n_toc_slides = get_no_of_toc_required_for_n_outlines(
            n_outlines=total_outlines,
            title_slide=presentation.include_title_slide,
            target_total_slides=(presentation.n_slides if presentation.n_slides > 0 else None),
        )
        toc_slide_layout_index = select_toc_or_list_slide_layout_index(structure_layout)
        _insert_toc_layouts(
            presentation_structure,
            n_toc_slides,
            presentation.include_title_slide,
            toc_slide_layout_index,
        )
        if toc_slide_layout_index != -1 and n_toc_slides > 0:
            presentation_outline_model = get_presentation_outline_model_with_toc(
                outline=presentation_outline_model,
                n_toc_slides=n_toc_slides,
                title_slide=presentation.include_title_slide,
            )

    sql_session.add(presentation)
    presentation.outlines = presentation_outline_model.model_dump(mode="json")
    presentation.title = title or presentation.title
    # Final slide generation should follow the reviewed outline text. The
    # original upload language can be stale after outline-page chat edits such
    # as "convert these to Chinese".
    presentation.language = ""
    presentation.layout = layout_payload
    presentation.fonts = template_fonts
    presentation.set_structure(presentation_structure)
    await sql_session.commit()

    await MEM0_PRESENTATION_MEMORY_SERVICE.store_generated_outlines(
        presentation.id,
        presentation.outlines,
    )

    return PresentationPrepareResponse(presentation_id=presentation.id)


@PRESENTATION_ROUTER.get("/stream/{id}", response_model=PresentationWithSlides)
async def stream_presentation(
    id: uuid.UUID,
    request: Request = None,
    regenerate: bool = Query(False),
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise CodedHTTPException(
        status_code=404,
        detail="Presentation not found",
        error_code="error.presentation_not_found",
    )
    if not presentation.structure:
        raise CodedHTTPException(
            status_code=400,
            detail="Presentation not prepared for stream",
            error_code="error.not_prepared_for_stream",
        )
    if not presentation.outlines:
        raise CodedHTTPException(
            status_code=400,
            detail="Outlines can not be empty",
            error_code="error.outlines_empty",
        )

    try:
        structure = presentation.get_structure()
        layout = _get_presentation_stream_layout(presentation)
        outline = presentation.get_presentation_outline()
    except Exception as exc:
        raise CodedHTTPException(
            status_code=400,
            detail="Presentation has invalid generated data",
            error_code="error.presentation_data_invalid",
        ) from exc

    if not layout.slides:
        raise CodedHTTPException(
            status_code=400,
            detail="Presentation layout has no slides",
            error_code="error.presentation_layout_empty",
        )
    if len(structure.slides) > len(outline.slides):
        raise CodedHTTPException(
            status_code=400,
            detail="Presentation structure has more slides than outlines",
            error_code="error.presentation_structure_mismatch",
        )
    invalid_layout_index = next(
        (
            slide_layout_index
            for slide_layout_index in structure.slides
            if slide_layout_index < 0 or slide_layout_index >= len(layout.slides)
        ),
        None,
    )
    if invalid_layout_index is not None:
        raise CodedHTTPException(
            status_code=400,
            detail="Presentation structure contains an invalid slide layout",
            error_code="error.presentation_layout_invalid",
        )

    image_generation_service = ImageGenerationService(get_images_directory())

    async def inner():
        icon_weight = layout.icon_weight
        image_urls_for_slides = get_images_for_slides_from_outline(outline.slides)

        async_assets_generation_tasks: List[asyncio.Task] = []
        asset_events: asyncio.Queue = asyncio.Queue()
        asset_warnings_by_slide: dict[int, list[dict]] = {}
        # Presentation-scoped: prevents several slots across the whole deck
        # from resolving to the same searched image or one dominant domain.
        deck_image_search_context = DeckResolutionContext()
        image_status_emitted: set[str] = set()

        def queue_image_status(code: str) -> None:
            """Progress codes from the search service land on the event queue."""
            asset_events.put_nowait(("status", code))

        async def emit_image_status(code: str):
            """Yield one SSE status per image-search phase (deduplicated)."""
            if code in image_status_emitted:
                return
            image_status_emitted.add(code)
            yield SSEStatusResponse(
                status=IMAGE_STATUS_TEXT.get(code, code),
                status_code=code,
            ).to_string()

        async def notify_slide_assets_ready(slide_index: int, asset_task: asyncio.Task):
            try:
                await asset_task
            except Exception:
                logger.exception(
                    "Slide asset generation failed: presentation_id=%s slide_index=%s",
                    id,
                    slide_index,
                )
                asset_warnings_by_slide.setdefault(slide_index, []).append(
                    {
                        "type": "asset_generation_failed",
                        "message": "Some slide assets could not be generated.",
                    }
                )
            finally:
                await asset_events.put(("slide", slide_index))

        slides: List[SlideModel] = []
        yield SSEResponse(
            event="response",
            data=json.dumps({"type": "chunk", "chunk": '{ "slides": [ '}),
        ).to_string()
        yielded_slide_asset_sse_count = 0

        for i, slide_layout_index in enumerate(structure.slides):
            if request is not None and await request.is_disconnected():
                logger.info(
                    "[presentation.stream.standard] client disconnected "
                    "presentation_id=%s slide_index=%s",
                    id,
                    i,
                )
                raise asyncio.CancelledError
            slide_layout = layout.slides[slide_layout_index]

            try:
                slide_content = await get_slide_content_from_type_and_outline(
                    slide_layout,
                    outline.slides[i],
                    presentation.language,
                    presentation.tone,
                    presentation.verbosity,
                    presentation.instructions,
                    slide_number=i + 1,
                    disconnect_checker=(
                        request.is_disconnected if request is not None else None
                    ),
                    web_search=presentation.web_search,
                )
            except HTTPException as e:
                yield SSEErrorResponse(
                    detail=e.detail,
                    error_code=(
                        e.error_code if isinstance(e, CodedHTTPException) else None
                    ),
                    error_params=(
                        e.error_params if isinstance(e, CodedHTTPException) else None
                    ),
                ).to_string()
                return

            slide = SlideModel(
                presentation=id,
                layout_group=layout.name,
                layout=slide_layout.id,
                index=i,
                speaker_note=slide_content.get("__speaker_note__", ""),
                content=slide_content,
                ui=_template_slide_ui(presentation.layout, slide_layout.id),
            )
            slides.append(slide)

            # This will mutate slide and add placeholder assets
            process_slide_add_placeholder_assets(slide)
            slide.ui = _apply_template_content_to_ui(slide.ui, slide.content)

            # This will mutate slide - start task immediately so it runs in parallel with next slide LLM generation
            asset_warnings_by_slide[i] = []
            asset_task = asyncio.create_task(
                process_slide_and_fetch_assets(
                    image_generation_service,
                    slide,
                    outline_image_urls=(
                        image_urls_for_slides[i]
                        if i < len(image_urls_for_slides)
                        else None
                    ),
                    icon_weight=icon_weight,
                    allow_image_fallback=True,
                    image_warnings=asset_warnings_by_slide[i],
                    web_search=presentation.web_search,
                    deck_image_context=deck_image_search_context,
                    image_status_callback=queue_image_status,
                )
            )
            async_assets_generation_tasks.append(asset_task)
            asyncio.create_task(notify_slide_assets_ready(i, asset_task))

            yield SSEResponse(
                event="response",
                data=json.dumps({"type": "chunk", "chunk": slide.model_dump_json()}),
            ).to_string()

            while True:
                try:
                    event_kind, event_value = asset_events.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if event_kind == "status":
                    async for status_frame in emit_image_status(event_value):
                        yield status_frame
                    continue
                done_idx = event_value
                slides[done_idx].ui = _apply_template_content_to_ui(
                    slides[done_idx].ui,
                    slides[done_idx].content,
                )
                yielded_slide_asset_sse_count += 1
                yield SSEResponse(
                    event="response",
                    data=json.dumps(
                        {
                            "type": "slide_assets",
                            "slide_index": done_idx,
                            "slide": slides[done_idx].model_dump(mode="json"),
                            "warnings": asset_warnings_by_slide.get(done_idx, []),
                        }
                    ),
                ).to_string()

        yield SSEResponse(
            event="response",
            data=json.dumps({"type": "chunk", "chunk": " ] }"}),
        ).to_string()

        while yielded_slide_asset_sse_count < len(slides):
            event_kind, event_value = await asset_events.get()
            if event_kind == "status":
                async for status_frame in emit_image_status(event_value):
                    yield status_frame
                continue
            done_idx = event_value
            slides[done_idx].ui = _apply_template_content_to_ui(
                slides[done_idx].ui,
                slides[done_idx].content,
            )
            yielded_slide_asset_sse_count += 1
            yield SSEResponse(
                event="response",
                data=json.dumps(
                    {
                        "type": "slide_assets",
                        "slide_index": done_idx,
                        "slide": slides[done_idx].model_dump(mode="json"),
                        "warnings": asset_warnings_by_slide.get(done_idx, []),
                    }
                ),
            ).to_string()

        generated_assets_lists = await asyncio.gather(
            *async_assets_generation_tasks,
            return_exceptions=True,
        )
        generated_assets = []
        for assets_list in generated_assets_lists:
            if isinstance(assets_list, Exception):
                logger.error(
                    "Slide asset generation failed during final collection: %s",
                    assets_list,
                )
                continue
            generated_assets.extend(assets_list)

        for slide in slides:
            slide.ui = _apply_template_content_to_ui(slide.ui, slide.content)

        # Moved this here to make sure new slides are generated before deleting the old ones
        await sql_session.execute(
            delete(SlideModel).where(
                SlideModel.presentation == id,
                SlideModel.owner_id == get_current_owner_id(),
            )
        )
        await sql_session.commit()

        sql_session.add(presentation)
        sql_session.add_all(slides)
        sql_session.add_all(generated_assets)
        await sql_session.commit()

        response = PresentationWithSlides(
            **_presentation_response_data(presentation),
            slides=slides,
        )

        yield SSECompleteResponse(
            key="presentation",
            value=response.model_dump(mode="json"),
        ).to_string()

    async def rollback_stream_session():
        await sql_session.rollback()

    return sse_streaming_response(
        safe_sse_stream(
            inner(),
            logger=logger,
            error_detail="Failed to generate presentation slides. Please try again.",
            error_code="error.slides_generation_failed",
            on_error=rollback_stream_session,
        )
    )




async def check_if_api_request_is_valid(
    request: GeneratePresentationRequest,
    sql_session: AsyncSession = Depends(get_async_session),
) -> Tuple[uuid.UUID,]:
    presentation_id = uuid.uuid4()
    print(f"Presentation ID: {presentation_id}")

    # Making sure either content, slides markdown or files is provided
    if not (request.content or request.slides_markdown or request.files):
        raise CodedHTTPException(
            status_code=400,
            detail="Either content or slides markdown or files is required to generate presentation",
            error_code="error.content_required",
        )

    if request.n_slides is not None and request.n_slides <= 0:
        raise CodedHTTPException(
            status_code=400,
            detail="Number of slides must be greater than 0",
            error_code="error.slides_must_be_positive",
        )

    if request.n_slides is not None and request.n_slides > MAX_NUMBER_OF_SLIDES:
        raise CodedHTTPException(
            status_code=400,
            detail=f"Number of slides cannot be greater than {MAX_NUMBER_OF_SLIDES}",
            error_code="error.slides_too_many",
            error_params={"max": MAX_NUMBER_OF_SLIDES},
        )

    if (
        request.slides_markdown is not None
        and len(request.slides_markdown) > MAX_NUMBER_OF_SLIDES
    ):
        raise CodedHTTPException(
            status_code=400,
            detail=f"Number of slides cannot be greater than {MAX_NUMBER_OF_SLIDES}",
            error_code="error.slides_too_many",
            error_params={"max": MAX_NUMBER_OF_SLIDES},
        )

    if (
        request.include_table_of_contents
        and request.n_slides is not None
        and request.n_slides < 3
    ):
        raise CodedHTTPException(
            status_code=400,
            detail="Number of slides cannot be less than 3 if table of contents is included",
            error_code="error.slides_toc_min",
        )

    # Checking if template is valid. Generation supports TemplateV2 rows and
    # bundled TemplateV2 names only.
    template = await _resolve_requested_template(
        request.template,
        sql_session,
    )
    if template:
        request.template = template.id
        return (presentation_id,)

    if resolve_default_template_id(request.template):
        return (presentation_id,)

    raise CodedHTTPException(
        status_code=400,
        detail="Template not found. Please use a valid template.",
        error_code="error.template_not_found",
    )


@PRESENTATION_ROUTER.post("/generate", response_model=PresentationPathAndEditPath)
async def generate_presentation_sync(
    request_http: Request,
    request: GeneratePresentationRequest,
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        (presentation_id,) = await check_if_api_request_is_valid(request, sql_session)
        return await generate_presentation_handler(
            request,
            presentation_id,
            None,
            export_cookie_header=_build_export_cookie_header(request_http),
            request_http=request_http,
            sql_session=sql_session,
        )
    except HTTPException:
        raise
    except Exception:
        traceback.print_exc()
        raise CodedHTTPException(
            status_code=500,
            detail="Presentation generation failed",
            error_code="error.generation_failed",
        )


async def _run_generate_presentation_task(
    request: GeneratePresentationRequest,
    presentation_id: uuid.UUID,
    task_id: str,
    export_cookie_header: Optional[str],
) -> None:
    async with async_session_maker() as sql_session:
        async_status = await sql_session.get(AsyncTaskModel, task_id)
        if not async_status:
            logger.warning(
                "[presentation.generate.async] task missing task_id=%s",
                task_id,
            )
            return

        async_status.status = AsyncTaskStatus.PENDING
        async_status.message = "Starting presentation generation"
        async_status.data = _presentation_task_progress_data(
            created_slides=0,
            remaining_slides=_requested_slide_count(request),
            presentation_id=presentation_id,
        )
        async_status.updated_at = datetime.now()
        sql_session.add(async_status)
        await sql_session.commit()

        await generate_presentation_handler(
            request,
            presentation_id,
            async_status=async_status,
            export_cookie_header=export_cookie_header,
            sql_session=sql_session,
        )


@PRESENTATION_ROUTER.post("/generate/async", response_model=AsyncTaskModel)
async def generate_presentation_async(
    request_http: Request,
    request: GeneratePresentationRequest,
    background_tasks: BackgroundTasks,
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        (presentation_id,) = await check_if_api_request_is_valid(request, sql_session)

        async_status = AsyncTaskModel(
            type=ASYNC_TASK_TYPE_PRESENTATION_GENERATE,
            status=AsyncTaskStatus.PENDING,
            message="Queued for generation",
            data=_presentation_task_progress_data(
                created_slides=0,
                remaining_slides=_requested_slide_count(request),
                presentation_id=presentation_id,
            ),
        )
        sql_session.add(async_status)
        await sql_session.commit()
        await sql_session.refresh(async_status)

        background_tasks.add_task(
            _run_generate_presentation_task,
            request,
            presentation_id,
            async_status.id,
            _build_export_cookie_header(request_http),
        )
        return async_status

    except Exception as e:
        if not isinstance(e, HTTPException):
            print(e)
            e = CodedHTTPException(
                status_code=500,
                detail="Presentation generation failed",
                error_code="error.generation_failed",
            )

        raise e


@PRESENTATION_ROUTER.get("/status/{id}", response_model=AsyncTaskModel)
async def check_async_presentation_generation_status(
    id: str = Path(description="ID of the presentation generation task"),
    sql_session: AsyncSession = Depends(get_async_session),
):
    status = await sql_session.get(AsyncTaskModel, id)
    if not status:
        raise CodedHTTPException(
            status_code=404,
            detail="No presentation generation task found",
            error_code="error.generation_task_not_found",
        )
    return status


