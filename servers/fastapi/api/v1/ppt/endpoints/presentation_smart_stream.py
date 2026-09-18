"""Smart presentation streaming flow.

Extracted from ``api.v1.ppt.endpoints.presentation`` so the router module only
orchestrates; this module owns the Smart-mode SSE pipeline that streams
slide HTML as the Smart generation runs. The router re-exports
``_stream_smart_presentation`` for the stream route and the existing tests.
"""

import asyncio
import json
import logging
from typing import Any, List, Optional
from fastapi import (
    HTTPException,
    Request,
)
from fastapi.responses import StreamingResponse
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from models.presentation_with_slides import (
    PresentationWithSlides,
)
from services.documents_loader import DocumentsLoader
from services.temp_file_service import TEMP_FILE_SERVICE
from models.sql.slide import SlideModel
from models.sse_response import (
    SSECompleteResponse,
    SSEResponse,
    SSEStatusResponse,
)

from services.image_search.models import DeckResolutionContext
from services.image_search import get_image_search_service
from services.image_search.constants import (
    IMAGE_STATUS_TEXT,
    limits as image_search_limits,
)
from services.image_search.service import FAILURE_PLACEHOLDER_FALLBACK
from services.image_search.smart_html import (
    apply_smart_image_resolutions,
    extract_image_query_slots,
    neutralize_invented_remote_images,
    strip_image_query_markers,
)
from services.image_search.warnings import (
    IMAGE_SEARCH_NOT_CONFIGURED_CODE,
    image_search_warning,
)
from models.sql.image_asset import ImageAsset
from models.sql.presentation import PresentationModel
from utils.asset_directory_utils import (
    filesystem_image_path_to_app_data_url,
)
from utils.process_slides import (
    IMAGE_SEARCH_MODE_GENERATION,
    IMAGE_SEARCH_MODE_SEARCH,
    IMAGE_SEARCH_MODE_UNAVAILABLE,
    resolve_web_image_search_mode,
)
from utils.llm_utils import TextGenerationMetrics
from utils.sse import safe_sse_stream, sse_streaming_response
from utils.web_search import build_web_search_query, get_web_search_context
from api.v1.auth.context import get_current_owner_id
from services.community_presentations import (
    build_community_design_context,
    load_community_references,
    merge_reference_fonts,
)
from utils.llm_calls.generate_smart_presentation import (
    generate_smart_presentation,
    normalize_smart_slide_html,
    resolve_smart_slide_count,
)


logger = logging.getLogger(__name__)
from api.v1.ppt.endpoints.presentation_responses import (  # noqa: I001
    _presentation_response_data,
)


