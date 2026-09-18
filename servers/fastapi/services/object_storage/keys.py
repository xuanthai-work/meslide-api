"""Object key construction and validation.

Conventions (see the S3/MinIO storage plan):

    users/{owner_id}/templates/{template_id}/revisions/{sha256}.json.gz
    users/{owner_id}/images/{asset_id}.{ext}
    users/{owner_id}/fonts/{font_id}.{ext}
    users/{owner_id}/exports/{export_id}.{ext}
    users/{owner_id}/conversions/{conversion_id}/...

Rules:
- Keys are immutable identifiers derived from DB records (IDs, checksums),
  never from user-supplied filenames.
- Every user object lives under its owner prefix so ownership checks are a
  string comparison, and keys can never escape the owner namespace.
"""

from services.object_storage.errors import InvalidObjectKeyError

MAX_OBJECT_KEY_LENGTH = 1024


def validate_object_key(key: str) -> str:
    """Validate an object key and return it.

    Rejects empty keys, absolute paths, ``.``/``..`` segments, empty segments,
    control characters, and keys longer than the S3 limit.
    """
    if not isinstance(key, str) or not key:
        raise InvalidObjectKeyError("Object key must be a non-empty string")
    if key.startswith("/"):
        raise InvalidObjectKeyError("Object key must not be absolute")
    for segment in key.split("/"):
        if segment in ("", ".", ".."):
            raise InvalidObjectKeyError(
                f"Object key contains an empty or relative path segment: {key!r}"
            )
    if len(key) > MAX_OBJECT_KEY_LENGTH:
        raise InvalidObjectKeyError(
            f"Object key exceeds the {MAX_OBJECT_KEY_LENGTH}-character limit"
        )
    for character in key:
        if ord(character) < 32 or ord(character) == 127:
            raise InvalidObjectKeyError(
                f"Object key contains control characters: {key!r}"
            )
    return key


def validate_owner_object_key(key: str, owner_id: str | int) -> str:
    """Validate an object key and enforce the owner prefix."""
    validate_object_key(key)
    prefix = f"users/{owner_id}/"
    if not key.startswith(prefix):
        raise InvalidObjectKeyError(
            f"Object key {key!r} does not belong to owner {owner_id}"
        )
    return key


def _validate_identifier(name: str, label: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise InvalidObjectKeyError(f"{label} must be a non-empty string")
    name = name.strip()
    if "/" in name or "\\" in name:
        raise InvalidObjectKeyError(f"{label} must not contain path separators")
    return name


def build_template_bundle_key(
    *, owner_id: str | int, template_id: str, sha256: str
) -> str:
    """Immutable revision key for a template bundle (gzipped JSON)."""
    owner_id = _validate_identifier(str(owner_id), "owner_id")
    template_id = _validate_identifier(template_id, "template_id")
    sha256 = _validate_identifier(sha256, "sha256")
    if not all(character in "0123456789abcdef" for character in sha256.lower()):
        raise InvalidObjectKeyError("sha256 must be a hexadecimal digest")
    return (
        f"users/{owner_id}/templates/{template_id}/revisions/{sha256.lower()}.json.gz"
    )


__all__ = [
    "MAX_OBJECT_KEY_LENGTH",
    "build_template_bundle_key",
    "validate_object_key",
    "validate_owner_object_key",
]