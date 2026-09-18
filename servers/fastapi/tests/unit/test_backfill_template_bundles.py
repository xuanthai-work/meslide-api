"""PR3 backfill tests: idempotent legacy-JSON -> object-storage migration."""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


from models.sql.template_v2 import TemplateV2
from services.object_storage.local import LocalObjectStorage
from services.template_bundles.repository import TemplateBundleRepository

from scripts.backfill_template_bundles import TemplateBundleBackfiller


def _text_element() -> dict:
    return {
        "type": "text",
        "runs": [{"text": "Hello"}],
        "decorative": False,
        "name": "title",
        "max_length": 10,
        "min_length": 5,
    }


def _layouts_payload() -> dict:
    return {
        "layouts": [
            {
                "id": "intro",
                "description": "Intro layout",
                "components": [
                    {
                        "id": "c1",
                        "description": "Title",
                        "position": {"x": 0.0, "y": 0.0},
                        "elements": [_text_element()],
                    }
                ],
            }
        ]
    }


def _merged_components_payload() -> dict:
    return {"components": []}


def _legacy_template(**kwargs) -> TemplateV2:
    template = TemplateV2(
        id=str(uuid4()),
        name="Legacy template",
        owner_id=uuid4(),
        **kwargs,
    )
    template.raw_layouts = {"layouts": [{"id": "r1", "description": "Raw", "elements": [_text_element()]}]}
    template.layouts = _layouts_payload()
    template.merged_components = _merged_components_payload()
    return template


class _BackfillSession:
    """Minimal async session: supports the select/get/add/commit the
    backfiller performs."""

    def __init__(self, templates):
        self._templates = list(templates)
        self.added: list = []
        self.commit_count = 0

    async def get(self, _model, key):
        return next((t for t in self._templates if t.id == key), None)

    async def execute(self, *_args, **_kwargs):
        session = self

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return list(session._templates)

        return _Result()

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        return None


class _SessionContext:
    def __init__(self, session: _BackfillSession):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return None


@dataclass
class _Harness:
    session: _BackfillSession
    storage: LocalObjectStorage
    backfiller: TemplateBundleBackfiller


def _harness(tmp_path: Path, templates, **backfiller_kwargs) -> _Harness:
    storage = LocalObjectStorage(root=tmp_path / "object-storage")
    session = _BackfillSession(templates)
    backfiller = TemplateBundleBackfiller(
        session_factory=lambda: _SessionContext(session),
        repository=TemplateBundleRepository(storage=storage),
        **backfiller_kwargs,
    )
    return _Harness(session=session, storage=storage, backfiller=backfiller)


def test_backfill_uploads_bundle_and_sets_reference(tmp_path):
    template = _legacy_template()
    original_payload = (
        template.raw_layouts,
        template.layouts,
        template.merged_components,
    )
    harness = _harness(tmp_path, [template])

    report = asyncio.run(harness.backfiller.run())

    assert report.backfilled == 1
    assert report.failures == []
    assert template.bundle_object_key is not None
    assert template.bundle_sha256 is not None
    assert asyncio.run(harness.storage.exists(template.bundle_object_key))
    assert harness.session.commit_count == 1
    assert (
        template.raw_layouts,
        template.layouts,
        template.merged_components,
    ) == original_payload


def test_backfill_is_idempotent(tmp_path):
    template = _legacy_template()
    harness = _harness(tmp_path, [template])
    asyncio.run(harness.backfiller.run())

    second = asyncio.run(harness.backfiller.run())

    assert second.backfilled == 0
    assert second.skipped_already_bundled == 1
    assert second.failures == []


def test_backfill_dry_run_touches_nothing(tmp_path):
    template = _legacy_template()
    harness = _harness(tmp_path, [template])

    report = asyncio.run(harness.backfiller.run(dry_run=True))

    assert report.dry_run is True
    assert report.would_backfill == 1
    assert report.backfilled == 0
    assert template.bundle_object_key is None
    assert list((tmp_path / "object-storage").rglob("*.json.gz")) == []
    assert harness.session.commit_count == 0


def test_backfill_skips_rows_without_payload(tmp_path):
    empty = TemplateV2(id=str(uuid4()), name="Empty", owner_id=uuid4())
    harness = _harness(tmp_path, [empty])

    report = asyncio.run(harness.backfiller.run())

    assert report.skipped_empty == 1
    assert report.backfilled == 0
    assert empty.bundle_object_key is None


def test_backfill_invalid_legacy_row_is_reported_not_fatal(tmp_path):
    broken = _legacy_template()
    broken.layouts = {"layouts": "not-a-list"}
    healthy = _legacy_template()
    harness = _harness(tmp_path, [broken, healthy])

    report = asyncio.run(harness.backfiller.run())

    assert report.backfilled == 1
    assert len(report.failures) == 1
    assert report.failures[0]["template_id"] == broken.id
    assert broken.bundle_object_key is None
    assert healthy.bundle_object_key is not None


def test_backfill_filters_by_template_id(tmp_path):
    target = _legacy_template()
    other = _legacy_template()
    harness = _harness(tmp_path, [target, other])

    report = asyncio.run(harness.backfiller.run(template_id=target.id))

    assert report.backfilled == 1
    assert report.scanned == 1
    assert target.bundle_object_key is not None
    assert other.bundle_object_key is None


def test_backfill_read_back_mismatch_rolls_back_reference(tmp_path):
    class _CorruptReadStorage(LocalObjectStorage):
        async def get_bytes(self, key):
            return b"corrupted-object-bytes"

    template = _legacy_template()
    session = _BackfillSession([template])
    storage = _CorruptReadStorage(root=tmp_path / "object-storage")
    backfiller = TemplateBundleBackfiller(
        session_factory=lambda: _SessionContext(session),
        repository=TemplateBundleRepository(storage=storage),
    )

    report = asyncio.run(backfiller.run())

    assert report.backfilled == 0
    assert len(report.failures) == 1
    assert template.bundle_object_key is None


def test_verify_reports_checksum_mismatch(tmp_path):
    template = _legacy_template()
    harness = _harness(tmp_path, [template])
    asyncio.run(harness.backfiller.run())
    # Corrupt the stored object in place.
    stored = next((tmp_path / "object-storage").rglob("*.json.gz"))
    stored.write_bytes(b"corrupted-but-present")

    report = asyncio.run(harness.backfiller.verify())

    assert report.verified == 0
    assert len(report.failures) == 1


def test_verify_passes_for_healthy_references(tmp_path):
    template = _legacy_template()
    harness = _harness(tmp_path, [template])
    asyncio.run(harness.backfiller.run())

    report = asyncio.run(harness.backfiller.verify())

    assert report.verified == 1
    assert report.failures == []


def test_report_is_written_as_json(tmp_path):
    template = _legacy_template()
    harness = _harness(tmp_path, [template])
    report_path = tmp_path / "report.json"

    asyncio.run(harness.backfiller.run(report_path=report_path))

    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["backfilled"] == 1
    assert saved["failures"] == []