async def _stream_smart_presentation(
    presentation: PresentationModel,
    sql_session: AsyncSession,
    request: Optional[Request] = None,
    regenerate: bool = False,
) -> StreamingResponse:
    presentation_id = presentation.id

    async def inner():
        existing_slides = list(
            await sql_session.scalars(
                select(SlideModel)
                .where(SlideModel.presentation == presentation_id)
                .order_by(SlideModel.index)
            )
        )
        if existing_slides and not regenerate:
            for slide in existing_slides:
                yield SSEResponse(
                    event="response",
                    data=json.dumps(
                        {
                            "type": "slide_html",
                            "index": slide.index,
                            "slide_id": str(slide.id),
                            "html": slide.html_content,
                            "slide": slide.model_dump(mode="json"),
                        }
                    ),
                ).to_string()
            response = PresentationWithSlides(
                **_presentation_response_data(presentation),
                slides=existing_slides,
            )
            yield SSECompleteResponse(
                key="presentation",
                value=response.model_dump(mode="json"),
            ).to_string()
            return

        yield SSEStatusResponse(
            status="Preparing Smart presentation",
            status_code="smart.preparing",
        ).to_string()
        references = await load_community_references(
            presentation.community_design_ids
        )
        community_context = build_community_design_context(references)
        reference_fonts = merge_reference_fonts(references)

        source_parts: list[str] = []
        if presentation.file_paths:
            yield SSEStatusResponse(
                status="Reading source documents",
                status_code="smart.reading_documents",
            ).to_string()
            documents_loader = DocumentsLoader(
                file_paths=presentation.file_paths,
                presentation_language=presentation.language,
            )
            await documents_loader.load_documents(
                TEMP_FILE_SERVICE.create_temp_dir()
            )
            source_parts.extend(document for document in documents_loader.documents if document)

        if presentation.web_search:
            yield SSEStatusResponse(
                status="Searching the web",
                status_code="smart.searching_web",
            ).to_string()
            search_context = await get_web_search_context(
                build_web_search_query(
                    presentation.content,
                    presentation.instructions,
                )
            )
            if search_context:
                source_parts.append(search_context)

        source_context = "\n\n".join(source_parts)
        if len(source_context) > 90_000:
            source_context = source_context[:90_000]

        # Image-search slots (Phase 6): web_search=true + rollout flag on.
        # The prompt makes the LLM emit placeholder <img data-image-query="...">
        # tags; each streamed slide keeps its placeholder HTML for fast
        # streaming while a bounded background task searches and imports.
        smart_image_search_mode = resolve_web_image_search_mode(
            presentation.web_search
        )
        smart_image_search_service = (
            get_image_search_service()
            if smart_image_search_mode == IMAGE_SEARCH_MODE_SEARCH
            else None
        )
        smart_image_context = DeckResolutionContext()
        smart_image_semaphore = asyncio.Semaphore(
            image_search_limits.max_concurrent_imports_per_presentation
        )
        smart_image_assets: List[ImageAsset] = []
        smart_image_warnings: dict[int, List[dict]] = {}
        resolved_slide_html: dict[int, str] = {}
        smart_asset_tasks: dict[int, asyncio.Task] = {}
        smart_image_status_emitted: set[str] = set()

        def smart_image_status(code: str) -> None:
            """Progress codes from the search service land on the event queue."""
            generation_events.put_nowait(("image_status", code))

        slide_count = resolve_smart_slide_count(presentation.n_slides)
        presentation.n_slides = slide_count
        # A Standard deck can be switched back later; keep its template font
        # map when Smart mode has no explicit community-reference override.
        presentation.fonts = reference_fonts or presentation.fonts or {
            "Inter": "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap"
        }
        yield SSEResponse(
            event="response",
            data=json.dumps({"type": "fonts", "fonts": presentation.fonts}),
        ).to_string()
        yield SSEStatusResponse(
            status=(
                "Applying community design reference"
                if references
                else "Designing the complete presentation"
            ),
            status_code=(
                "smart.applying_community"
                if references
                else "smart.designing_complete"
            ),
        ).to_string()
        streamed_slides: dict[int, SlideModel] = {}
        generation_events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        smart_content = presentation.content
        if presentation.outlines:
            approved_outline = presentation.get_presentation_outline()
            outline_text = "\n\n".join(
                f"## Slide {index + 1}\n{slide.content}"
                for index, slide in enumerate(approved_outline.slides)
            )
            smart_content = (
                f"{presentation.content}\n\n"
                "Use the following approved slide outline as the required content "
                f"and ordering for the deck:\n\n{outline_text}"
            )

        async def emit_slide(index: int, slide: dict[str, str]) -> None:
            if index < 0 or index >= slide_count:
                return
            streamed_slide = streamed_slides.get(index)
            if streamed_slide is None:
                streamed_slide = SlideModel(
                    presentation=presentation_id,
                    layout_group="smart-html",
                    layout="smart-html",
                    index=index,
                    content={"title": slide["title"]},
                    html_content=slide["html"],
                    speaker_note="",
                )
                streamed_slides[index] = streamed_slide
            else:
                streamed_slide.content = {"title": slide["title"]}
                streamed_slide.html_content = slide["html"]
                streamed_slide.speaker_note = ""
            await generation_events.put(("slide", streamed_slide))

        async def emit_metrics(metrics: TextGenerationMetrics) -> None:
            await generation_events.put(("metrics", metrics))

        async def resolve_smart_slide_images(index: int, slide: SlideModel) -> None:
            """Search/import the slide's image slots, then emit slide_assets.

            Failed slots keep the placeholder plus a coded warning; every
            data-image-query marker is stripped before the update is
            stored or streamed. HTML is re-sanitized after replacement.
            """
            html = slide.html_content
            try:
                if smart_image_search_mode == IMAGE_SEARCH_MODE_UNAVAILABLE:
                    # No SearXNG behind the flag, but the model may still have
                    # invented remote URLs following the search-mode prompt.
                    base_html = neutralize_invented_remote_images(html)
                    slots = extract_image_query_slots(base_html)
                    if not slots and base_html == html:
                        return
                    updated = strip_image_query_markers(base_html)
                    warnings = [
                        image_search_warning(IMAGE_SEARCH_NOT_CONFIGURED_CODE)
                    ]
                else:
                    # The prompt forbids remote URLs; enforce it anyway.
                    base_html = neutralize_invented_remote_images(html)
                    slots = extract_image_query_slots(base_html)[
                        : image_search_limits.max_slots_per_smart_slide
                    ]
                    outcomes = []
                    if slots:

                        async def bounded_search(slot_query: str):
                            async with smart_image_semaphore:
                                return await smart_image_search_service.search_and_import(
                                    slot_query,
                                    context=smart_image_context,
                                    on_status=smart_image_status,
                                )

                        outcomes = list(
                            await asyncio.gather(
                                *[bounded_search(slot.query) for slot in slots]
                            )
                        )
                    warnings = [
                        image_search_warning(outcome.failure_code)
                        for outcome in outcomes
                        if not outcome.ok
                    ]
                    smart_image_assets.extend(
                        outcome.asset for outcome in outcomes if outcome.ok
                    )
                    resolutions = [
                        (
                            slot,
                            (
                                filesystem_image_path_to_app_data_url(
                                    outcome.asset.path
                                )
                                if outcome.ok
                                else None
                            ),
                        )
                        for slot, outcome in zip(slots, outcomes)
                    ]
                    updated = strip_image_query_markers(
                        apply_smart_image_resolutions(base_html, resolutions)
                    )
                    if not slots and updated == html:
                        return
                try:
                    updated = normalize_smart_slide_html(updated)
                except HTTPException:
                    logger.warning(
                        "[presentation.stream.smart] image replacement produced "
                        "invalid HTML; keeping placeholders presentation_id=%s "
                        "slide_index=%s",
                        presentation_id,
                        index,
                    )
                    updated = strip_image_query_markers(html)
                slide.html_content = updated
                resolved_slide_html[index] = updated
                if warnings:
                    smart_image_warnings.setdefault(index, []).extend(warnings)
                await generation_events.put(("slide_assets", slide))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "[presentation.stream.smart] image search failed "
                    "presentation_id=%s slide_index=%s",
                    presentation_id,
                    index,
                )
                # Unexpected failure: the slide keeps its placeholder HTML
                # (markers stripped) and the user sees a coded warning
                # instead of silence.
                try:
                    updated = strip_image_query_markers(html)
                    slide.html_content = updated
                    resolved_slide_html[index] = updated
                    smart_image_warnings.setdefault(index, []).append(
                        image_search_warning(FAILURE_PLACEHOLDER_FALLBACK)
                    )
                    await generation_events.put(("slide_assets", slide))
                except Exception:
                    logger.exception(
                        "[presentation.stream.smart] placeholder fallback failed "
                        "presentation_id=%s slide_index=%s",
                        presentation_id,
                        index,
                    )

        def maybe_start_smart_image_task(slide: SlideModel) -> None:
            if smart_image_search_mode == IMAGE_SEARCH_MODE_GENERATION:
                return
            if slide.index in smart_asset_tasks:
                return
            smart_asset_tasks[slide.index] = asyncio.create_task(
                resolve_smart_slide_images(slide.index, slide)
            )

        generation_task = asyncio.create_task(
            generate_smart_presentation(
                content=smart_content,
                n_slides=slide_count,
                language=presentation.language,
                tone=presentation.tone,
                verbosity=presentation.verbosity,
                instructions=presentation.instructions,
                include_title_slide=presentation.include_title_slide,
                include_table_of_contents=presentation.include_table_of_contents,
                source_context=source_context,
                community_design_context=community_context,
                fonts=presentation.fonts,
                on_slide=emit_slide,
                on_metrics=emit_metrics,
                web_search=presentation.web_search,
            )
        )

        try:
            while not generation_task.done() or not generation_events.empty():
                if request is not None and await request.is_disconnected():
                    logger.info(
                        "[presentation.stream.smart] client disconnected "
                        "presentation_id=%s",
                        presentation_id,
                    )
                    raise asyncio.CancelledError
                try:
                    event_type, event_value = await asyncio.wait_for(
                        generation_events.get(), timeout=0.1
                    )
                except asyncio.TimeoutError:
                    continue
                if event_type == "metrics":
                    metrics = event_value
                    if not isinstance(metrics, TextGenerationMetrics):
                        raise TypeError("Invalid Smart generation metrics event")
                    yield SSEResponse(
                        event="response",
                        data=json.dumps(
                            {"type": "generation_metrics", **metrics.to_dict()}
                        ),
                    ).to_string()
                    continue
                if event_type == "image_status":
                    # Progress without blocking the streamed slide text; each
                    # code is reported at most once per presentation.
                    if event_value not in smart_image_status_emitted:
                        smart_image_status_emitted.add(event_value)
                        yield SSEStatusResponse(
                            status=IMAGE_STATUS_TEXT.get(event_value, event_value),
                            status_code=event_value,
                        ).to_string()
                    continue
                if event_type == "slide_assets":
                    assets_slide = event_value
                    if not isinstance(assets_slide, SlideModel):
                        raise TypeError("Invalid Smart slide assets event")
                    yield SSEResponse(
                        event="response",
                        data=json.dumps(
                            {
                                "type": "slide_assets",
                                "slide_index": assets_slide.index,
                                "slide": assets_slide.model_dump(mode="json"),
                                "warnings": smart_image_warnings.get(
                                    assets_slide.index, []
                                ),
                            }
                        ),
                    ).to_string()
                    continue
                streamed_slide = event_value
                if not isinstance(streamed_slide, SlideModel):
                    raise TypeError("Invalid Smart slide event")
                yield SSEResponse(
                    event="response",
                    data=json.dumps(
                        {
                            "type": "slide_html",
                            "index": streamed_slide.index,
                            "slide_id": str(streamed_slide.id),
                            "html": streamed_slide.html_content,
                            "slide": streamed_slide.model_dump(mode="json"),
                        }
                    ),
                ).to_string()
                # Placeholder HTML streams first; the search runs behind it.
                maybe_start_smart_image_task(streamed_slide)
            deck = await generation_task
        except BaseException:
            # Disconnect or failure: cancel everything still in flight.
            # Nothing is committed before this point, so cancellation
            # leaves no orphan ImageAsset rows and no committed slide HTML
            # with unresolved markers.
            if not generation_task.done():
                generation_task.cancel()
            for asset_task in smart_asset_tasks.values():
                if not asset_task.done():
                    asset_task.cancel()
            raise
        finally:
            # Normal completion awaits every asset task here (their
            # slide_assets events are drained right after); disconnect or
            # failure has already cancelled them above.
            await asyncio.gather(
                generation_task,
                *smart_asset_tasks.values(),
                return_exceptions=True,
            )

        # Asset tasks that finished after the pump loop exited pushed their
        # slide_assets events into the queue; stream them before the commit.
        while not generation_events.empty():
            event_type, event_value = generation_events.get_nowait()
            if event_type == "image_status":
                if event_value not in smart_image_status_emitted:
                    smart_image_status_emitted.add(event_value)
                    yield SSEStatusResponse(
                        status=IMAGE_STATUS_TEXT.get(event_value, event_value),
                        status_code=event_value,
                    ).to_string()
                continue
            if event_type != "slide_assets" or not isinstance(
                event_value, SlideModel
            ):
                continue
            yield SSEResponse(
                event="response",
                data=json.dumps(
                    {
                        "type": "slide_assets",
                        "slide_index": event_value.index,
                        "slide": event_value.model_dump(mode="json"),
                        "warnings": smart_image_warnings.get(event_value.index, []),
                    }
                ),
            ).to_string()

        presentation.title = deck["title"]
        slides: list[SlideModel] = []
        for index, slide in enumerate(deck["slides"]):
            final_slide = streamed_slides.get(index)
            if final_slide is None:
                final_slide = SlideModel(
                    presentation=presentation_id,
                    layout_group="smart-html",
                    layout="smart-html",
                    index=index,
                    content={"title": slide["title"]},
                    html_content=slide["html"],
                    speaker_note="",
                )
                yield SSEResponse(
                    event="response",
                    data=json.dumps(
                        {
                            "type": "slide_html",
                            "index": index,
                            "slide_id": str(final_slide.id),
                            "html": final_slide.html_content,
                            "slide": final_slide.model_dump(mode="json"),
                        }
                    ),
                ).to_string()
            else:
                final_slide.content = {"title": slide["title"]}
                final_slide.html_content = slide["html"]
                final_slide.speaker_note = ""
            resolved_html = resolved_slide_html.get(index)
            if resolved_html is not None:
                final_slide.html_content = resolved_html
            elif smart_image_search_mode != IMAGE_SEARCH_MODE_GENERATION:
                # The asset task crashed or never completed: keep the
                # placeholders, but never persist query markers.
                final_slide.html_content = strip_image_query_markers(
                    final_slide.html_content
                )
            slides.append(final_slide)

        await sql_session.execute(
            delete(SlideModel).where(
                SlideModel.presentation == presentation_id,
                SlideModel.owner_id == get_current_owner_id(),
            )
        )
        sql_session.add(presentation)
        sql_session.add_all(slides)
        sql_session.add_all(smart_image_assets)
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
            error_detail="Failed to generate the Smart presentation. Please try again.",
            error_code="error.smart_generation_failed",
            on_error=rollback_stream_session,
        )
    )


