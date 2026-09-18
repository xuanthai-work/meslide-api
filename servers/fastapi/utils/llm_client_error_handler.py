from fastapi import HTTPException
from openai import APIError as OpenAIAPIError
from google.genai.errors import APIError as GoogleAPIError
import traceback

from utils.coded_exception import CodedHTTPException
from utils.image_generation_error import openai_error_classification
from utils.provider_error_messages import (
    classify_raw_provider_error,
    safe_llm_provider_error_detail,
)


def handle_llm_client_exceptions(e: Exception) -> HTTPException:
    traceback.print_exc()
    if isinstance(e, HTTPException):
        return e
    if isinstance(e, OpenAIAPIError):
        status_code = getattr(e, "status_code", None) or 500
        detail, error_code, error_params = openai_error_classification(
            e, operation="API request"
        )
        return CodedHTTPException(
            status_code=status_code,
            detail=detail,
            error_code=error_code,
            error_params=error_params,
        )
    if isinstance(e, GoogleAPIError):
        status_code = (
            getattr(e, "code", None)
            or getattr(e, "status_code", None)
            or 500
        )
        message = f"Google API error: {getattr(e, 'message', None) or str(e)}"
        detail = safe_llm_provider_error_detail(
            status_code=status_code,
            message=message,
        )
        classification = classify_raw_provider_error(
            "Google",
            "API request",
            status_code=status_code,
            message=message,
        )
        if classification is not None:
            error_code, error_params = classification
            return CodedHTTPException(
                status_code=500,
                detail=detail,
                error_code=error_code,
                error_params=error_params,
            )
        return HTTPException(
            status_code=500,
            detail=detail,
        )
    return HTTPException(
        status_code=500,
        detail=safe_llm_provider_error_detail(message=f"LLM API error: {e}"),
    )


__all__ = ["handle_llm_client_exceptions"]
