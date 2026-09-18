"""Flow tests: template write/read endpoints route through the bundle
repository (upload on write, hydrate on read, delete after commit)."""

import asyncio
import uuid
from unittest.mock import AsyncMock, Mock, patch

import pytest

from api.v1.ppt.endpoints.presentation_layout import (
    _resolve_generation_layout,
    _resolve_prepare_layout,
    _resolve_requested_template,
)
from api.v1.ppt.endpoints.template import (
    _create_template_sync,
    delete_template,
    generate_template_blocks,
    get_template,
    init_template,
    list_templates,
    patch_template_slide_layout,
)
from api.v1.ppt.endpoints.template import (
    CreateTemplateRequest,
    GenerateTemplateBlocksRequest,
    InitTemplateRequest,
    PatchTemplateSlideLayoutRequest,
)
from models.sql.template_v2 import TemplateV2
from templates.v2.models.layouts import MergedComponents
from services.export_task_service import PptxToJsonDocument
from services.object_storage.local import LocalObjectStorage
from services.template_bundles.repository import TemplateBundleRepository

RAW_LAYOUTS = {
    "layouts": [
        {
            "id": "slide_1",
            "description": "Full slide layout converted from PPTX slide 1.",
            "elements": [
                {
                    "type": "vector",
                    "points": [
                        {"x": 0, "y": 0},
                        {"x": 1280, "y": 0},
                        {"x": 1280, "y": 720},
                        {"x": 0, "y": 720},
                    ],
                    "closed": True,
                    "fill": {"color": "#FFFFFF"},
                },
                {
                    "type": "image",
                    "position": {"x": 100, "y": 120},
                    "size": {"width": 320, "height": 180},
                    "data": "/app_data/pptx-to-json/session/images/photo.png",
                    "decorative": True,
                    "name": "photo",
                    "is_icon": False,
                },
            ],
        }
    ]
}

TEMPLATE_LAYOUTS = {
    "layouts": [
        {
            "id": "slide_1",
            "description": "Full slide layout converted from PPTX slide 1.",
            "components": [
                {
                    "id": "photo_component",
                    "description": "Reusable image component.",
                    "position": {"x": 100, "y": 120},
                    "elements": [
                        {
                            "type": "image",
                            "position": {"x": 0, "y": 0},
                            "size": {"width": 320, "height": 180},
                            "data": "/app_data/pptx-to-json/session/images/photo.png",
                            "decorative": True,
                            "name": "photo",
                            "is_icon": False,
                        }
                    ],
                }
            ],
        }
    ]
}

MERGED_COMPONENTS = {
    "components": [
        {
            "id": "photo_component",
            "description": "Reusable image component.",
            "variants": TEMPLATE_LAYOUTS["layouts"][0]["components"],
        }
    ]
}


@pytest.fixture
def bundle_storage(tmp_path, monkeypatch):
    """Route all repositories created by endpoint code to a local store."""
    storage = LocalObjectStorage(root=tmp_path / "object-storage")
    monkeypatch.setattr(
        "services.template_bundles.repository.get_object_storage",
        lambda: storage,
    )
    return storage


def _saved_template(bundle_storage, **kwargs) -> TemplateV2:
    """Persist a template payload through the repository (simulates PR3 row)."""
    template = TemplateV2(
        name="Custom",
        raw_layouts=RAW_LAYOUTS,
        layouts=TEMPLATE_LAYOUTS,
        merged_components=MERGED_COMPONENTS,
        **kwargs,
    )
    asyncio.run(TemplateBundleRepository(storage=bundle_storage).save(template))
    return template


# --- Write flows ------------------------------------------------------------


