"""Template bundle model, validation, and canonical serialization.

Bundle format (schema_version 1):

    {
      "schema_version": 1,
      "raw_layouts": {...} | null,
      "layouts": {...} | null,
      "merged_components": {...} | null
    }

Serialization pipeline (fixed convention):

    validate -> canonical JSON (sorted keys) -> UTF-8 -> gzip (mtime=0)
    -> SHA-256 over the *compressed* bytes -> immutable object key

The checksum covers the compressed bytes: object stores already deliver the
exact bytes stored, so this verifies end-to-end integrity of both the
transfer and the storage.
"""

import gzip
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from services.template_bundles.errors import InvalidBundleError
from templates.v2.models.layouts import (
    MergedComponents,
    RawSlideLayouts,
    SlideLayouts,
)

BUNDLE_SCHEMA_VERSION = 1


@dataclass
class TemplateBundle:
    raw_layouts: dict[str, Any] | None
    layouts: dict[str, Any] | None
    merged_components: dict[str, Any] | None

    @classmethod
    def from_template(cls, template: Any) -> "TemplateBundle":
        """Read the bundle fields off a TemplateV2 row (no validation)."""
        return cls(
            raw_layouts=template.raw_layouts,
            layouts=template.layouts,
            merged_components=template.merged_components,
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TemplateBundle":
        return cls(
            raw_layouts=payload.get("raw_layouts"),
            layouts=payload.get("layouts"),
            merged_components=payload.get("merged_components"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "raw_layouts": self.raw_layouts,
            "layouts": self.layouts,
            "merged_components": self.merged_components,
        }

    def validate(self) -> None:
        """Validate every present field against the shared layout models.

        Missing fields are allowed (templates legitimately lack e.g.
        ``layouts`` right after /init); anything present must be structurally
        valid before it can be uploaded or surfaced from storage.
        """
        for label, value, model in (
            ("raw_layouts", self.raw_layouts, RawSlideLayouts),
            ("layouts", self.layouts, SlideLayouts),
            ("merged_components", self.merged_components, MergedComponents),
        ):
            if value is None:
                continue
            if not isinstance(value, dict):
                raise InvalidBundleError(f"Bundle field {label!r} must be an object")
            try:
                model.model_validate(value)
            except ValidationError as exc:
                raise InvalidBundleError(
                    f"Bundle field {label!r} failed validation: {exc.errors()[:3]}"
                ) from exc

    def layout_count(self) -> int:
        layouts = self.layouts if isinstance(self.layouts, dict) else {}
        items = layouts.get("layouts")
        if isinstance(items, list):
            return len(items)
        return 0


def serialize_bundle(bundle: TemplateBundle) -> tuple[bytes, str]:
    """Validate + canonical-serialize + compress + hash.

    Returns ``(compressed_bytes, sha256_hex)``.
    """
    bundle.validate()
    canonical = json.dumps(
        bundle.to_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    compressed = gzip.compress(canonical, mtime=0)
    return compressed, hashlib.sha256(compressed).hexdigest()


def deserialize_bundle(compressed: bytes) -> TemplateBundle:
    """Decompress + parse + validate a stored bundle."""
    try:
        canonical = gzip.decompress(compressed)
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise InvalidBundleError(f"Stored bundle is not valid gzip: {exc}") from exc
    try:
        payload = json.loads(canonical.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidBundleError(f"Stored bundle is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InvalidBundleError("Stored bundle must be a JSON object")
    if payload.get("schema_version") != BUNDLE_SCHEMA_VERSION:
        raise InvalidBundleError(
            f"Unsupported bundle schema_version: {payload.get('schema_version')!r}"
        )
    bundle = TemplateBundle.from_payload(payload)
    bundle.validate()
    return bundle


__all__ = [
    "BUNDLE_SCHEMA_VERSION",
    "TemplateBundle",
    "deserialize_bundle",
    "serialize_bundle",
]