"""Persistence CRUD, export, edit, and derive endpoints.

Extracted from ``api.v1.ppt.endpoints.presentation`` so the router module
keeps generation/prepare/stream orchestration. The sub-router is included
on ``PRESENTATION_ROUTER`` and names are re-exported for existing tests.
"""

from __future__ import annotations

import copy
import logging
from typing import Annotated, Any, List, Literal, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from api.v1.auth.config import SESSION_COOKIE_NAME, read_session_token
from api.v1.auth.context import get_current_owner_id
from api.v1.ppt.endpoints.presentation_layout import _hydrate_template_slide_ui
from api.v1.ppt.endpoints.presentation_responses import _presentation_response_data
from constants.presentation import MAX_NUMBER_OF_SLIDES
from models.presentation_create_request import CreatePresentationRequest
from models.presentation_and_path import PresentationPathAndEditPath
from models.presentation_from_template import EditPresentationRequest
from models.presentation_with_slides import PresentationWithSlides
from models.sql.presentation import PresentationModel, PresentationVersion
from models.sql.slide import SlideModel
from services.database import get_async_session
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.coded_exception import CodedHTTPException
from utils.dict_utils import deep_update
from utils.export_utils import export_presentation
from utils.web_search import get_selected_web_search_provider, get_web_search_route
import uuid

logger = logging.getLogger(__name__)

PRESENTATION_CRUD_ROUTER = APIRouter()

BLANK_PRESENTATION_LAYOUT_GROUP = "blank"
BLANK_PRESENTATION_LAYOUT_ID = "__blank_slide__"
BLANK_PRESENTATION_SLIDE_UI: dict[str, Any] = {
    "id": BLANK_PRESENTATION_LAYOUT_ID,
    "description": "Empty slide.",
    "background": "#FFFFFF",
    "components": [],
    "elements": [
        {
            "type": "vector",
            "shape": "polygon",
            "points": [
                {"x": 0, "y": 0},
                {"x": 1280, "y": 0},
                {"x": 1280, "y": 720},
                {"x": 0, "y": 720},
            ],
            "closed": True,
            "fill": {"color": "#FFFFFF"},
            "decorative": True,
        }
    ],
}

def _blank_presentation_slide_ui() -> dict[str, Any]:
    return copy.deepcopy(BLANK_PRESENTATION_SLIDE_UI)


def _build_export_cookie_header(request: Request) -> Optional[str]:
    cookie_header = (request.headers.get("cookie") or "").strip()
    if cookie_header:
        return cookie_header

    internal_session_token = getattr(
        request.state, "internal_session_token", None
    )
    if isinstance(internal_session_token, str) and internal_session_token:
        return f"{SESSION_COOKIE_NAME}={internal_session_token}"

    session_token = read_session_token(request.cookies)
    if session_token:
        return f"{SESSION_COOKIE_NAME}={session_token}"

    return None

@PRESENTATION_CRUD_ROUTER.get("/all", response_model=List[PresentationWithSlides])
async def get_all_presentations(
    version: Annotated[
        Optional[PresentationVersion],
        Query(description="Only include presentations matching this version."),
    ] = None,
    include_slides: Annotated[
        bool,
        Query(
            description=(
                "Include the first slide for dashboard previews. "
                "Disable this for metadata-only presentation lists."
            )
        ),
    ] = True,
    sql_session: AsyncSession = Depends(get_async_session),
):
    if include_slides:
        query = select(PresentationModel, SlideModel).join(
            SlideModel,
            (SlideModel.presentation == PresentationModel.id) & (SlideModel.index == 0),
        )
    else:
        query = select(PresentationModel)

    if version is not None:
        query = query.where(PresentationModel.version == version)
    query = query.order_by(PresentationModel.created_at.desc())

    results = await sql_session.execute(query)
    if not include_slides:
        return [
            PresentationWithSlides(
                **_presentation_response_data(presentation),
                slides=[],
            )
            for presentation in results.scalars().all()
        ]

    rows = results.all()
    presentations_with_slides = []
    for presentation, first_slide in rows:
        slides = [first_slide]
        presentations_with_slides.append(
            PresentationWithSlides(
                **_presentation_response_data(presentation),
                slides=slides,
            )
        )
    return presentations_with_slides


