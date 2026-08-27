from fastapi import HTTPException
from openai import APIError as OpenAIAPIError
from google.genai.errors import APIError as GoogleAPIError
import traceback

from utils.image_generation_error import openai_error_detail
from utils.provider_error_messages import safe_llm_provider_error_detail


def handle_llm_client_exceptions(e: Exception) -> HTTPException:
    traceback.print_exc()
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, OpenAIAPIError):
        status_code = getattr(e, "status_code", None) or 500
        detail = openai_error_detail(e, operation="API request")
        return HTTPException(
            status_code=status_code,
            detail=detail,
        )
    if isinstance(e, GoogleAPIError):
        status_code = (
            getattr(e, "code", None)
            or getattr(e, "status_code", None)
            or 500
        )
        return HTTPException(
            status_code=500,
            detail=safe_llm_provider_error_detail(
                status_code=status_code,
                message=f"Google API error: {getattr(e, 'message', None) or str(e)}",
            ),
        )
    return HTTPException(
        status_code=500,
        detail=safe_llm_provider_error_detail(message=f"LLM API error: {e}"),
    )


__all__ = ["handle_llm_client_exceptions"]
