import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import uuid

from api.v1.auth.context import reset_current_owner_id, set_current_owner_id
from models.sql.template_v2 import TemplateV2
from services.object_storage.local import LocalObjectStorage
from services.template_bundles.repository import TemplateBundleRepository
from templates import default_templates


async def _first_stored_key(storage: LocalObjectStorage) -> str | None:
    for path in sorted(storage.root.rglob("*.json.gz")):
        return str(path.relative_to(storage.root)).replace("\\", "/")
    return None


def _component() -> dict[str, Any]:
    return {
        "id": "photo_component",
        "description": "Reusable photo component for a default template.",
        "position": {"x": 10, "y": 20},
        "size": {"width": 320, "height": 180},
        "elements": [
            {
                "type": "image",
                "position": {"x": 0, "y": 0},
                "size": {"width": 320, "height": 180},
                "data": "static/image.png",
                "decorative": False,
                "name": "photo",
                "is_icon": False,
            }
        ],
    }


def _raw_layout() -> dict[str, Any]:
    return {
        "id": "slide_1",
        "description": "Raw slide layout that should not be stored for defaults.",
        "elements": [
            {
                "type": "image",
                "position": {"x": 10, "y": 20},
                "size": {"width": 320, "height": 180},
                "data": "static/image.png",
                "decorative": False,
                "name": "raw_photo",
                "is_icon": False,
            }
        ],
    }


def _write_template_bundle(root: Path, *, include_raw_layouts: bool = False) -> Path:
    template_dir = root / "general"
    static_dir = template_dir / "static"
    static_dir.mkdir(parents=True)
    (static_dir / "image.png").write_bytes(b"image")
    (static_dir / "thumbnail.png").write_bytes(b"thumbnail")
    (template_dir / "template.json").write_text(
        json.dumps(
            {
                "id": "general",
                "name": "General",
                "description": "General purpose default template.",
                "thumbnail": "static/thumbnail.png",
                "fonts": {"Inter": "static/inter.ttf"},
                "layouts": [
                    {
                        "id": "slide_1",
                        "description": "A valid slide layout for default import.",
                        "components": [_component()],
                    }
                ],
                "merged_components": [
                    {
                        "id": "photo_component",
                        "description": "Merged reusable photo component.",
                        "variants": [_component()],
                    }
                ],
                **(
                    {"raw_layouts": {"layouts": [_raw_layout()]}}
                    if include_raw_layouts
                    else {}
                ),
            }
        ),
        encoding="utf-8",
    )
    return template_dir


class _FakeSession:
    def __init__(
        self,
        existing: TemplateV2 | None = None,
        default_templates: list[TemplateV2] | None = None,
    ):
        self.existing = existing
        self.default_templates = default_templates or []
        self.added: list[TemplateV2] = []
        self.deleted: list[TemplateV2] = []
        self.commit_count = 0

    async def get(self, _model: Any, _key: str):
        return self.existing

    def add(self, template: TemplateV2) -> None:
        self.added.append(template)

    async def commit(self) -> None:
        self.commit_count += 1

    async def delete(self, template: TemplateV2) -> None:
        self.deleted.append(template)

    async def execute(self, *_args: Any, **_kwargs: Any):
        templates = self.default_templates

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return templates

        return _Result()


class _SessionContext:
    def __init__(self, session: _FakeSession):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


def test_default_template_import_normalizes_shapes_and_copies_static(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root)
    app_data_dir = tmp_path / "app_data"
    session = _FakeSession()
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert session.commit_count == 1
    assert len(session.added) == 1
    template = session.added[0]
    assert template.id == "general"
    assert template.is_default is True
    assert template.raw_layouts is None
    # PR3 cutover: the payload lives in the uploaded bundle, not the row.
    assert template.bundle_object_key is not None
    bundle = asyncio.run(TemplateBundleRepository().load(template))
    assert list(bundle.layouts) == ["layouts"]
    assert list(bundle.merged_components) == ["components"]
    assert (
        bundle.layouts["layouts"][0]["components"][0]["elements"][0]["data"]
        == "/app_data/templates/general/static/image.png"
    )
    assert (
        bundle.merged_components["components"][0]["variants"][0]["elements"][0][
            "data"
        ]
        == "/app_data/templates/general/static/image.png"
    )
    assert template.assets["thumbnail"] == (
        "/app_data/templates/general/static/thumbnail.png"
    )
    assert template.assets["fonts"] == {
        "Inter": "/app_data/templates/general/static/inter.ttf"
    }
    assert template.assets["images"] == ["/app_data/templates/general/static/image.png"]
    assert (
        app_data_dir / "templates/general/static/image.png"
    ).read_bytes() == b"image"


