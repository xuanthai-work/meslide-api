from services.export_image_assets import _localize_json_image_assets


def test_remote_image_source_is_preserved():
    source = [{"type": "image", "data": "https://cdn.example.test/image.png"}]

    assert _localize_json_image_assets(source) == source


def test_local_image_source_embeds_file_bytes(monkeypatch, tmp_path):
    image = tmp_path / "image.png"
    image.write_bytes(b"png")
    monkeypatch.setattr(
        "services.export_image_assets.resolve_app_path_to_filesystem",
        lambda _path: str(image),
    )
    source = [{"type": "image", "data": "/app_data/images/image.png"}]

    assert _localize_json_image_assets(source)[0]["data"] == (
        "data:image/png;base64,cG5n"
    )