def test_init_template_uploads_bundle_reference(
    tmp_path, fake_async_session, bundle_storage
):
    pptx_path = tmp_path / "quarterly-review.pptx"
    pptx_path.write_bytes(b"pptx")
    with patch(
        "api.v1.ppt.endpoints.template.resolve_app_path_to_filesystem",
        return_value=str(pptx_path),
    ), patch(
        "api.v1.ppt.endpoints.template.EXPORT_TASK_SERVICE.convert_pptx_to_json",
        new=AsyncMock(return_value=PptxToJsonDocument(**RAW_LAYOUTS)),
    ):
        asyncio.run(
            init_template(
                InitTemplateRequest(
                    pptx_url="/app_data/uploads/quarterly-review.pptx",
                    slide_image_urls=["/app_data/images/slide-1.png"],
                ),
                sql_session=fake_async_session,
            )
        )

    template = fake_async_session.added[0]
    assert template.bundle_object_key is not None
    assert template.bundle_object_key.endswith(".json.gz")
    assert len(template.bundle_sha256) == 64
    assert template.bundle_size_bytes > 0
    assert template.bundle_schema_version == 1
    assert asyncio.run(bundle_storage.exists(template.bundle_object_key)) is True


def test_create_template_sync_uploads_bundle_reference(
    tmp_path, fake_async_session, bundle_storage
):
    from templates.v2.models.layouts import MergedComponents, SlideLayouts

    pptx_path = tmp_path / "quarterly-review.pptx"
    pptx_path.write_bytes(b"pptx")
    with patch(
        "api.v1.ppt.endpoints.template.resolve_app_path_to_filesystem",
        return_value=str(pptx_path),
    ), patch(
        "api.v1.ppt.endpoints.template.EXPORT_TASK_SERVICE.convert_pptx_to_json",
        new=AsyncMock(return_value=PptxToJsonDocument(**RAW_LAYOUTS)),
    ), patch(
        "api.v1.ppt.endpoints.template.generate_template",
        new=Mock(return_value=SlideLayouts.model_validate(TEMPLATE_LAYOUTS)),
    ), patch(
        "api.v1.ppt.endpoints.template.merge_similar_components",
        new=Mock(return_value=MergedComponents.model_validate(MERGED_COMPONENTS)),
    ), patch(
        "api.v1.ppt.endpoints.template.random.randint",
        return_value=4801,
    ):
        template = asyncio.run(
            _create_template_sync(
                CreateTemplateRequest(
                    pptx_url="/app_data/uploads/quarterly-review.pptx",
                    slide_image_urls=["/app_data/images/slide-1.png"],
                ),
                sql_session=fake_async_session,
            )
        )

    assert template.bundle_object_key is not None
    assert template.layout_count == 1
    assert asyncio.run(bundle_storage.exists(template.bundle_object_key)) is True


def test_generate_template_blocks_uploads_new_bundle_revision(
    fake_async_session, bundle_storage
):
    template = _saved_template(bundle_storage)
    old_key = template.bundle_object_key
    fake_async_session._get_results[template.id] = template

    with patch(
        "api.v1.ppt.endpoints.template.merge_similar_components",
        new=Mock(
            return_value=MergedComponents.model_validate(MERGED_COMPONENTS)
        ),
    ):
        asyncio.run(
            generate_template_blocks(
                GenerateTemplateBlocksRequest.model_validate(
                    {"id": str(template.id)}
                ),
                sql_session=fake_async_session,
            )
        )

    assert template.bundle_object_key != old_key
    # Both the previous and the new revision stay in storage (rollback safety).
    assert asyncio.run(bundle_storage.exists(old_key)) is True
    assert asyncio.run(bundle_storage.exists(template.bundle_object_key)) is True


def test_patch_template_slide_layout_writes_new_bundle_revision(
    fake_async_session, bundle_storage
):
    template = _saved_template(bundle_storage)
    old_key = template.bundle_object_key
    fake_async_session._get_results[template.id] = template
    patched_layout = {
        **TEMPLATE_LAYOUTS["layouts"][0],
        "id": "slide_1_updated",
    }

    asyncio.run(
        patch_template_slide_layout(
            str(template.id),
            PatchTemplateSlideLayoutRequest(index=0, layout=patched_layout),
            sql_session=fake_async_session,
        )
    )

    assert template.bundle_object_key != old_key
    assert asyncio.run(bundle_storage.exists(template.bundle_object_key)) is True