def test_default_template_import_force_replaces_static_assets(tmp_path, monkeypatch):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root)
    app_data_dir = tmp_path / "app_data"
    old_static_dir = app_data_dir / "templates/general/static"
    old_static_dir.mkdir(parents=True)
    (old_static_dir / "removed-from-bundle.png").write_bytes(b"stale")
    (old_static_dir / "image.png").write_bytes(b"old")
    session = _FakeSession()
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert (old_static_dir / "image.png").read_bytes() == b"image"
    assert not (old_static_dir / "removed-from-bundle.png").exists()


def test_default_template_import_removes_static_directory_removed_from_bundle(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    template_dir = _write_template_bundle(templates_root)
    shutil.rmtree(template_dir / "static")
    app_data_dir = tmp_path / "app_data"
    old_static_dir = app_data_dir / "templates/general/static"
    old_static_dir.mkdir(parents=True)
    (old_static_dir / "removed-from-bundle.png").write_bytes(b"stale")
    session = _FakeSession()
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert not old_static_dir.exists()


def test_default_template_import_uploads_bundle_object(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root)
    app_data_dir = tmp_path / "app_data"
    storage = LocalObjectStorage(root=tmp_path / "object-storage")
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        "services.template_bundles.repository.get_object_storage",
        lambda: storage,
    )
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(_FakeSession()),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    object_key = asyncio.run(_first_stored_key(storage))
    assert object_key is not None
    # Default templates carry the system owner id (get_current_owner_id
    # outside a request context), so the key lands under that owner.
    assert "/templates/general/revisions/" in object_key
    assert object_key.endswith(".json.gz")


def test_default_template_import_updates_existing_database_row(tmp_path, monkeypatch):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root, include_raw_layouts=True)
    app_data_dir = tmp_path / "app_data"
    existing = TemplateV2(
        id="general",
        name="Old name",
        description="Old description",
        raw_layouts={"layouts": [_raw_layout()]},
        layouts={"layouts": []},
        merged_components={"components": []},
        assets={"thumbnail": "/old.png"},
        is_default=False,
    )
    session = _FakeSession(existing=existing)
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert session.commit_count == 1
    assert session.added == [existing]
    assert existing.name == "General"
    assert existing.description == "General purpose default template."
    assert existing.is_default is True
    assert existing.raw_layouts is None
    # PR3 cutover: the seeded payload is re-uploaded as a bundle revision.
    assert existing.bundle_object_key is not None
    assert existing.layouts is None
    assert existing.merged_components is None
    assert existing.assets["thumbnail"] == (
        "/app_data/templates/general/static/thumbnail.png"
    )
    assert (app_data_dir / "templates/general/static/image.png").exists()


def test_default_template_import_clears_legacy_owner_so_catalog_is_shared(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root)
    app_data_dir = tmp_path / "app_data"
    existing = TemplateV2(
        id="general",
        name="Old name",
        owner_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        is_default=True,
    )
    session = _FakeSession(existing=existing)
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert existing.owner_id is None
    assert existing.is_default is True


def test_default_template_import_stays_shared_even_if_owner_context_is_set(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root)
    app_data_dir = tmp_path / "app_data"
    session = _FakeSession()
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    token = set_current_owner_id(uuid.uuid4())
    try:
        asyncio.run(
            default_templates.import_default_templates_on_startup(templates_root)
        )
    finally:
        reset_current_owner_id(token)

    assert len(session.added) == 1
    assert session.added[0].owner_id is None
    assert session.added[0].is_default is True


def test_default_template_import_removes_stale_default_database_rows(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    _write_template_bundle(templates_root)
    app_data_dir = tmp_path / "app_data"
    stale_template_dir = app_data_dir / "templates/legacy-default/static"
    stale_template_dir.mkdir(parents=True)
    (stale_template_dir / "old.png").write_bytes(b"stale")
    stale_template = TemplateV2(
        id="legacy-default",
        name="Legacy Default",
        layouts={"layouts": []},
        is_default=True,
    )
    session = _FakeSession(default_templates=[stale_template])
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert session.commit_count == 2
    assert session.deleted == [stale_template]
    assert not (app_data_dir / "templates/legacy-default").exists()
    assert (app_data_dir / "templates/general/static/image.png").exists()


def test_default_template_import_removes_all_defaults_when_bundle_is_empty(
    tmp_path,
    monkeypatch,
):
    templates_root = tmp_path / "templates"
    templates_root.mkdir()
    app_data_dir = tmp_path / "app_data"
    stale_template_dir = app_data_dir / "templates/legacy-default/static"
    stale_template_dir.mkdir(parents=True)
    (stale_template_dir / "old.png").write_bytes(b"stale")
    stale_template = TemplateV2(
        id="legacy-default",
        name="Legacy Default",
        layouts={"layouts": []},
        is_default=True,
    )
    session = _FakeSession(default_templates=[stale_template])
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data_dir))
    monkeypatch.setattr(
        default_templates,
        "async_session_maker",
        lambda: _SessionContext(session),
    )

    asyncio.run(default_templates.import_default_templates_on_startup(templates_root))

    assert session.commit_count == 1
    assert session.added == []
    assert session.deleted == [stale_template]
    assert not (app_data_dir / "templates/legacy-default").exists()


