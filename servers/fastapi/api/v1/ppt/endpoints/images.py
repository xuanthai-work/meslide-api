from io import BytesIO
from fastapi import APIRouter, Depends, File, UploadFile, HTTPException
from PIL import Image, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from models.image_prompt import ImagePrompt
from models.sql.image_asset import ImageAsset
from services.database import get_async_session
from services.image_generation_service import ImageGenerationService
from services.image_search.metrics import hash_query_for_log
from utils.coded_exception import CodedHTTPException
from utils.asset_directory_utils import (
    build_image_storage_key,
    filesystem_image_path_to_app_data_url,
    get_images_directory,
    is_image_storage_key,
    normalize_slide_asset_url,
    resolve_app_path_to_filesystem,
)
from utils.file_utils import get_file_name_with_random_uuid
from services.object_storage import get_object_storage, get_object_storage_backend
from services.object_storage.errors import ObjectStorageError
from services.object_storage.factory import OBJECT_STORAGE_S3_BACKEND
from api.v1.auth.context import get_current_owner_id
import mimetypes
import os
import uuid

IMAGES_ROUTER = APIRouter(prefix="/images", tags=["Images"])

ALLOWED_UPLOAD_IMAGE_EXTENSIONS = {
    ".avif",
    ".bmp",
    ".gif",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}
ALLOWED_UPLOAD_IMAGE_FORMATS = {
    "AVIF",
    "BMP",
    "GIF",
    "JPEG",
    "PNG",
    "TIFF",
    "WEBP",
}


async def _read_validated_image_upload(file: UploadFile) -> bytes:
    filename = file.filename or ""
    extension = os.path.splitext(filename)[1].lower()
    if extension not in ALLOWED_UPLOAD_IMAGE_EXTENSIONS:
        accepted = ", ".join(sorted(ALLOWED_UPLOAD_IMAGE_EXTENSIONS))
        raise CodedHTTPException(
            status_code=400,
            detail=f"Invalid image file type. Accepted types: {accepted}",
            error_code="error.image.invalid_type",
            error_params={"accepted": accepted},
        )

    content = await file.read()
    if not content:
        raise CodedHTTPException(
            status_code=400,
            detail="Uploaded image file is empty",
            error_code="error.image.empty_file",
        )

    try:
        with Image.open(BytesIO(content)) as image:
            if image.format not in ALLOWED_UPLOAD_IMAGE_FORMATS:
                raise CodedHTTPException(
                    status_code=400,
                    detail="Uploaded image format is not supported",
                    error_code="error.image.unsupported_format",
                )
            image.verify()
    except HTTPException:
        raise
    except (
        Image.DecompressionBombError,
        OSError,
        SyntaxError,
        UnidentifiedImageError,
        ValueError,
    ):
        raise CodedHTTPException(
            status_code=400,
            detail="Uploaded file is not a valid image",
            error_code="error.image.invalid_image",
        )

    return content


@IMAGES_ROUTER.get("/generate")
async def generate_image(
    prompt: str, sql_session: AsyncSession = Depends(get_async_session)
):
    images_directory = get_images_directory()
    image_prompt = ImagePrompt(prompt=prompt)
    image_generation_service = ImageGenerationService(images_directory)

    image = await image_generation_service.generate_image(image_prompt)
    if not isinstance(image, ImageAsset):
        return normalize_slide_asset_url(image) if isinstance(image, str) else image

    sql_session.add(image)
    await sql_session.commit()

    return filesystem_image_path_to_app_data_url(image.path)


def _image_asset_api_dict(asset: ImageAsset) -> dict:
    return {
        "id": asset.id,
        "created_at": asset.created_at,
        "is_uploaded": asset.is_uploaded,
        "path": asset.path,
        "extras": asset.extras,
        "file_url": filesystem_image_path_to_app_data_url(asset.path),
    }


async def _image_asset_file_is_available(asset: ImageAsset) -> bool:
    """True when the asset still has a fetchable source.

    Remote/data URLs are always served by their own origin. Object-storage keys
    are probed through the configured backend. Local paths must resolve to a
    real file on disk (and be authorized for the current owner), so stale rows
    whose file was wiped by a redeploy never render as broken images.
    """
    path = asset.path
    if not isinstance(path, str) or not path.strip():
        return False
    stripped = path.strip()
    if stripped.startswith(("http://", "https://", "data:", "blob:")):
        return True
    if is_image_storage_key(stripped):
        try:
            return await get_object_storage().exists(stripped)
        except ObjectStorageError:
            return False
    return resolve_app_path_to_filesystem(stripped) is not None


async def _available_image_asset_dicts(assets) -> list[dict]:
    available: list[dict] = []
    for asset in assets:
        if await _image_asset_file_is_available(asset):
            available.append(_image_asset_api_dict(asset))
    return available


@IMAGES_ROUTER.get("/generated")
async def get_generated_images(sql_session: AsyncSession = Depends(get_async_session)):
    try:
        images_result = await sql_session.scalars(
            select(ImageAsset)
            .where(ImageAsset.is_uploaded == False)
            .order_by(ImageAsset.created_at.desc())
        )
        return await _available_image_asset_dicts(images_result)
    except Exception as e:
        raise CodedHTTPException(
            status_code=500,
            detail=f"Failed to retrieve generated images: {str(e)}",
            error_code="error.image.retrieve_generated_failed",
            error_params={"reason": str(e)},
        )


