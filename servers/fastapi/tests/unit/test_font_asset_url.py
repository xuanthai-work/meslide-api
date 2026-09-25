from pathlib import Path

from utils import asset_directory_utils


def test_font_url_uses_same_origin_when_api_origin_is_unset(monkeypatch):
    app_data = Path.cwd() / "test-app-data"
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))
    monkeypatch.delenv("NEXT_PUBLIC_FAST_API", raising=False)

    path = app_data / "fonts" / "A.ttf"
    assert asset_directory_utils.font_path_to_url(str(path)) == (
        "/app_data/fonts/A.ttf"
    )


def test_font_url_uses_public_api_origin(monkeypatch):
    from utils.font_uploads import _font_path_to_url

    app_data = Path.cwd() / "test-app-data"
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))
    monkeypatch.setenv("NEXT_PUBLIC_FAST_API", "https://api.example.test/")

    path = app_data / "fonts" / "A.ttf"
    assert asset_directory_utils.font_path_to_url(str(path)) == (
        "https://api.example.test/app_data/fonts/A.ttf"
    )
    assert _font_path_to_url(str(path)) == (
        "https://api.example.test/app_data/fonts/A.ttf"
    )


def test_font_path_outside_app_data_is_unchanged(monkeypatch):
    from utils.font_uploads import _font_path_to_url

    app_data = Path.cwd() / "test-app-data"
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))
    outside = app_data.parent / "outside.ttf"

    assert asset_directory_utils.font_path_to_url(str(outside)) == str(outside)
    assert _font_path_to_url(str(outside)) == str(outside)
