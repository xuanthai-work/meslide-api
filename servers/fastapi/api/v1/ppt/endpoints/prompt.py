import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.prompt_rewrite import PromptRewriteRequest, PromptRewriteResponse
from models.sql.template_v2 import TemplateV2
from models.template_selection import TemplateSelectionRequest, TemplateSelectionResponse
from services.database import get_async_session
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.coded_exception import CodedHTTPException
from utils.llm_calls.rewrite_presentation_prompt import rewrite_presentation_prompt
from utils.llm_calls.select_presentation_template import (
    TemplateCandidate,
    select_presentation_template,
)

PROMPT_ROUTER = APIRouter(prefix="/prompt", tags=["Prompt"])
LOGGER = logging.getLogger(__name__)

DOCUMENT_NOT_FOUND_ERROR_CODE = "error.prompt_rewrite.document_not_found"
DOCUMENT_INVALID_ERROR_CODE = "error.prompt_rewrite.document_invalid"


def _resolve_document_paths(file_paths):
    """Resolve document paths to coded, client-safe 4xx errors.

    The temp-file service raises plain ``HTTPException``s; clients need
    stable error codes to detect stale server-side files and re-prepare
    their local documents, so every resolution failure is re-raised as a
    coded ``CodedHTTPException`` without changing the status code.
    """
    try:
        return TEMP_FILE_SERVICE.resolve_existing_temp_paths(file_paths)
    except HTTPException as exc:
        if exc.status_code == 404:
            raise CodedHTTPException(
                status_code=404,
                detail=(
                    "One of the documents could not be found on the server. "
                    "Please re-attach your files and try again."
                ),
                error_code=DOCUMENT_NOT_FOUND_ERROR_CODE,
            ) from exc
        raise CodedHTTPException(
            status_code=400,
            detail=(
                "One of the documents is not accessible from this session. "
                "Please re-attach your files and try again."
            ),
            error_code=DOCUMENT_INVALID_ERROR_CODE,
        ) from exc


@PROMPT_ROUTER.post("/rewrite", response_model=PromptRewriteResponse)
async def rewrite_prompt(
    payload: PromptRewriteRequest,
    request: Request,
) -> PromptRewriteResponse:
    """Rewrite a raw user prompt into a presentation-generation brief.

    Validation is fully handled by the request model (FastAPI returns a
    standard ``422`` for invalid payloads) and provider failures propagate as
    the coded ``CodedHTTPException``/``HTTPException`` raised by the LLM layer.
    The endpoint is read-only: it never writes to the database.

    Optional ``file_paths`` name the user's decomposed document texts. They
    are resolved through the owner-scoped temp-file service before the rewrite
    call, so missing, out-of-root, or cross-owner paths fail fast with a
    coded 4xx instead of surfacing mid-rewrite; the resolved paths then ground
    the rewrite in the cached document profile.
    """
    resolved_file_paths = _resolve_document_paths(payload.file_paths)
    rewritten_prompt = await rewrite_presentation_prompt(
        payload.prompt,
        language=payload.language,
        n_slides=payload.n_slides,
        file_paths=resolved_file_paths or None,
        previous_prompt=payload.previous_prompt,
        disconnect_checker=request.is_disconnected,
    )
    return PromptRewriteResponse(rewritten_prompt=rewritten_prompt)


async def load_template_candidates(
    session: AsyncSession, candidate_ids: list[str]
) -> list[TemplateCandidate]:
    """Recheck client IDs against current default templates, preserving picker order."""
    rows = (
        await session.execute(select(TemplateV2).where(TemplateV2.id.in_(candidate_ids)))
    ).scalars().all()
    eligible: dict[str, TemplateCandidate] = {}
    for row in rows:
        layouts = row.layouts
        if isinstance(layouts, dict) and isinstance(layouts.get("layouts"), list):
            legacy_count = len(layouts["layouts"])
        elif isinstance(layouts, list):
            legacy_count = len(layouts)
        else:
            legacy_count = 0
        count = row.layout_count if row.layout_count is not None else legacy_count
        if row.is_default and count > 0:
            eligible[row.id] = TemplateCandidate(
                row.id, row.name, row.description or ""
            )
    return [eligible[template_id] for template_id in candidate_ids if template_id in eligible]


def fallback_template_id(candidates: list[TemplateCandidate]) -> str:
    return next(
        (item.id for item in candidates if item.name.strip().casefold() == "general"),
        candidates[0].id,
    )


@PROMPT_ROUTER.post("/select-template", response_model=TemplateSelectionResponse)
async def select_template(
    payload: TemplateSelectionRequest,
    request: Request,
    sql_session: AsyncSession = Depends(get_async_session),
) -> TemplateSelectionResponse:
    candidates = await load_template_candidates(sql_session, payload.candidate_ids)
    if not candidates:
        raise CodedHTTPException(
            status_code=422,
            detail="No eligible built-in templates are available.",
            error_code="error.template_selection.no_candidates",
        )

    fallback = fallback_template_id(candidates)
    try:
        chosen = await select_presentation_template(
            payload.prompt, candidates, request.is_disconnected
        )
        if chosen not in {candidate.id for candidate in candidates}:
            raise ValueError("Model selected an ineligible template")
    except Exception as exc:
        LOGGER.warning(
            "Template recommendation failed (%s); using fallback", type(exc).__name__
        )
        chosen = fallback

    return TemplateSelectionResponse(template_id=chosen)