# --- Read flows -------------------------------------------------------------


def test_get_template_hydrates_bundle_from_storage(fake_async_session, bundle_storage):
    template = _saved_template(bundle_storage)
    # Post-cutover rows keep only the object reference in the database.
    template.raw_layouts = None
    template.layouts = None
    template.merged_components = None
    fake_async_session._get_results[template.id] = template

    response = asyncio.run(
        get_template(str(template.id), sql_session=fake_async_session)
    )

    assert response.layouts == TEMPLATE_LAYOUTS
    assert response.merged_components == MERGED_COMPONENTS


def test_get_template_legacy_row_keeps_working(fake_async_session, bundle_storage):
    template = TemplateV2(
        name="Legacy",
        raw_layouts=RAW_LAYOUTS,
        layouts=TEMPLATE_LAYOUTS,
        merged_components=MERGED_COMPONENTS,
    )
    fake_async_session._get_results[template.id] = template

    response = asyncio.run(
        get_template(str(template.id), sql_session=fake_async_session)
    )

    assert response.layouts == TEMPLATE_LAYOUTS


def test_resolve_generation_layout_hydrates_bundle(
    fake_async_session, bundle_storage
):
    template = _saved_template(bundle_storage)
    template.raw_layouts = None
    template.layouts = None
    template.merged_components = None
    fake_async_session._get_results[template.id] = template

    resolved = asyncio.run(_resolve_requested_template(str(template.id), fake_async_session))

    assert resolved is template
    assert resolved.layouts == TEMPLATE_LAYOUTS


def test_resolve_prepare_layout_hydrates_bundle(
    fake_async_session, bundle_storage
):
    template = _saved_template(bundle_storage)
    fake_async_session._get_results[template.id] = template

    layout_payload, structure_layout, _fonts = asyncio.run(
        _resolve_prepare_layout(str(template.id), fake_async_session)
    )

    assert layout_payload["layouts"] == TEMPLATE_LAYOUTS["layouts"]
    assert structure_layout.slides[0].id == "slide_1"


def test_resolve_bundled_alias_hydrates_bundle(
    fake_async_session, bundle_storage
):
    template = _saved_template(bundle_storage, id="bundled-template-id")
    fake_async_session._get_results[template.id] = template

    with patch(
        "api.v1.ppt.endpoints.presentation_layout.resolve_default_template_id",
        return_value=template.id,
    ):
        layout_payload, structure_layout, _fonts = asyncio.run(
            _resolve_generation_layout("public-template-name", fake_async_session)
        )

    assert layout_payload["layouts"] == TEMPLATE_LAYOUTS["layouts"]
    assert structure_layout.slides[0].id == "slide_1"


# --- Delete flow ------------------------------------------------------------


def test_delete_template_removes_bundle_object_after_commit(
    fake_async_session, bundle_storage
):
    template = _saved_template(bundle_storage)
    object_key = template.bundle_object_key
    fake_async_session._get_results[template.id] = template

    asyncio.run(delete_template(str(template.id), sql_session=fake_async_session))

    assert fake_async_session.commit_count == 1
    assert fake_async_session.deleted == [template]
    assert asyncio.run(bundle_storage.exists(object_key)) is False


def test_bundle_reference_uuid_matches_template_id(fake_async_session, bundle_storage):
    owner_id = uuid.uuid4()
    template = _saved_template(bundle_storage, owner_id=owner_id)

    assert template.bundle_object_key.startswith(f"users/{owner_id}/templates/")
    assert template.id in template.bundle_object_key


# --- PR3 cutover: new rows keep no raw bundle in the DB ----------------------


