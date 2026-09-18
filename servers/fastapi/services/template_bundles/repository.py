"""Template bundle repository: the single gateway between template flows
and object storage.

Business code must not call object storage directly; it goes through
``load`` / ``save`` / ``delete_if_unreferenced`` here so that:

- reads fall back to the legacy JSON columns while bundles are backfilled,
- writes validate, serialize, and upload atomically before the DB
  reference is set (an upload failure never leaves a DB reference),
- the process-local cache absorbs repeat reads of immutable revisions.

Cross-store write protocol (no distributed transactions):

    upload new immutable object
    -> set DB reference fields (the caller commits the session)
    -> DB commit fails: the row still references the old object, and the
       new object is unreferenced by anything (a revision key is content
       addressed, so nothing else can point at it) — it is collected by
       the orphan scanner in a later phase, or delete_if_unreferenced on
       retry.
"""

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from services.object_storage.base import ObjectStorage
from services.object_storage.errors import ObjectStorageError
from services.object_storage.factory import get_object_storage
from services.object_storage.keys import build_template_bundle_key
from services.template_bundles.bundle import (
    BUNDLE_SCHEMA_VERSION,
    TemplateBundle,
    deserialize_bundle,
    serialize_bundle,
)
from services.template_bundles.cache import BundleCache, get_template_bundle_cache
from services.template_bundles.errors import BundleChecksumMismatchError

logger = logging.getLogger(__name__)

# Ownerless rows (bundled default templates) live under a shared system
# prefix instead of a user prefix.
SYSTEM_OWNER_SEGMENT = "system"


@dataclass(frozen=True)
class BundleReference:
    object_key: str
    sha256: str
    size_bytes: int
    schema_version: int
    layout_count: int


def _owner_segment(template: Any) -> str:
    owner_id = getattr(template, "owner_id", None)
    return str(owner_id) if owner_id is not None else SYSTEM_OWNER_SEGMENT


class TemplateBundleRepository:
    def __init__(
        self,
        storage: ObjectStorage | None = None,
        cache: BundleCache | None = None,
    ):
        self._storage = storage if storage is not None else get_object_storage()
        self._cache = cache if cache is not None else get_template_bundle_cache()

    @property
    def storage(self) -> ObjectStorage:
        """The underlying backend (used by the backfiller for read-back
        verification; business code should stick to load/save)."""
        return self._storage

    async def load(self, template: Any) -> TemplateBundle:
        """Hydrate the bundle for a template row.

        Prefers the stored object (checksum-verified, validated, cached);
        rows without a bundle reference fall back to the legacy JSON
        columns exactly as before the migration.
        """
        object_key = getattr(template, "bundle_object_key", None)
        if not object_key:
            return TemplateBundle.from_template(template)

        expected_sha256 = getattr(template, "bundle_sha256", None)
        cached = self._cache.get(expected_sha256)
        if cached is not None:
            return cached

        compressed = await self._storage.get_bytes(object_key)
        actual_sha256 = hashlib.sha256(compressed).hexdigest()
        if expected_sha256 and actual_sha256 != expected_sha256:
            logger.error(
                "template bundle checksum mismatch template_id=%s key=%s",
                getattr(template, "id", None),
                object_key,
            )
            raise BundleChecksumMismatchError(
                "Stored template bundle does not match the recorded checksum"
            )
        bundle = deserialize_bundle(compressed)
        self._cache.put(actual_sha256, bundle)
        return bundle

    async def hydrate_template(self, template: Any) -> Any:
        """Ensure a loaded template row carries its payload in memory.

        During dual-write the legacy JSON columns are already populated and
        this is a no-op attribute check. Once a row stops writing the legacy
        columns (post-cutover), the payload is fetched from object storage
        here so downstream code keeps reading ``template.layouts`` etc.
        """
        if not getattr(template, "bundle_object_key", None):
            return template
        needs_hydration = (
            getattr(template, "raw_layouts", None) is None
            or getattr(template, "layouts", None) is None
            or getattr(template, "merged_components", None) is None
        )
        if not needs_hydration:
            return template
        bundle = await self.load(template)
        if template.raw_layouts is None:
            template.raw_layouts = bundle.raw_layouts
        if template.layouts is None:
            template.layouts = bundle.layouts
        if template.merged_components is None:
            template.merged_components = bundle.merged_components
        return template

    async def save(
        self,
        template: Any,
        bundle: TemplateBundle | None = None,
        *,
        clear_legacy_payload: bool = True,
    ) -> BundleReference:
        """Validate, serialize, upload, and set the DB reference fields.

        The caller still owns the session commit; a storage failure raises
        before any template field is touched, so the row keeps pointing at
        the previous revision.
        """
        if bundle is None:
            bundle = TemplateBundle.from_template(template)
        compressed, sha256 = serialize_bundle(bundle)
        object_key = build_template_bundle_key(
            owner_id=_owner_segment(template),
            template_id=template.id,
            sha256=sha256,
        )
        if not await self._storage.exists(object_key):
            await self._storage.put_bytes(
                object_key,
                compressed,
                content_type="application/json",
                content_encoding="gzip",
                metadata={
                    "sha256": sha256,
                    "schema-version": str(BUNDLE_SCHEMA_VERSION),
                },
            )
            logger.info(
                "template bundle uploaded template_id=%s key=%s bytes=%d",
                template.id,
                object_key,
                len(compressed),
            )

        layout_count = bundle.layout_count()
        reference = BundleReference(
            object_key=object_key,
            sha256=sha256,
            size_bytes=len(compressed),
            schema_version=BUNDLE_SCHEMA_VERSION,
            layout_count=layout_count,
        )
        template.bundle_object_key = object_key
        template.bundle_sha256 = sha256
        template.bundle_size_bytes = len(compressed)
        template.bundle_schema_version = BUNDLE_SCHEMA_VERSION
        template.layout_count = layout_count
        # PR3 cutover: normal writes keep only the reference. The backfill
        # path opts out during the rollback window so migrating old rows does
        # not destroy their last database-resident copy.
        if clear_legacy_payload:
            template.raw_layouts = None
            template.layouts = None
            template.merged_components = None
        self._cache.put(sha256, bundle)
        return reference

    async def delete_if_unreferenced(self, template: Any) -> None:
        """Best-effort delete of the bundle object once the row is gone.

        Bundle keys are content-addressed revisions scoped to one template,
        so deleting the template's key can never remove data another row
        still points at.
        """
        object_key = getattr(template, "bundle_object_key", None)
        if not object_key:
            return
        try:
            await self._storage.delete(object_key)
            logger.info(
                "template bundle deleted template_id=%s key=%s",
                getattr(template, "id", None),
                object_key,
            )
        except ObjectStorageError as exc:
            logger.warning(
                "template bundle delete failed (orphan scanner will reap it) "
                "template_id=%s key=%s error=%s",
                getattr(template, "id", None),
                object_key,
                exc,
            )


__all__ = [
    "BundleReference",
    "SYSTEM_OWNER_SEGMENT",
    "TemplateBundleRepository",
]
