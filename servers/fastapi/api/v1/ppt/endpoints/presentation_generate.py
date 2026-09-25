"""Standard presentation generation handler.

Extracted from ``api.v1.ppt.endpoints.presentation`` so the router only
orchestrates HTTP/async-task wiring. Existing tests keep patching the
re-exported ``generate_presentation_handler`` on the router module.
"""

from __future__ import annotations

import asyncio
import logging
import random
import traceback
import uuid
from datetime import datetime
from typing import List, Optional

import dirtyjson
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from constants.presentation import MAX_NUMBER_OF_SLIDES
from enums.async_task_status import AsyncTaskStatus
from enums.webhook_event import WebhookEvent
from models.api_error_model import APIErrorModel
from models.generate_presentation_request import GeneratePresentationRequest
from models.presentation_and_path import PresentationPathAndEditPath
from models.presentation_outline_model import (
    PresentationOutlineModel,
    SlideOutlineModel,
)
from models.presentation_structure_model import PresentationStructureModel
from models.sql.async_task import AsyncTaskModel
from models.sql.presentation import PresentationModel, PresentationVersion
from models.sql.slide import SlideModel
from services.concurrent_service import CONCURRENT_SERVICE
from services.database import get_async_session
from services.documents_loader import DocumentsLoader
from services.image_generation_service import ImageGenerationService
from services.image_search.models import DeckResolutionContext
from services.mem0_presentation_memory_service import (
    MEM0_PRESENTATION_MEMORY_SERVICE,
)
from services.webhook_service import WebhookService
from utils.asset_directory_utils import get_images_directory
from utils.coded_exception import CodedHTTPException
from utils.export_utils import export_presentation
from utils.json_parse_diagnostics import describe_json_parse_failure
from utils.generation_font import with_generation_font
from utils.llm_calls.generate_presentation_outlines import (
    generate_ppt_outline,
    get_messages as get_outline_messages,
)
from utils.llm_calls.generate_presentation_structure import (
    generate_presentation_structure,
)
from utils.llm_calls.generate_slide_content import (
    get_slide_content_from_type_and_outline,
)
from utils.llm_utils import message_content_to_text
from utils.outline_limits import normalize_outline_payload
from utils.outline_utils import (
    get_images_for_slides_from_outline,
    get_no_of_outlines_to_generate_for_n_slides,
    get_no_of_toc_required_for_n_outlines,
    get_presentation_outline_model_with_toc,
    get_presentation_title_from_presentation_outline,
    strip_generated_toc_outlines,
)
from utils.ppt_utils import select_toc_or_list_slide_layout_index
from utils.process_slides import process_slide_and_fetch_assets
from api.v1.ppt.endpoints.presentation_layout import (
    _hydrate_template_slide_ui,
    _insert_toc_layouts,
    _presentation_task_progress_data,
    _requested_slide_count,
    _resolve_generation_layout,
)
from api.v1.ppt.endpoints.presentation_template_content import _template_slide_ui

logger = logging.getLogger(__name__)


