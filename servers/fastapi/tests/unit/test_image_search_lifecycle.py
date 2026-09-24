"""Lifecycle and export tests for searched slide images (plan Phase 8).

- Local searched assets survive a presentation reload: the persisted JSON
  slide keeps pointing at its ``/app_data/images`` file, and the file is
  still on disk.
- Duplicates share (not copy) the searched asset files, and deleting a
  presentation never removes them — asset cleanup is owned by the image
  asset surface (``DELETE /images/{id}``), not by presentations.
- Export embeds local searched assets: JSON-rendered slides localize
  ``/app_data/images`` sources into data URIs, and the Node export runtime
  is pointed at the same asset mount.
"""

import asyncio
import base64
import uuid

from api.v1.ppt.endpoints.presentation import (
    delete_presentation,
    duplicate_presentation,
    get_presentation,
)
from models.sql.presentation import PresentationModel, PresentationVersion
from models.sql.slide import SlideModel


class _LifecycleSession:
    """Endpoint-shaped async session fake with seeded rows."""

    def __init__(self, presentations=None, slides=None):
        self.presentations = presentations or {}
        self.slides = slides or []
        self.added = []
        self.added_all = []
        self.deleted = []
        self.commits = 0

    async def get(self, _model, key):
        return self.presentations.get(key)

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added_all.extend(list(objs))

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None

    async def scalars(self, *_args, **_kwargs):
        return list(self.slides)


def _write_searched_asset(tmp_path, name=None):
    """Persist a file the way ``SsrSafeImageImporter._write_atomically`` does
    (``<images dir>/<uuid>.<raster ext>``) and return (path, /app_data URL)."""
    file_name = name or f"{uuid.uuid4()}.png"
    asset_path = tmp_path / "images" / file_name
    asset_path.parent.mkdir(parents=True, exist_ok=True)
    asset_path.write_bytes(b"\x89PNG\r\n\x1a\n searched-bytes")
    asset_url = f"/app_data/images/{file_name}"
    return str(asset_path), asset_url


def _deck_with_searched_image(tmp_path, asset_url):
    from utils.datetime_utils import get_current_utc_datetime

    now = get_current_utc_datetime()
    presentation = PresentationModel(
        id=uuid.uuid4(),
        version=PresentationVersion.V2_STANDARD,
        content="topic",
        n_slides=1,
        language="vi",
        title="Searched deck",
        web_search=True,
        created_at=now,
        updated_at=now,
    )
    slide = SlideModel(
        id=uuid.uuid4(),
        presentation=presentation.id,
        layout_group="group",
        layout="image-left",
        index=0,
        content={"elements": [{"type": "image", "data": asset_url}]},
    )
    return presentation, slide


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


def test_searched_asset_survives_presentation_reload(tmp_path):
    asset_path, asset_url = _write_searched_asset(tmp_path)
    presentation, slide = _deck_with_searched_image(tmp_path, asset_url)
    session = _LifecycleSession(
        presentations={presentation.id: presentation}, slides=[slide]
    )

    response = asyncio.run(get_presentation(presentation.id, object(), session))

    assert response.slides[0].content == slide.content
    assert response.slides[0].content["elements"][0]["data"] == asset_url
    # The underlying local file is still served at that URL.
    import os

    assert os.path.isfile(asset_path)


# ---------------------------------------------------------------------------
# Duplicate / delete: cleanup ownership
# ---------------------------------------------------------------------------


def test_duplicate_presentation_shares_searched_asset_files(tmp_path, monkeypatch):
    asset_path, asset_url = _write_searched_asset(tmp_path)
    presentation, slide = _deck_with_searched_image(tmp_path, asset_url)
    session = _LifecycleSession(
        presentations={presentation.id: presentation}, slides=[slide]
    )

    # ``get_new_presentation`` relies on the database to fill created_at /
    # updated_at defaults; emulate that for the in-memory fake session.
    original_get_new = PresentationModel.get_new_presentation

    def _stamped_get_new(self):
        copy = original_get_new(self)
        copy.created_at = self.created_at
        copy.updated_at = self.updated_at
        return copy

    monkeypatch.setattr(PresentationModel, "get_new_presentation", _stamped_get_new)

    response = asyncio.run(duplicate_presentation(presentation.id, session))

    new_slide = response.slides[0]
    # The copy references the same already-imported local file — duplicate
    # must not re-search, re-import, or re-download anything.
    assert new_slide.content == slide.content
    assert new_slide.content["elements"][0]["data"] == asset_url
    assert session.added[0].id != presentation.id
    assert session.commits == 1

    import os

    assert os.path.isfile(asset_path)


def test_delete_presentation_keeps_searched_asset_files(tmp_path):
    asset_path, _asset_url = _write_searched_asset(tmp_path)
    presentation, slide = _deck_with_searched_image(tmp_path, _asset_url)
    session = _LifecycleSession(
        presentations={presentation.id: presentation}, slides=[slide]
    )

    asyncio.run(delete_presentation(presentation.id, session))

    # Presentation deletion removes DB rows only. Searched image files are
    # owner assets — their lifecycle belongs to the image asset surface,
    # so already-imported images (and other decks sharing them) survive.
    assert session.deleted == [presentation]
    assert session.commits == 1

    import os

    assert os.path.isfile(asset_path)


# ---------------------------------------------------------------------------
# Export embeds local searched assets
# ---------------------------------------------------------------------------


def test_json_export_localizes_searched_asset_into_data_uri(tmp_path, monkeypatch):
    from services import export_image_assets, export_task_service

    asset_path, asset_url = _write_searched_asset(tmp_path)
    url_to_file = {asset_url: asset_path}
    monkeypatch.setattr(
        export_image_assets,
        "resolve_app_path_to_filesystem",
        lambda value: url_to_file.get(value),
    )

    localized = export_task_service._localize_json_image_assets(
        [{"type": "image", "data": asset_url}]
    )

    data_uri = localized[0]["data"]
    assert data_uri.startswith("data:image/png;base64,")
    decoded = base64.b64decode(data_uri.split(",", 1)[1])
    assert decoded == (tmp_path / "images" / asset_url.rsplit("/", 1)[1]).read_bytes()


def test_json_export_leaves_remote_and_unresolvable_sources_alone(tmp_path, monkeypatch):
    from services import export_image_assets, export_task_service

    monkeypatch.setattr(
        export_image_assets, "resolve_app_path_to_filesystem", lambda value: None
    )

    payload = [
        {"type": "image", "data": "https://remote.example/a.jpg"},
        {"type": "image", "data": "/app_data/images/missing.png"},
        {"type": "text", "data": "headline"},
    ]
    localized = export_task_service._localize_json_image_assets(payload)
    assert localized[0]["data"] == "https://remote.example/a.jpg"
    assert localized[1]["data"] == "/app_data/images/missing.png"
    assert localized[2] == {"type": "text", "data": "headline"}


def test_export_runtime_env_points_the_renderer_at_app_data(monkeypatch, tmp_path):
    """The headless export renderer resolves searched assets through the same
    ``/app_data`` mount that served them in the editor."""
    from services import export_task_service

    monkeypatch.setattr(
        export_task_service, "get_app_data_directory_env", lambda: str(tmp_path)
    )
    monkeypatch.setattr(
        export_task_service, "get_temp_directory_env", lambda: str(tmp_path / "temp")
    )

    env = export_task_service.ExportTaskService()._build_node_env()

    assert env["APP_DATA_DIRECTORY"] == str(tmp_path)
    assert env["ASSETS_BASE_URL"] == "/app_data"