@PRESENTATION_CRUD_ROUTER.get("/{id}", response_model=PresentationWithSlides)
async def get_presentation(
    id: uuid.UUID,
    request: Request,
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise CodedHTTPException(
        404, "Presentation not found", error_code="error.presentation_not_found"
    )
    slides_result = await sql_session.scalars(
        select(SlideModel)
        .where(SlideModel.presentation == id)
        .order_by(SlideModel.index)
    )
    slides = list(slides_result)
    return PresentationWithSlides(
        **_presentation_response_data(presentation),
        slides=slides,
    )


@PRESENTATION_CRUD_ROUTER.delete("/{id}", status_code=204)
async def delete_presentation(
    id: uuid.UUID, sql_session: AsyncSession = Depends(get_async_session)
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise CodedHTTPException(
        404, "Presentation not found", error_code="error.presentation_not_found"
    )

    await sql_session.delete(presentation)
    await sql_session.commit()


@PRESENTATION_CRUD_ROUTER.post("/{id}/duplicate", response_model=PresentationWithSlides)
async def duplicate_presentation(
    id: uuid.UUID, sql_session: AsyncSession = Depends(get_async_session)
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise CodedHTTPException(
        404, "Presentation not found", error_code="error.presentation_not_found"
    )

    slides = list(
        await sql_session.scalars(
            select(SlideModel)
            .where(SlideModel.presentation == id)
            .order_by(SlideModel.index)
        )
    )
    new_presentation = presentation.get_new_presentation()
    if new_presentation.title:
        new_presentation.title = f"{new_presentation.title} (Copy)"
    new_slides = [slide.get_new_slide(new_presentation.id) for slide in slides]

    sql_session.add(new_presentation)
    sql_session.add_all(new_slides)
    await sql_session.commit()
    await sql_session.refresh(new_presentation)

    return PresentationWithSlides(
        **_presentation_response_data(new_presentation),
        slides=new_slides,
    )


@PRESENTATION_CRUD_ROUTER.post("/{id}/export", response_model=PresentationPathAndEditPath)
async def export_existing_presentation(
    id: uuid.UUID,
    request_http: Request,
    export_as: Annotated[Literal["pptx", "pdf"], Body(embed=True)] = "pptx",
    sql_session: AsyncSession = Depends(get_async_session),
):
    """Export a presentation that already exists.

    Until now export could only happen as a side effect of the endpoint that
    created a presentation (`/generate`, `/edit`, `/derive`). If that request
    was interrupted — a reverse proxy closing a long-running connection, a
    client disconnect, a browser tab closing — the presentation itself was
    still generated and stored, but the exported file could no longer be
    reached through the API at all. The work was done and paid for, and the
    only way back to it was generating the whole thing again.

    This exposes the same `export_presentation` helper those endpoints already
    call, keyed on an existing presentation id, so an interrupted export can be
    retried without regenerating. It also makes the self-hosted v1 API
    consistent with the hosted v3 API, which offers `POST /presentation/export`.
    """
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise CodedHTTPException(
        404, "Presentation not found", error_code="error.presentation_not_found"
    )

    presentation_and_path = await export_presentation(
        presentation.id,
        presentation.title or str(uuid.uuid4()),
        export_as,
        cookie_header=_build_export_cookie_header(request_http),
    )

    return PresentationPathAndEditPath(
        **presentation_and_path.model_dump(),
        edit_path=f"/presentation?id={presentation.id}",
    )


@PRESENTATION_CRUD_ROUTER.post("/create", response_model=PresentationModel)
async def create_presentation(
    payload: CreatePresentationRequest,
    sql_session: AsyncSession = Depends(get_async_session),
):

    n_slides = payload.n_slides

    if n_slides is not None and n_slides < 1:
        raise CodedHTTPException(
            status_code=400,
            detail="Number of slides must be greater than 0",
            error_code="error.slides_must_be_positive",
        )

    if n_slides is not None and n_slides > MAX_NUMBER_OF_SLIDES:
        raise CodedHTTPException(
            status_code=400,
            detail=f"Number of slides cannot be greater than {MAX_NUMBER_OF_SLIDES}",
            error_code="error.slides_too_many",
            error_params={"max": MAX_NUMBER_OF_SLIDES},
        )

    if payload.include_table_of_contents and n_slides is not None and n_slides < 3:
        raise CodedHTTPException(
            status_code=400,
            detail="Number of slides cannot be less than 3 if table of contents is included",
            error_code="error.slides_toc_min",
        )

    presentation_id = uuid.uuid4()
    language_to_store = (payload.language or "").strip()
    validated_file_paths = (
        TEMP_FILE_SERVICE.resolve_existing_temp_paths(payload.file_paths)
        if payload.file_paths
        else None
    )
    # DB schema stores an int; 0 is used as internal marker for auto slide count.
    n_slides_to_store = n_slides if n_slides is not None else 0

    presentation = PresentationModel(
        id=presentation_id,
        version=PresentationVersion.V2_STANDARD,
        content=payload.content,
        n_slides=n_slides_to_store,
        language=language_to_store,
        file_paths=validated_file_paths,
        tone=payload.tone.value,
        verbosity=payload.verbosity.value,
        instructions=payload.instructions,
        include_table_of_contents=payload.include_table_of_contents,
        include_title_slide=payload.include_title_slide,
        web_search=payload.web_search,
    )

    sql_session.add(presentation)
    await sql_session.commit()

    search_route, actual_search_provider = get_web_search_route()
    logger.info(
        "Created presentation: id=%s web_search_enabled=%s selected_web_search_provider=%s "
        "web_search_route=%s actual_web_search_provider=%s",
        presentation_id,
        payload.web_search,
        get_selected_web_search_provider().value,
        search_route,
        (
            actual_search_provider.value
            if actual_search_provider
            else ("model-native" if search_route == "native" else "none")
        ),
    )

    return presentation


@PRESENTATION_CRUD_ROUTER.post(
    "/create/blank",
    response_model=PresentationWithSlides,
    status_code=201,
)
async def create_blank_presentation(
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation_id = uuid.uuid4()
    presentation = PresentationModel(
        id=presentation_id,
        version=PresentationVersion.V2_STANDARD,
        content="",
        n_slides=1,
        language="English",
        title="Untitled Presentation",
        layout=None,
        fonts=None,
        include_table_of_contents=False,
        include_title_slide=False,
    )
    slide = SlideModel(
        presentation=presentation_id,
        layout_group=BLANK_PRESENTATION_LAYOUT_GROUP,
        layout=BLANK_PRESENTATION_LAYOUT_ID,
        index=0,
        content={},
        speaker_note="",
        ui=_blank_presentation_slide_ui(),
    )

    sql_session.add(presentation)
    sql_session.add(slide)
    try:
        await sql_session.commit()
    except Exception:
        await sql_session.rollback()
        raise

    await sql_session.refresh(presentation)
    await sql_session.refresh(slide)

    return PresentationWithSlides(
        **_presentation_response_data(presentation),
        slides=[slide],
    )

@PRESENTATION_CRUD_ROUTER.patch("/update", response_model=PresentationWithSlides)
async def update_presentation(
    id: Annotated[uuid.UUID, Body()],
    n_slides: Annotated[Optional[int], Body()] = None,
    title: Annotated[Optional[str], Body()] = None,
    theme: Annotated[Optional[dict], Body()] = None,
    slides: Annotated[Optional[List[SlideModel]], Body()] = None,
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, id)
    if not presentation:
        raise CodedHTTPException(
        status_code=404,
        detail="Presentation not found",
        error_code="error.presentation_not_found",
    )

    presentation_update_dict = {}
    if n_slides is not None:
        if n_slides < 1:
            raise CodedHTTPException(
                status_code=400,
                detail="Number of slides must be greater than 0",
                error_code="error.slides_must_be_positive",
            )
        if n_slides > MAX_NUMBER_OF_SLIDES:
            raise CodedHTTPException(
                status_code=400,
                detail=f"Number of slides cannot be greater than {MAX_NUMBER_OF_SLIDES}",
                error_code="error.slides_too_many",
                error_params={"max": MAX_NUMBER_OF_SLIDES},
            )
        presentation_update_dict["n_slides"] = n_slides
    if title:
        presentation_update_dict["title"] = title
    if theme or theme is None:
        presentation_update_dict["theme"] = theme

    if presentation_update_dict:
        presentation.sqlmodel_update(presentation_update_dict)
    if slides:
        if len(slides) > MAX_NUMBER_OF_SLIDES:
            raise CodedHTTPException(
                status_code=400,
                detail=f"Number of slides cannot be greater than {MAX_NUMBER_OF_SLIDES}",
                error_code="error.slides_too_many",
                error_params={"max": MAX_NUMBER_OF_SLIDES},
            )
        # Just to make sure id is UUID
        for slide in slides:
            slide.presentation = uuid.UUID(slide.presentation)
            slide.id = uuid.UUID(slide.id)

        await sql_session.execute(
            delete(SlideModel).where(
                SlideModel.presentation == presentation.id,
                SlideModel.owner_id == get_current_owner_id(),
            )
        )
        sql_session.add_all(slides)

    await sql_session.commit()

    response_slides = slides or []
    return PresentationWithSlides(
        **_presentation_response_data(presentation),
        slides=response_slides,
    )


@PRESENTATION_CRUD_ROUTER.patch("/slide_update", response_model=SlideModel)
async def update_presentation_slide(
    slide: Annotated[SlideModel, Body(embed=True)],
    sql_session: AsyncSession = Depends(get_async_session),
):
    try:
        slide_id = uuid.UUID(str(slide.id))
        presentation_id = uuid.UUID(str(slide.presentation))
    except (AttributeError, TypeError, ValueError) as exc:
        raise CodedHTTPException(
            status_code=422,
            detail="Slide and presentation IDs must be valid UUIDs",
            error_code="error.uuid_required",
        ) from exc

    stored_slide = await sql_session.get(SlideModel, slide_id)
    if not stored_slide:
        raise CodedHTTPException(
            status_code=404, detail="Slide not found", error_code="error.slide_not_found"
        )

    if stored_slide.presentation != presentation_id:
        raise CodedHTTPException(
            status_code=400,
            detail="Slide does not belong to the supplied presentation",
            error_code="error.slide_mismatch",
        )

    stored_slide.sqlmodel_update(
        slide.model_dump(
            exclude={"id", "presentation", "index"},
        )
    )
    sql_session.add(stored_slide)
    await sql_session.commit()
    await sql_session.refresh(stored_slide)
    return stored_slide

@PRESENTATION_CRUD_ROUTER.post("/edit", response_model=PresentationPathAndEditPath)
async def edit_presentation_with_new_content(
    request_http: Request,
    data: Annotated[EditPresentationRequest, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, data.presentation_id)
    if not presentation:
        raise CodedHTTPException(
        status_code=404,
        detail="Presentation not found",
        error_code="error.presentation_not_found",
    )

    slides = await sql_session.scalars(
        select(SlideModel).where(SlideModel.presentation == data.presentation_id)
    )

    new_slides = []
    slides_to_delete = []
    for each_slide in slides:
        updated_content = None
        new_slide_data = list(
            filter(lambda x: x.index == each_slide.index, data.slides)
        )
        if new_slide_data:
            updated_content = deep_update(each_slide.content, new_slide_data[0].content)
            new_slide = each_slide.get_new_slide(presentation.id, updated_content)
            _hydrate_template_slide_ui(new_slide, presentation.layout)
            new_slides.append(new_slide)
            slides_to_delete.append(each_slide.id)

    await sql_session.execute(
        delete(SlideModel).where(
            SlideModel.id.in_(slides_to_delete),
            SlideModel.owner_id == get_current_owner_id(),
        )
    )

    sql_session.add_all(new_slides)
    await sql_session.commit()

    presentation_and_path = await export_presentation(
        presentation.id,
        presentation.title or str(uuid.uuid4()),
        data.export_as,
        cookie_header=_build_export_cookie_header(request_http),
    )

    return PresentationPathAndEditPath(
        **presentation_and_path.model_dump(),
        edit_path=f"/presentation?id={presentation.id}",
    )


@PRESENTATION_CRUD_ROUTER.post("/derive", response_model=PresentationPathAndEditPath)
async def derive_presentation_from_existing_one(
    request_http: Request,
    data: Annotated[EditPresentationRequest, Body()],
    sql_session: AsyncSession = Depends(get_async_session),
):
    presentation = await sql_session.get(PresentationModel, data.presentation_id)
    if not presentation:
        raise CodedHTTPException(
        status_code=404,
        detail="Presentation not found",
        error_code="error.presentation_not_found",
    )

    slides = await sql_session.scalars(
        select(SlideModel).where(SlideModel.presentation == data.presentation_id)
    )

    new_presentation = presentation.get_new_presentation()
    new_slides = []
    for each_slide in slides:
        updated_content = None
        new_slide_data = list(
            filter(lambda x: x.index == each_slide.index, data.slides)
        )
        if new_slide_data:
            updated_content = deep_update(each_slide.content, new_slide_data[0].content)
        new_slide = each_slide.get_new_slide(new_presentation.id, updated_content)
        _hydrate_template_slide_ui(new_slide, new_presentation.layout)
        new_slides.append(new_slide)

    sql_session.add(new_presentation)
    sql_session.add_all(new_slides)
    await sql_session.commit()

    presentation_and_path = await export_presentation(
        new_presentation.id,
        new_presentation.title or str(uuid.uuid4()),
        data.export_as,
        cookie_header=_build_export_cookie_header(request_http),
    )

    return PresentationPathAndEditPath(
        **presentation_and_path.model_dump(),
        edit_path=f"/presentation?id={new_presentation.id}",
    )