def test_init_template_does_not_persist_legacy_json(
    tmp_path, fake_async_session, bundle_storage
):
    pptx_path = tmp_path / "quarterly-review.pptx"
    pptx_path.write_bytes(b"pptx")
    with patch(
        "api.v1.ppt.endpoints.template.resolve_app_path_to_filesystem",
        return_value=str(pptx_path),
    ), patch(
        "api.v1.ppt.endpoints.template.EXPORT_TASK_SERVICE.convert_pptx_to_json",
        new=AsyncMock(return_value=PptxToJsonDocument(**RAW_LAYOUTS)),
    ):
        asyncio.run(
            init_template(
                InitTemplateRequest(
                    pptx_url="/app_data/uploads/quarterly-review.pptx",
                    slide_image_urls=["/app_data/images/slide-1.png"],
                ),
                sql_session=fake_async_session,
            )
        )

    template = fake_async_session.added[0]
    assert template.bundle_object_key is not None
    # The bundle lives in object storage; the DB row keeps metadata only.
    assert template.raw_layouts is None
    assert template.layouts is None
    assert template.merged_components is None


def test_create_template_response_hydrates_from_bundle(
    tmp_path, fake_async_session, bundle_storage
):
    from templates.v2.models.layouts import MergedComponents, SlideLayouts

    pptx_path = tmp_path / "quarterly-review.pptx"
    pptx_path.write_bytes(b"pptx")
    with patch(
        "api.v1.ppt.endpoints.template.resolve_app_path_to_filesystem",
        return_value=str(pptx_path),
    ), patch(
        "api.v1.ppt.endpoints.template.EXPORT_TASK_SERVICE.convert_pptx_to_json",
        new=AsyncMock(return_value=PptxToJsonDocument(**RAW_LAYOUTS)),
    ), patch(
        "api.v1.ppt.endpoints.template.generate_template",
        new=Mock(return_value=SlideLayouts.model_validate(TEMPLATE_LAYOUTS)),
    ), patch(
        "api.v1.ppt.endpoints.template.merge_similar_components",
        new=Mock(return_value=MergedComponents.model_validate(MERGED_COMPONENTS)),
    ), patch(
        "api.v1.ppt.endpoints.template.random.randint",
        return_value=4801,
    ):
        template = asyncio.run(
            _create_template_sync(
                CreateTemplateRequest(
                    pptx_url="/app_data/uploads/quarterly-review.pptx",
                    slide_image_urls=["/app_data/images/slide-1.png"],
                ),
                sql_session=fake_async_session,
            )
        )

    # Response payload must survive the cutover: the template is re-hydrated
    # from the bundle after the write, so callers still see the layouts.
    assert template.bundle_object_key is not None
    assert template.layouts is not None
    # Layout ids are randomized by the create flow; compare structure.
    assert len(template.layouts["layouts"]) == 1
    assert template.layouts["layouts"][0]["components"][0]["id"] == (
        "photo_component"
    )


class _ListSession:
    """Session double that answers list_templates' aggregate select."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_args, **_kwargs):
        session = self

        class _Result:
            def all(self):
                return list(session.rows)

        return _Result()


def test_list_templates_counts_rows_without_legacy_layouts(bundle_storage):
    from datetime import datetime, timezone

    template = _saved_template(bundle_storage)
    legacy = TemplateV2(
        name="Legacy",
        raw_layouts=None,
        layouts=TEMPLATE_LAYOUTS,
        merged_components=MERGED_COMPONENTS,
    )
    now = datetime.now(timezone.utc)
    # Cutover row: no legacy layouts column, layout_count carries the count.
    cut_rows = [
        (
            template.id,
            "Custom",
            None,
            None,
            {},
            False,
            now,
            now,
            1,
        ),
        (
            legacy.id,
            "Legacy",
            None,
            TEMPLATE_LAYOUTS,
            {},
            False,
            now,
            now,
            None,
        ),
    ]

    response = asyncio.run(
        list_templates(
            page=1,
            page_size=20,
            default=None,
            sql_session=_ListSession(cut_rows),
        )
    )

    counts = {item.id: item.layout_count for item in response.items}
    assert counts[template.id] == 1
    assert counts[legacy.id] == 1