@IMAGES_ROUTER.post("/upload")
async def upload_image(
    file: UploadFile = File(...), sql_session: AsyncSession = Depends(get_async_session)
):
    try:
        content = await _read_validated_image_upload(file)
        new_filename = get_file_name_with_random_uuid(file)
        if get_object_storage_backend() == OBJECT_STORAGE_S3_BACKEND:
            key = build_image_storage_key(
                os.path.basename(new_filename),
                owner_id=get_current_owner_id(),
            )
            await get_object_storage().put_bytes(
                key,
                content,
                content_type=file.content_type
                or mimetypes.guess_type(new_filename)[0]
                or "image/png",
            )
            image_asset = ImageAsset(path=key, is_uploaded=True)
        else:
            image_path = os.path.join(
                get_images_directory(), os.path.basename(new_filename)
            )

            with open(image_path, "wb") as f:
                f.write(content)

            image_asset = ImageAsset(path=image_path, is_uploaded=True)

        sql_session.add(image_asset)
        await sql_session.commit()
        # Refresh to ensure all defaults are loaded
        await sql_session.refresh(image_asset)

        return _image_asset_api_dict(image_asset)
    except HTTPException:
        raise
    except Exception as e:
        raise CodedHTTPException(
            status_code=500,
            detail=f"Failed to upload image: {str(e)}",
            error_code="error.image.upload_failed",
            error_params={"reason": str(e)},
        )


@IMAGES_ROUTER.get("/uploaded")
async def get_uploaded_images(sql_session: AsyncSession = Depends(get_async_session)):
    try:
        images_result = await sql_session.scalars(
            select(ImageAsset)
            .where(ImageAsset.is_uploaded == True)
            .order_by(ImageAsset.created_at.desc())
        )
        return await _available_image_asset_dicts(images_result)
    except Exception as e:
        raise CodedHTTPException(
            status_code=500,
            detail=f"Failed to retrieve uploaded images: {str(e)}",
            error_code="error.image.list_uploaded_failed",
            error_params={"reason": str(e)},
        )


@IMAGES_ROUTER.delete("/{id}", status_code=204)
async def delete_uploaded_image_by_id(
    id: uuid.UUID, sql_session: AsyncSession = Depends(get_async_session)
):
    try:
        # Fetch the asset to get its actual file path
        image = await sql_session.get(ImageAsset, id)
        if not image:
            raise CodedHTTPException(
                status_code=404, detail="Image not found", error_code="error.image.not_found"
            )

        if get_object_storage_backend() == OBJECT_STORAGE_S3_BACKEND or is_image_storage_key(
            image.path
        ):
            await get_object_storage().delete(image.path)
        elif os.path.exists(image.path):
            os.remove(image.path)

        await sql_session.delete(image)
        await sql_session.commit()

    except Exception as e:
        raise CodedHTTPException(
            status_code=500,
            detail=f"Failed to delete image: {str(e)}",
            error_code="error.image.delete_failed",
            error_params={"reason": str(e)},
        )

@IMAGES_ROUTER.get("/search/health")
async def get_image_search_health(probe: bool = False):
    """Operator diagnostics for automatic web image search (plan Phase 8).

    Read-only: reports the rollout gate and SearXNG configuration, and
    optionally (``?probe=true``) performs one fixed benign image search —
    search only, never an import — so operators can verify the instance
    and its image engines without mutating any presentation or asset.

    Always answers 200: this is a diagnostics surface, not a liveness
    gate; failures appear inside the ``probe`` block instead.
    """
    from services.image_search import get_image_search_service
    from services.image_search.availability import (
        get_web_image_search_max_results,
        web_image_search_available,
        web_image_search_configured,
        web_image_search_feature_enabled,
    )
    from utils.searxng import get_searxng_base_url, redact_searxng_url_credentials

    feature_enabled = web_image_search_feature_enabled()
    configured = web_image_search_configured()
    base_url = get_searxng_base_url()

    health: dict = {
        "feature_enabled": feature_enabled,
        "configured": configured,
        "available": web_image_search_available(),
        "base_url": redact_searxng_url_credentials(base_url) if base_url else None,
        "max_results": get_web_image_search_max_results(),
    }

    if not probe:
        return health

    if not feature_enabled or not configured:
        health["probe"] = {
            "ok": False,
            "reason": "not_enabled" if not feature_enabled else "not_configured",
        }
        return health

    # Fixed benign query: the probe must not depend on any user content.
    probe_query = "gslide image search health probe"
    search_stats: dict = {}
    try:
        service = get_image_search_service()
        candidates = await service.search(
            probe_query,
            limit=get_web_image_search_max_results(),
            stats=search_stats,
        )
    except TimeoutError:
        health["probe"] = {"ok": False, "reason": "timeout"}
        return health
    except HTTPException as exc:
        health["probe"] = {"ok": False, "reason": str(exc.detail)[:200]}
        return health
    except Exception as exc:  # noqa: BLE001 — diagnostics report, never raise
        health["probe"] = {"ok": False, "reason": str(exc)[:200]}
        return health

    engines = sorted(
        {candidate.engine for candidate in candidates if candidate.engine}
    )
    health["probe"] = {
        "ok": True,
        "query_hash": hash_query_for_log(probe_query),
        "latency_ms": search_stats.get("fetch_latency_ms"),
        "cached": search_stats.get("cached", False),
        "candidates": len(candidates),
        "engines": engines,
    }
    return health
