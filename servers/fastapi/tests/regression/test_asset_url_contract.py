from utils.asset_directory_utils import (
    filesystem_image_path_to_app_data_url,
    normalize_slide_asset_url,
)


def test_asset_urls_keep_same_origin_and_public_api_shapes(monkeypatch, tmp_path):
    app_data = tmp_path / "app_data"
    image = app_data / "images" / "users" / "owner" / "slide.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))
    monkeypatch.delenv("NEXT_PUBLIC_FAST_API", raising=False)

    path_only = "/app_data/images/users/owner/slide.png"
    assert filesystem_image_path_to_app_data_url(str(image)) == path_only

    monkeypatch.setenv("NEXT_PUBLIC_FAST_API", "https://api.example.test/")
    assert filesystem_image_path_to_app_data_url(str(image)) == (
        "https://api.example.test" + path_only
    )
    assert normalize_slide_asset_url("/static/icons/placeholder.svg") == (
        "https://api.example.test/static/icons/placeholder.svg"
    )
    remote = "https://cdn.example.test/slide.png"
    assert normalize_slide_asset_url(remote) == remote
