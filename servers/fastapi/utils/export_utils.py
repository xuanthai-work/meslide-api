import os
import logging
from typing import Literal
from urllib.parse import unquote, urlencode
import uuid

from pathvalidate import sanitize_filename

from api.v1.auth.config import LEGACY_SESSION_COOKIE_NAME, SESSION_COOKIE_NAME
from models.presentation_and_path import PresentationAndPath
from utils.filename_utils import safe_export_basename
from services.export_task_service import EXPORT_TASK_SERVICE
from utils.runtime_limits import log_memory


LOGGER = logging.getLogger(__name__)


def _get_export_page_base_url() -> str:
    return (
        (os.getenv("EXPORT_PAGE_BASE_URL") or os.getenv("NEXT_PUBLIC_URL") or "").strip()
        or "http://127.0.0.1"
    )


def _get_next_public_fastapi_url() -> str | None:
    value = (os.getenv("NEXT_PUBLIC_FAST_API") or "").strip()
    return value or None


def _session_token_from_cookie_header(cookie_header: str | None) -> str | None:
    """Extract gslide_session / presenton_session for /pdf-maker?exportSession=."""
    if not cookie_header:
        return None
    for part in cookie_header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name in {SESSION_COOKIE_NAME, LEGACY_SESSION_COOKIE_NAME}:
            token = unquote(value.strip())
            if token:
                return token
    return None


def _build_presentation_export_url(
    presentation_id: uuid.UUID, cookie_header: str | None = None
) -> tuple[str, str | None]:
    params = {"id": str(presentation_id)}
    fastapi_url = _get_next_public_fastapi_url()
    if fastapi_url:
        params["fastapiUrl"] = fastapi_url
    session_token = _session_token_from_cookie_header(cookie_header)
    if session_token:
        params["exportSession"] = session_token
    export_url = f"{_get_export_page_base_url().rstrip('/')}/pdf-maker?{urlencode(params)}"
    if cookie_header:
        export_url = f"{export_url}#{urlencode({'exportCookie': cookie_header})}"
    return (
        export_url,
        fastapi_url,
    )


async def export_presentation(
    presentation_id: uuid.UUID,
    title: str,
    export_as: Literal["pptx", "pdf"],
    cookie_header: str | None = None,
) -> PresentationAndPath:
    log_memory(
        LOGGER,
        "presentation.export.start",
        presentation_id=str(presentation_id),
        export_as=export_as,
    )
    export_url, fastapi_url = _build_presentation_export_url(
        presentation_id, cookie_header
    )
    name = (title or "").strip() or str(uuid.uuid4())
    export_result = await EXPORT_TASK_SERVICE.export_from_url(
        url=export_url,
        title=safe_export_basename(sanitize_filename(name)),
        export_as=export_as,
        fastapi_url=fastapi_url,
        cookie_header=cookie_header,
    )
    log_memory(
        LOGGER,
        "presentation.export.finish",
        presentation_id=str(presentation_id),
        export_as=export_as,
    )
    return PresentationAndPath(
        presentation_id=presentation_id,
        path=export_result.path,
    )
