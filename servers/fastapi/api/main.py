import os
import mimetypes

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import FileResponse, Response

from api.health import HEALTH_ROUTER
from api.lifespan import app_lifespan
from api.middlewares import SessionAuthMiddleware
from api.v1.async_tasks.router import API_V1_ASYNC_TASKS_ROUTER
from api.v1.auth.assets import is_app_data_path_authorized
from api.v1.auth.context import get_current_owner_id
from api.v1.auth.router import API_V1_AUTH_ROUTER
from api.v1.mock.router import API_V1_MOCK_ROUTER
from api.v1.ppt.router import API_V1_PPT_ROUTER
from api.v1.settings.router import API_V1_SETTINGS_ROUTER
from api.v1.webhook.router import API_V1_WEBHOOK_ROUTER
from services.object_storage import get_object_storage, get_object_storage_backend
from services.object_storage.errors import ObjectStorageError
from services.object_storage.factory import OBJECT_STORAGE_S3_BACKEND
from utils.asset_directory_utils import resolve_app_path_to_filesystem
from utils.coded_exception import register_coded_exception_handler
from utils.get_env import get_app_data_directory_env, is_disable_auth_enabled
from utils.mime_types import init_sandbox_safe_mimetypes
from utils.path_helpers import get_resource_path


init_sandbox_safe_mimetypes()


app = FastAPI(lifespan=app_lifespan)

# Routers
app.include_router(HEALTH_ROUTER)
app.include_router(API_V1_PPT_ROUTER)
app.include_router(API_V1_WEBHOOK_ROUTER)
app.include_router(API_V1_MOCK_ROUTER)
app.include_router(API_V1_AUTH_ROUTER)
app.include_router(API_V1_SETTINGS_ROUTER)
app.include_router(API_V1_ASYNC_TASKS_ROUTER)

# Serialize CodedHTTPException with error_code/error_params so clients can
# localize errors without matching English prose.
register_coded_exception_handler(app)

# Serve image assets from local disk first, then the object-storage backend.
# Registered before the /app_data mount so it wins for image requests; kept out
# of the OpenAPI schema because it is a browser-facing asset route.
async def serve_app_data_image(image_path: str) -> Response:
    normalized = image_path.replace("\\", "/").lstrip("/")
    app_data_path = f"/app_data/images/{normalized}"
    owner_id = get_current_owner_id()
    authorized = (
        is_app_data_path_authorized(app_data_path, user_id=owner_id)
        if owner_id is not None
        else is_disable_auth_enabled()
    )
    if not authorized:
        raise HTTPException(status_code=404, detail="Image not found")

    # Priority 1: local disk (backward compatible).
    resolved = resolve_app_path_to_filesystem(app_data_path)
    if resolved:
        return FileResponse(resolved)

    # Priority 2: object storage (S3/MinIO).
    if get_object_storage_backend() == OBJECT_STORAGE_S3_BACKEND:
        key = f"images/{normalized}"
        try:
            storage = get_object_storage()
            if await storage.exists(key):
                data = await storage.get_bytes(key)
                media_type = (
                    mimetypes.guess_type(normalized)[0]
                    or "application/octet-stream"
                )
                return Response(
                    content=data,
                    media_type=media_type,
                    headers={
                        "Cache-Control": "public, max-age=31536000, immutable"
                    },
                )
        except ObjectStorageError:
            pass

    raise HTTPException(status_code=404, detail="Image not found")


app.add_api_route(
    "/app_data/images/{image_path:path}",
    serve_app_data_image,
    methods=["GET"],
    include_in_schema=False,
)

# Mount app_data and static assets (direct FastAPI access; nginx also serves /static in Docker).
app_data_dir = get_app_data_directory_env()
if app_data_dir:
    os.makedirs(app_data_dir, exist_ok=True)
    app.mount("/app_data", StaticFiles(directory=app_data_dir), name="app_data")

static_dir = get_resource_path("static")
if os.path.isdir(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

# When the public Next.js origin is available (split origins behind nginx),
# use that exact origin so credentialed requests remain standards-compliant.
# Docker stays same-origin behind nginx; the wildcard fallback preserves
# standalone FastAPI development behavior.
next_public_origin = (os.getenv("NEXT_PUBLIC_URL") or "").strip().rstrip("/")
origins = [next_public_origin] if next_public_origin else ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(SessionAuthMiddleware)


@app.middleware("http")
async def static_icon_fallback_middleware(request: Request, call_next):
    """Serve placeholder when icon paths are missing (e.g. renamed Phosphor icons)."""
    response = await call_next(request)
    if response.status_code != 404:
        return response
    path = request.url.path
    if not path.startswith("/static/icons/"):
        return response
    placeholder = get_resource_path("static/icons/placeholder.svg")
    if not os.path.isfile(placeholder):
        return response
    return FileResponse(placeholder, media_type="image/svg+xml")
