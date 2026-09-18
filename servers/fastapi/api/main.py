import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import FileResponse

from api.lifespan import app_lifespan
from api.middlewares import SessionAuthMiddleware
from api.v1.async_tasks.router import API_V1_ASYNC_TASKS_ROUTER
from api.v1.auth.router import API_V1_AUTH_ROUTER
from api.v1.mock.router import API_V1_MOCK_ROUTER
from api.v1.ppt.router import API_V1_PPT_ROUTER
from api.v1.settings.router import API_V1_SETTINGS_ROUTER
from api.v1.webhook.router import API_V1_WEBHOOK_ROUTER
from utils.coded_exception import register_coded_exception_handler
from utils.get_env import get_app_data_directory_env
from utils.mime_types import init_sandbox_safe_mimetypes
from utils.path_helpers import get_resource_path


init_sandbox_safe_mimetypes()


app = FastAPI(lifespan=app_lifespan)

# Routers
app.include_router(API_V1_PPT_ROUTER)
app.include_router(API_V1_WEBHOOK_ROUTER)
app.include_router(API_V1_MOCK_ROUTER)
app.include_router(API_V1_AUTH_ROUTER)
app.include_router(API_V1_SETTINGS_ROUTER)
app.include_router(API_V1_ASYNC_TASKS_ROUTER)

# Serialize CodedHTTPException with error_code/error_params so clients can
# localize errors without matching English prose.
register_coded_exception_handler(app)

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
