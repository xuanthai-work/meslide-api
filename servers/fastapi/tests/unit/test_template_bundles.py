"""Unit tests: template bundle serialization, repository, and cache."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from models.sql.template_v2 import TemplateV2
from services.template_bundles.bundle import (
    BUNDLE_SCHEMA_VERSION,
    TemplateBundle,
    deserialize_bundle,
    serialize_bundle,
)
from services.template_bundles.cache import BundleCache
from services.template_bundles.errors import (
    BundleChecksumMismatchError,
    InvalidBundleError,
)
from services.template_bundles.repository import TemplateBundleRepository
from services.object_storage.errors import ObjectStorageUnavailableError
from services.object_storage.local import LocalObjectStorage


def _text_element() -> dict:
    return {
        "type": "text",
        "runs": [{"text": "Hello"}],
        "decorative": False,
        "name": "title",
        "max_length": 10,
        "min_length": 5,
    }


def _raw_layouts_payload() -> dict:
    return {
        "layouts": [
            {"id": "r1", "description": "Raw slide", "elements": [_text_element()]}
        ]
    }


def _valid_layouts_payload() -> dict:
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


def _make_template(owner_id=None, *, with_payload: bool = True) -> TemplateV2:
    template = TemplateV2(
        id=str(uuid4()),
        name="Test template",
        owner_id=owner_id,
    )
    if with_payload:
        template.raw_layouts = _raw_layouts_payload()
        template.layouts = _valid_layouts_payload()
        template.merged_components = _merged_components_payload()
    return template


# --- Serialization ----------------------------------------------------------


def test_serialization_is_canonical_and_stable():
    bundle = TemplateBundle(
        raw_layouts=_raw_layouts_payload(),
        layouts=_valid_layouts_payload(),
        merged_components=_merged_components_payload(),
    )
    # Same payload with different dict insertion order must serialize
    # identically: canonical JSON sorts keys before compression.
    reordered_bundle = TemplateBundle(
        raw_layouts=dict(reversed(list(_raw_layouts_payload().items()))),
        layouts=dict(reversed(list(_valid_layouts_payload().items()))),
        merged_components=dict(
            reversed(list(_merged_components_payload().items()))
        ),
    )

    first_bytes, first_sha = serialize_bundle(bundle)
    second_bytes, second_sha = serialize_bundle(reordered_bundle)

    assert first_bytes == second_bytes
    assert first_sha == second_sha


def test_roundtrip_preserves_payload_and_schema_version():
    bundle = TemplateBundle(
        raw_layouts=_raw_layouts_payload(),
        layouts=_valid_layouts_payload(),
        merged_components=_merged_components_payload(),
    )

    compressed, _sha = serialize_bundle(bundle)
    restored = deserialize_bundle(compressed)

    assert restored.raw_layouts == bundle.raw_layouts
    assert restored.layouts == bundle.layouts
    assert restored.merged_components == bundle.merged_components


def test_bundle_payload_carries_schema_version():
    bundle = TemplateBundle(raw_layouts=None, layouts=None, merged_components=None)

    payload = bundle.to_payload()

    assert payload["schema_version"] == BUNDLE_SCHEMA_VERSION


def test_invalid_bundle_is_rejected_at_serialization():
    bundle = TemplateBundle(
        raw_layouts=None,
        layouts={"layouts": "not-a-list"},
        merged_components=None,
    )

    with pytest.raises(InvalidBundleError):
        serialize_bundle(bundle)


def test_deserialize_rejects_corrupt_payload():
    with pytest.raises(InvalidBundleError):
        deserialize_bundle(b"not gzip at all")


# --- Cache ------------------------------------------------------------------


def _placeholder_bundle(tag: str) -> TemplateBundle:
    return TemplateBundle(raw_layouts={tag: 1}, layouts=None, merged_components=None)


def test_cache_is_bounded_and_keyed_by_checksum():
    cache = BundleCache(max_entries=2, ttl_seconds=60)

    cache.put("sha-a", _placeholder_bundle("a"))
    cache.put("sha-b", _placeholder_bundle("b"))
    cache.put("sha-c", _placeholder_bundle("c"))  # evicts sha-a

    assert cache.get("sha-c") is not None
    assert cache.get("sha-b") is not None
    assert cache.get("sha-a") is None


def test_cache_entries_expire():
    cache = BundleCache(max_entries=4, ttl_seconds=0)

    cache.put("sha-a", _placeholder_bundle("a"))
    assert cache.get("sha-a") is None


# --- Repository -------------------------------------------------------------


def test_save_uploads_object_and_sets_reference(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())

    reference = asyncio.run(repository.save(template))

    assert reference.object_key.startswith(f"users/{template.owner_id}/templates/")
    assert reference.object_key.endswith(".json.gz")
    assert template.bundle_object_key == reference.object_key
    assert template.bundle_sha256 == reference.sha256
    assert template.bundle_size_bytes > 0
    assert template.bundle_schema_version == BUNDLE_SCHEMA_VERSION
    assert template.layout_count == 1
    assert asyncio.run(storage.exists(reference.object_key))


def test_ownerless_template_uses_system_prefix(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=None)

    reference = asyncio.run(repository.save(template))

    assert reference.object_key.startswith("users/system/templates/")


def test_load_reads_from_storage_and_verifies_checksum(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(
        storage=storage, cache=BundleCache(max_entries=1, ttl_seconds=0)
    )
    template = _make_template(owner_id=uuid4())
    expected = (
        _raw_layouts_payload(),
        _valid_layouts_payload(),
        _merged_components_payload(),
    )
    asyncio.run(repository.save(template))

    # Fresh repository (no cache) must still hydrate the template.
    fresh = TemplateBundleRepository(
        storage=storage, cache=BundleCache(max_entries=1, ttl_seconds=0)
    )
    loaded = asyncio.run(fresh.load(template))

    assert loaded.raw_layouts == expected[0]
    assert loaded.layouts == expected[1]
    assert loaded.merged_components == expected[2]


def test_load_rejects_checksum_mismatch(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())
    asyncio.run(repository.save(template))

    # Corrupt the stored object in place.
    stored_path = tmp_path / template.bundle_object_key
    stored_path.write_bytes(b"corrupted-but-valid-gzip-less-bytes")
    fresh = TemplateBundleRepository(
        storage=storage, cache=BundleCache(max_entries=1, ttl_seconds=0)
    )

    with pytest.raises(BundleChecksumMismatchError):
        asyncio.run(fresh.load(template))


def test_load_falls_back_to_legacy_columns(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())
    # No bundle_object_key: legacy row.
    template.bundle_object_key = None
    template.bundle_sha256 = None

    loaded = asyncio.run(repository.load(template))

    assert loaded.layouts == template.layouts
    assert loaded.raw_layouts == template.raw_layouts


def test_cache_prevents_repeated_storage_reads(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    # Save through a throwaway cache so the reads below start cold.
    repository = TemplateBundleRepository(
        storage=storage, cache=BundleCache(max_entries=1, ttl_seconds=0)
    )
    template = _make_template(owner_id=uuid4())
    asyncio.run(repository.save(template))

    reads = {"count": 0}
    original_get = storage.get_bytes

    async def counting_get(key):
        reads["count"] += 1
        return await original_get(key)

    storage.get_bytes = counting_get
    cached_repository = TemplateBundleRepository(
        storage=storage, cache=BundleCache(max_entries=8, ttl_seconds=60)
    )

    asyncio.run(cached_repository.load(template))
    asyncio.run(cached_repository.load(template))

    assert reads["count"] == 1


def test_save_is_idempotent_for_identical_content(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())
    payload = (
        _raw_layouts_payload(),
        _valid_layouts_payload(),
        _merged_components_payload(),
    )

    first = asyncio.run(repository.save(template))
    # save() clears the legacy columns (cutover); re-writing the same
    # payload must land on the identical content-addressed revision.
    template.raw_layouts, template.layouts, template.merged_components = payload
    puts = {"count": 0}
    original_put = storage.put_bytes

    async def counting_put(key, data, **kwargs):
        puts["count"] += 1
        await original_put(key, data, **kwargs)

    storage.put_bytes = counting_put
    second = asyncio.run(repository.save(template))

    assert puts["count"] == 0
    assert second.object_key == first.object_key


def test_storage_failure_leaves_no_db_reference(tmp_path: Path):
    class FailingStorage(LocalObjectStorage):
        async def put_bytes(self, key, data, **kwargs):
            raise ObjectStorageUnavailableError("storage down")

    storage = FailingStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())

    with pytest.raises(ObjectStorageUnavailableError):
        asyncio.run(repository.save(template))

    assert template.bundle_object_key is None
    assert template.bundle_sha256 is None


def test_save_rejects_invalid_bundle_before_upload(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())
    template.layouts = {"layouts": "broken"}

    with pytest.raises(InvalidBundleError):
        asyncio.run(repository.save(template))

    # Nothing was uploaded.
    assert list(tmp_path.rglob("*.json.gz")) == []


def test_save_clears_legacy_columns_and_populates_cache(tmp_path: Path):
    """PR3 cutover: a saved row keeps no raw bundle in the DB, and the
    just-written bundle is cached so post-write hydration is free."""
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())
    reference = asyncio.run(repository.save(template))

    # Legacy JSON columns are cleared: the DB row keeps metadata + key only.
    assert template.raw_layouts is None
    assert template.layouts is None
    assert template.merged_components is None
    assert template.bundle_object_key == reference.object_key

    # Hydrating right after the write must not hit storage (cache hit).
    reads = {"count": 0}
    original_get = storage.get_bytes

    async def counting_get(key):
        reads["count"] += 1
        return await original_get(key)

    storage.get_bytes = counting_get
    fresh_template = _make_template(owner_id=None, with_payload=False)
    fresh_template.bundle_object_key = reference.object_key
    fresh_template.bundle_sha256 = reference.sha256
    asyncio.run(repository.hydrate_template(fresh_template))

    assert reads["count"] == 0
    assert fresh_template.layouts == _valid_layouts_payload()


def test_delete_if_unreferenced_removes_object(tmp_path: Path):
    storage = LocalObjectStorage(root=tmp_path)
    repository = TemplateBundleRepository(storage=storage)
    template = _make_template(owner_id=uuid4())
    reference = asyncio.run(repository.save(template))

    asyncio.run(repository.delete_if_unreferenced(template))

    assert asyncio.run(storage.exists(reference.object_key)) is False
    # Idempotent: deleting again must not raise.
    asyncio.run(repository.delete_if_unreferenced(template))