import os
from urllib.parse import unquote, urlsplit
import uuid


PRIVATE_APP_DATA_ROOTS = {
    "images",
    "exports",
    "uploads",
    "pptx-to-html",
    "pptx-to-json",
}
SHARED_APP_DATA_ROOTS = {"fonts", "templates"}


def _legacy_private_root_file_exists(parts: tuple[str, ...]) -> bool:
    """True when a pre-multi-user asset still exists on disk under app_data.

    Legacy assets live directly under the private root (e.g.
    ``images/<file>.png``) without a ``users/<user_id>`` namespace. They were
    never claimed by a signed-in user, so we only serve them while the file is
    still present -- a dead DB row must never surface as a 404 in the picker.
    """
    app_data = os.environ.get("APP_DATA_DIRECTORY")
    if not app_data:
        return False
    candidate = os.path.join(app_data, *parts)
    try:
        resolved_candidate = os.path.realpath(candidate)
        resolved_root = os.path.realpath(app_data)
        if os.path.commonpath([resolved_candidate, resolved_root]) != resolved_root:
            return False
    except (OSError, ValueError):
        return False
    return os.path.isfile(resolved_candidate)


def normalized_app_data_parts(path_or_uri: str) -> tuple[str, ...] | None:
    """Return a traversal-safe app_data path split into URL path segments."""
    if not isinstance(path_or_uri, str) or not path_or_uri:
        return None

    try:
        path = urlsplit(path_or_uri).path
    except ValueError:
        return None

    # Proxies and ASGI servers do not all expose encoded paths consistently.
    # Decode repeatedly, with a hard cap that fails closed for pathological
    # nesting, so encoded traversal cannot cross this boundary.
    for _ in range(8):
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    else:
        if unquote(path) != path:
            return None

    if "\x00" in path or "\\" in path or not path.startswith("/app_data/"):
        return None

    parts = tuple(path.split("/")[2:])
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    return parts


def is_app_data_path_authorized(
    path_or_uri: str,
    *,
    user_id: uuid.UUID,
) -> bool:
    """Authorize a browser-visible app_data path for one authenticated user."""
    parts = normalized_app_data_parts(path_or_uri)
    if not parts:
        return False

    root = parts[0]
    if root in SHARED_APP_DATA_ROOTS:
        return True
    if root not in PRIVATE_APP_DATA_ROOTS or len(parts) < 2:
        return False

    relative_parts = parts[1:]
    if relative_parts[0] == "users":
        return (
            len(relative_parts) >= 3
            and relative_parts[1] == str(user_id)
        )

    # Pre-multi-user assets remain directly under the private root. Serve them
    # to authenticated users only while the physical file still exists, so old
    # slides keep working without ever exposing a dead 404 URL.
    if root == "images":
        return _legacy_private_root_file_exists(parts)

    return False