def test_bundled_general_template_json_matches_template_v2_shapes():
    template_dir = Path(__file__).resolve().parents[4] / "templates" / "general"

    template = default_templates._load_default_template(template_dir)

    template_id = "general"
    assert template.id == template_id
    assert template.is_default is True
    assert list(template.layouts) == ["layouts"]
    assert len(template.layouts["layouts"]) > 0
    assert list(template.merged_components) == ["components"]
    assert len(template.merged_components["components"]) > 0
    assert template.assets["thumbnail"] == (
        f"/app_data/templates/{template_id}/static/thumbnail.png"
    )


def test_resolve_default_template_id_maps_public_name_to_json_id():
    templates_root = Path(__file__).resolve().parents[4] / "templates"

    assert default_templates.resolve_default_template_id(
        "general", templates_root
    ) == "general"


def test_resolve_default_template_id_rejects_paths(tmp_path):
    assert default_templates.resolve_default_template_id("../general", tmp_path) is None


_PROSE_HINTS = (
    "heading",
    "headline",
    "title",
    "subtitle",
    "caption",
    "statement",
    "label",
    "topic",
    "card",
    "summary",
    "copy",
    "body",
    "description",
    "paragraph",
    "content",
    "text",
    "name",
)
_NUMERIC_HINTS = (
    "date",
    "page",
    "pagination",
    "folio",
    "value",
    "metric",
    "number",
    "index",
    "percent",
    "pct",
    "count",
    "code",
    "marker",
    "initial",
    "quote_mark",
)


def _iter_fields(node: Any):
    if isinstance(node, dict):
        if "max_length" in node or "min_length" in node:
            yield node
        for child in node.values():
            yield from _iter_fields(child)
    elif isinstance(node, list):
        for child in node:
            yield from _iter_fields(child)


def _is_prose_field(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    lowered = name.lower()
    if any(hint in lowered for hint in _NUMERIC_HINTS):
        return False
    return any(hint in lowered for hint in _PROSE_HINTS)


def test_bundled_templates_load_and_have_wide_prose_ceilings():
    templates_root = Path(__file__).resolve().parents[4] / "templates"
    template_dirs = [
        path
        for path in sorted(templates_root.iterdir())
        if (path / "template.json").is_file()
    ]
    assert len(template_dirs) >= 8

    for template_dir in template_dirs:
        # Loading validates the raw JSON against the Template V2 Pydantic shapes.
        template = default_templates._load_default_template(template_dir)
        assert template.id

        raw = json.loads((template_dir / "template.json").read_text(encoding="utf-8"))
        for field in _iter_fields(raw):
            if not _is_prose_field(field.get("name")):
                continue
            max_length = field.get("max_length")
            if isinstance(max_length, int) and not isinstance(max_length, bool):
                assert max_length > 15, (
                    f"{template_dir.name}:{field.get('name')} max_length={max_length}"
                )


def test_infographic_data_accepts_newer_types():
    from templates.v2.models.elements import Infographic

    for infographic_type in (
        "timeline",
        "vertical_funnel",
        "maturity_model",
        "staircase",
    ):
        element = Infographic.model_validate(
            {
                "type": "infographic",
                "decorative": False,
                "name": f"{infographic_type}_element",
                "data": {
                    "type": infographic_type,
                    "steps": [{"label": "A", "value": 1}],
                },
                "colors": ["#111111"],
            }
        )
        # The unknown type is preserved verbatim instead of crashing the loader.
        assert element.data.type == infographic_type
        assert element.data.model_extra["steps"] == [{"label": "A", "value": 1}]

    gauge = Infographic.model_validate(
        {
            "type": "infographic",
            "decorative": False,
            "name": "gauge_element",
            "data": {"type": "gauge", "min_value": 0, "max_value": 100, "value": 40},
        }
    )
    assert gauge.data.type == "gauge"