async def generate_presentation_handler(
    request: GeneratePresentationRequest,
    presentation_id: uuid.UUID,
    async_status: Optional[AsyncTaskModel],
    export_cookie_header: Optional[str] = None,
    request_http: Optional[Request] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        disconnect_checker = (
            request_http.is_disconnected if request_http is not None else None
        )

        async def raise_if_client_disconnected() -> None:
            if disconnect_checker and await disconnect_checker():
                logger.info(
                    "[presentation.generate] client disconnected presentation_id=%s",
                    presentation_id,
                )
                raise asyncio.CancelledError

        await raise_if_client_disconnected()
        using_slides_markdown = False
        language_to_use = (request.language or "").strip() or None
        additional_context = ""

        if request.slides_markdown:
            using_slides_markdown = True
            if len(request.slides_markdown) > MAX_NUMBER_OF_SLIDES:
                raise CodedHTTPException(
                    status_code=400,
                    detail=f"Number of slides cannot be greater than {MAX_NUMBER_OF_SLIDES}",
                    error_code="error.slides_too_many",
                    error_params={"max": MAX_NUMBER_OF_SLIDES},
                )
            request.n_slides = len(request.slides_markdown)

        if not using_slides_markdown:
            # Updating async status
            if async_status:
                async_status.message = "Generating presentation outlines"
                async_status.data = _presentation_task_progress_data(
                    created_slides=0,
                    remaining_slides=_requested_slide_count(request),
                    presentation_id=presentation_id,
                )
                async_status.updated_at = datetime.now()
                sql_session.add(async_status)
                await sql_session.commit()

            if request.files:
                documents_loader = DocumentsLoader(
                    file_paths=request.files,
                    presentation_language=request.language,
                )
                await documents_loader.load_documents()
                documents = documents_loader.documents
                if documents:
                    additional_context = "\n\n".join(documents)

            # Finding number of slides to generate by considering table of contents
            n_slides_to_generate = request.n_slides
            if request.include_table_of_contents and request.n_slides is not None:
                n_slides_to_generate = (
                    get_no_of_outlines_to_generate_for_n_slides(
                        n_slides=request.n_slides,
                        toc=True,
                        title_slide=request.include_title_slide,
                    )
                )

            outline_messages = get_outline_messages(
                request.content,
                n_slides_to_generate,
                language_to_use,
                additional_context,
                request.tone.value,
                request.verbosity.value,
                request.instructions,
                request.include_title_slide,
                request.include_table_of_contents,
            )
            await MEM0_PRESENTATION_MEMORY_SERVICE.store_generation_context(
                presentation_id=presentation_id,
                system_prompt=(
                    message_content_to_text(outline_messages[0].content)
                    if len(outline_messages) > 0
                    else None
                ),
                user_prompt=(
                    message_content_to_text(outline_messages[1].content)
                    if len(outline_messages) > 1
                    else None
                ),
                extracted_document_text=additional_context,
                source_content=request.content,
                instructions=request.instructions,
            )

            presentation_outlines_text = ""
            async for chunk in generate_ppt_outline(
                request.content,
                n_slides_to_generate,
                language_to_use,
                additional_context,
                request.tone.value,
                request.verbosity.value,
                request.instructions,
                request.include_title_slide,
                request.web_search,
                request.include_table_of_contents,
                disconnect_checker=disconnect_checker,
            ):

                if isinstance(chunk, HTTPException):
                    raise chunk

                presentation_outlines_text += chunk

            try:
                presentation_outlines_json = dict(
                    dirtyjson.loads(presentation_outlines_text)
                )
            except Exception as error:
                traceback.print_exc()
                logger.error(
                    "Outline JSON parse failed %s",
                    describe_json_parse_failure(presentation_outlines_text, error),
                )
                raise CodedHTTPException(
                    status_code=400,
                    detail="Failed to generate presentation outlines. Please try again.",
                    error_code="error.outline_generation_failed",
                )
            presentation_outlines = PresentationOutlineModel(
                **normalize_outline_payload(
                    presentation_outlines_json,
                    MAX_NUMBER_OF_SLIDES,
                )
            )

            if n_slides_to_generate is not None:
                if len(presentation_outlines.slides) < n_slides_to_generate:
                    raise CodedHTTPException(
                        status_code=400,
                        detail=(
                            "Failed to generate presentation outlines with requested "
                            "number of slides. Please try again."
                        ),
                        error_code="error.outline_slide_count",
                    )
                # Overshooting by a slide or two is common and recoverable:
                # keep the first n and drop the rest. Only undershooting is a
                # real failure, since there is nothing to pad the deck with.
                presentation_outlines.slides = presentation_outlines.slides[
                    :n_slides_to_generate
                ]

            total_outlines = len(presentation_outlines.slides)

        else:
            # Setting outlines to slides markdown
            presentation_outlines = PresentationOutlineModel(
                slides=[
                    SlideOutlineModel(content=slide)
                    for slide in request.slides_markdown
                ]
            )
            total_outlines = len(request.slides_markdown)

            await MEM0_PRESENTATION_MEMORY_SERVICE.store_generation_context(
                presentation_id=presentation_id,
                system_prompt=None,
                user_prompt=None,
                extracted_document_text=None,
                source_content=request.content,
                instructions=request.instructions,
            )

        await MEM0_PRESENTATION_MEMORY_SERVICE.store_generated_outlines(
            presentation_id,
            presentation_outlines.model_dump(mode="json"),
        )

        # Updating async status
        if async_status:
            async_status.message = "Selecting layout for each slide"
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        print("-" * 40)
        print(f"Generated {total_outlines} outlines for the presentation")

        logger.info(
            "[presentation.generate] loading layout template=%r presentation_id=%s",
            request.template,
            presentation_id,
        )
        (
            layout_payload,
            layout_model,
            template_fonts,
        ) = await _resolve_generation_layout(request.template, sql_session)
        logger.info(
            "[presentation.generate] layout ready template=%r slides=%d ordered=%s icon_weight=%s",
            request.template,
            len(layout_model.slides),
            layout_model.ordered,
            layout_model.icon_weight,
        )
        total_slide_layouts = len(layout_model.slides)

        should_include_toc = (
            request.include_table_of_contents and not using_slides_markdown
        )
        if should_include_toc:
            presentation_outlines = strip_generated_toc_outlines(
                presentation_outlines,
                title_slide=request.include_title_slide,
            )
            total_outlines = len(presentation_outlines.slides)

        # Generate Structure
        if layout_model.ordered:
            presentation_structure = layout_model.to_presentation_structure()
        else:
            presentation_structure: PresentationStructureModel = (
                await generate_presentation_structure(
                    presentation_outline=presentation_outlines,
                    presentation_layout=layout_model,
                    instructions=request.instructions,
                    using_slides_markdown=using_slides_markdown,
                    source_content=request.content,
                    disconnect_checker=disconnect_checker,
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

        if should_include_toc:
            n_toc_slides = get_no_of_toc_required_for_n_outlines(
                n_outlines=total_outlines,
                title_slide=request.include_title_slide,
                target_total_slides=request.n_slides,
            )
            toc_slide_layout_index = select_toc_or_list_slide_layout_index(layout_model)
            _insert_toc_layouts(
                presentation_structure,
                n_toc_slides,
                request.include_title_slide,
                toc_slide_layout_index,
            )
            if toc_slide_layout_index != -1 and n_toc_slides > 0:
                presentation_outlines = get_presentation_outline_model_with_toc(
                    outline=presentation_outlines,
                    n_toc_slides=n_toc_slides,
                    title_slide=request.include_title_slide,
                )

        final_n_slides = request.n_slides
        if final_n_slides is None:
            final_n_slides = len(presentation_outlines.slides)

        # Create PresentationModel
        presentation = PresentationModel(
            id=presentation_id,
            version=PresentationVersion.V2_STANDARD,
            content=request.content,
            n_slides=final_n_slides,
            language=language_to_use or "",
            title=get_presentation_title_from_presentation_outline(
                presentation_outlines
            ),
            outlines=presentation_outlines.model_dump(),
            layout=layout_payload,
            structure=presentation_structure.model_dump(),
            tone=request.tone.value,
            verbosity=request.verbosity.value,
            instructions=request.instructions,
            web_search=request.web_search,
            fonts=template_fonts,
        )

        # Updating async status
        if async_status:
            async_status.message = "Generating slides"
            async_status.data = _presentation_task_progress_data(
                created_slides=0,
                remaining_slides=final_n_slides or 0,
                presentation_id=presentation_id,
            )
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        image_generation_service = ImageGenerationService(get_images_directory())
        async_assets_generation_tasks = []
        image_warnings: List[dict] = []
        deck_image_search_context = DeckResolutionContext()

        # 7. Generate slide content concurrently (batched), then build slides and fetch assets
        slides: List[SlideModel] = []

        slide_layout_indices = presentation_structure.slides
        slide_layouts = [layout_model.slides[idx] for idx in slide_layout_indices]
        total_slides_to_create = len(slide_layouts)

        # Schedule slide content generation and asset fetching in batches of 10
        batch_size = 10
        for start in range(0, len(slide_layouts), batch_size):
            await raise_if_client_disconnected()
            end = min(start + batch_size, len(slide_layouts))

            print(f"Generating slides from {start} to {end}")

            # Generate contents for this batch concurrently
            content_tasks = [
                get_slide_content_from_type_and_outline(
                    slide_layouts[i],
                    presentation_outlines.slides[i],
                    language_to_use,
                    request.tone.value,
                    request.verbosity.value,
                    request.instructions,
                    slide_number=i + 1,
                    disconnect_checker=disconnect_checker,
                    web_search=request.web_search,
                )
                for i in range(start, end)
            ]
            batch_contents: List[dict] = await asyncio.gather(*content_tasks)

            # Build slides for this batch
            batch_slides: List[SlideModel] = []
            for offset, slide_content in enumerate(batch_contents):
                i = start + offset
                slide_layout = slide_layouts[i]
                slide = SlideModel(
                    presentation=presentation_id,
                    layout_group=layout_model.name,
                    layout=slide_layout.id,
                    index=i,
                    speaker_note=slide_content.get("__speaker_note__"),
                    content=slide_content,
                    ui=_template_slide_ui(layout_payload, slide_layout.id),
                )
                slides.append(slide)
                batch_slides.append(slide)

            if async_status:
                async_status.data = _presentation_task_progress_data(
                    created_slides=len(slides),
                    remaining_slides=total_slides_to_create - len(slides),
                    presentation_id=presentation_id,
                )
                async_status.updated_at = datetime.now()
                sql_session.add(async_status)
                await sql_session.commit()

            if using_slides_markdown:
                image_urls_for_batch = get_images_for_slides_from_outline(
                    presentation_outlines.slides[start:end]
                )
            else:
                image_urls_for_batch = [[] for _ in batch_slides]

            # Start asset fetch tasks immediately so they run in parallel with next batch's LLM calls
            asset_tasks = [
                asyncio.create_task(
                    process_slide_and_fetch_assets(
                        image_generation_service,
                        slide,
                        outline_image_urls=image_urls_for_batch[offset],
                        icon_weight=layout_model.icon_weight,
                        allow_image_fallback=True,
                        image_warnings=image_warnings,
                        web_search=request.web_search,
                        deck_image_context=deck_image_search_context,
                    )
                )
                for offset, slide in enumerate(batch_slides)
            ]
            async_assets_generation_tasks.extend(asset_tasks)

        if async_status:
            async_status.message = "Fetching assets for slides"
            async_status.data = _presentation_task_progress_data(
                created_slides=len(slides),
                remaining_slides=0,
                presentation_id=presentation_id,
            )
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        # Run all asset tasks concurrently while batches may still be generating content
        generated_assets_list = await asyncio.gather(*async_assets_generation_tasks)
        generated_assets = []
        for assets_list in generated_assets_list:
            generated_assets.extend(assets_list)
        for warning in image_warnings:
            logger.warning(
                "Slide image generation warning: presentation_id=%s detail=%s",
                presentation_id,
                warning.get("detail"),
            )

        for slide in slides:
            _hydrate_template_slide_ui(slide, layout_payload)
            slide.ui = with_generation_font(slide.ui)

        # 8. Save PresentationModel and Slides
        sql_session.add(presentation)
        sql_session.add_all(slides)
        sql_session.add_all(generated_assets)
        await sql_session.commit()

        if async_status:
            async_status.message = "Exporting presentation"
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)

        # 9. Export
        presentation_and_path = await export_presentation(
            presentation_id,
            presentation.title or str(uuid.uuid4()),
            request.export_as,
            cookie_header=export_cookie_header,
        )

        response = PresentationPathAndEditPath(
            **presentation_and_path.model_dump(),
            edit_path=f"/presentation?id={presentation_id}",
        )

        if async_status:
            async_status.message = "Presentation generation completed"
            async_status.status = AsyncTaskStatus.COMPLETED
            async_status.data = {
                **_presentation_task_progress_data(
                    created_slides=len(slides),
                    remaining_slides=0,
                    presentation_id=presentation_id,
                ),
                # The same fields the synchronous endpoint returns in its
                # response body. Without them a polling caller learns only
                # that *a* task finished, with no way to reach the file it
                # produced -- the result was previously reachable only by
                # receiving the completion webhook.
                "path": response.path,
                "edit_path": response.edit_path,
            }
            async_status.updated_at = datetime.now()
            sql_session.add(async_status)
            await sql_session.commit()

        # Triggering webhook on success
        CONCURRENT_SERVICE.run_task(
            None,
            WebhookService.send_webhook,
            WebhookEvent.PRESENTATION_GENERATION_COMPLETED,
            response.model_dump(mode="json"),
        )

        return response

    except Exception as e:
        if not isinstance(e, HTTPException):
            traceback.print_exc()
            e = CodedHTTPException(
                status_code=500,
                detail="Presentation generation failed",
                error_code="error.generation_failed",
            )

        api_error_model = APIErrorModel.from_exception(e)

        # Triggering webhook on failure
        CONCURRENT_SERVICE.run_task(
            None,
            WebhookService.send_webhook,
            WebhookEvent.PRESENTATION_GENERATION_FAILED,
            api_error_model.model_dump(mode="json"),
        )

        if async_status:
            async_status.status = AsyncTaskStatus.ERROR
            async_status.message = "Presentation generation failed"
            async_status.updated_at = datetime.now()
            async_status.error = api_error_model.model_dump(mode="json")
            sql_session.add(async_status)
            await sql_session.commit()

        else:
            raise e


