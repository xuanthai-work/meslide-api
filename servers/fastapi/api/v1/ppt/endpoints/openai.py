from typing import Annotated, List
from fastapi import APIRouter, Body, HTTPException

from utils.available_models import (
    ModelAvailabilityError,
    list_available_openai_compatible_models,
)
from utils.coded_exception import CodedHTTPException
from utils.provider_error_messages import classify_raw_provider_error

OPENAI_ROUTER = APIRouter(prefix="/openai", tags=["OpenAI"])


@OPENAI_ROUTER.post("/models/available", response_model=List[str])
async def get_available_models(
    url: Annotated[str, Body()],
    api_key: Annotated[str, Body()],
):
    try:
        return await list_available_openai_compatible_models(url, api_key)
    except ModelAvailabilityError as e:
        classification = classify_raw_provider_error(
            getattr(e, "provider", "OpenAI-compatible provider"),
            "model validation",
            status_code=getattr(e, "provider_status_code", None),
            message=getattr(e, "raw_message", None),
        )
        if classification is not None:
            error_code, error_params = classification
            raise CodedHTTPException(
                status_code=e.status_code,
                detail=str(e),
                error_code=error_code,
                error_params=error_params,
            )
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        classification = classify_raw_provider_error(
            "OpenAI-compatible provider",
            "model validation",
            status_code=getattr(e, "status_code", None),
            message=str(e),
        )
        if classification is not None:
            error_code, error_params = classification
            raise CodedHTTPException(
                status_code=500,
                detail=str(e),
                error_code=error_code,
                error_params=error_params,
            )
        raise HTTPException(status_code=500, detail=str(e))
