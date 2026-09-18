from fastapi import APIRouter, HTTPException, Request

from models.prompt_rewrite import PromptRewriteRequest, PromptRewriteResponse
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.coded_exception import CodedHTTPException
from utils.llm_calls.rewrite_presentation_prompt import rewrite_presentation_prompt

PROMPT_ROUTER = APIRouter(prefix="/prompt", tags=["Prompt"])

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
        disconnect_checker=request.is_disconnected,
    )
    return PromptRewriteResponse(rewritten_prompt=rewritten_prompt)
