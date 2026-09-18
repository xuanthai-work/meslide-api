"""Guard tests: SearXNG is a search engine, never an AI image provider.

docs/gslide-searxng-image-search-spec.md NFR-1: SearXNG must never be
accepted as an `IMAGE_PROVIDER` value, and the image-search package must
stay independent of `ImageGenerationService`.
"""

from pathlib import Path

import pytest

from enums.image_provider import ImageProvider

IMAGE_SEARCH_PACKAGE = Path(__file__).parents[2] / "services" / "image_search"


def test_searxng_is_not_an_image_provider_value():
    values = {member.value for member in ImageProvider}
    assert "searxng" not in values
    assert not any("searx" in value for value in values)


def test_searxng_cannot_be_parsed_as_image_provider():
    with pytest.raises(ValueError):
        ImageProvider("searxng")


def test_image_search_package_does_not_reference_image_generation():
    import ast

    offenders = []
    for py_file in IMAGE_SEARCH_PACKAGE.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                if name in ("services.image_generation_service", "utils.image_provider"):
                    offenders.append(f"{py_file.name}: import {name}")
        # Also catch plain Name/Attribute usages of the banned symbols.
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in (
                "ImageGenerationService",
                "ImageProvider",
            ):
                offenders.append(f"{py_file.name}: {node.id}")
            if isinstance(node, ast.Attribute) and node.attr in (
                "ImageGenerationService",
                "image_generation_service",
            ):
                offenders.append(f"{py_file.name}: .{node.attr}")
    assert not offenders, (
        "the image_search package must not depend on the AI image generation "
        f"path: {sorted(set(offenders))}"
    )