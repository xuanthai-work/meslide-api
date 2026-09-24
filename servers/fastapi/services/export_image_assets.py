import base64
import mimetypes
from typing import Any
from urllib.parse import unquote, urlparse

from utils.asset_directory_utils import resolve_app_path_to_filesystem


def _localize_json_image_assets(
    value: Any,
    data_uri_cache: dict[str, str] | None = None,
) -> Any:
    """Return a copy with local image sources embedded for headless rendering."""
    cache = data_uri_cache if data_uri_cache is not None else {}

    if isinstance(value, list):
        return [_localize_json_image_assets(item, cache) for item in value]
    if not isinstance(value, dict):
        return value

    localized = {
        key: _localize_json_image_assets(item, cache) for key, item in value.items()
    }
    if localized.get("type") != "image":
        return localized

    source = localized.get("data")
    if not isinstance(source, str) or not source:
        return localized

    parsed = urlparse(source)
    if parsed.scheme in ("http", "https"):
        candidate = unquote(parsed.path)
    elif not parsed.scheme and source.startswith(("/app_data/", "/static/")):
        candidate = source
    else:
        return localized

    if not candidate.startswith(("/app_data/", "/static/")):
        return localized

    resolved = resolve_app_path_to_filesystem(candidate)
    if not resolved:
        return localized

    data_uri = cache.get(resolved)
    if data_uri is None:
        try:
            with open(resolved, "rb") as asset_file:
                asset_data = asset_file.read()
        except OSError:
            return localized
        mime_type = mimetypes.guess_type(resolved)[0] or "application/octet-stream"
        encoded = base64.b64encode(asset_data).decode("ascii")
        data_uri = f"data:{mime_type};base64,{encoded}"
        cache[resolved] = data_uri

    localized["data"] = data_uri
    return localized
