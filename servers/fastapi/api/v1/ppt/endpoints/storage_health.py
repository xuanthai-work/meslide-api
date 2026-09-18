"""Object-storage readiness endpoint.

Returns 200 with backend/bucket info when storage is usable, and a stable
503 (``error.storage.unavailable``) when the S3 backend is unreachable or
the bucket is missing — never a vague 500.
"""

from fastapi import APIRouter

from services.object_storage.readiness import check_object_storage_readiness
from utils.coded_exception import CodedHTTPException

STORAGE_HEALTH_ROUTER = APIRouter()


@STORAGE_HEALTH_ROUTER.get("/health/storage")
async def get_storage_health():
    result = await check_object_storage_readiness()

    if not result.get("available"):
        raise CodedHTTPException(
            status_code=503,
            detail=(
                "Object storage is unavailable"
                if result.get("reason") == "unavailable"
                else "Object storage bucket is missing"
            ),
            error_code="error.storage.unavailable",
        )

    return result