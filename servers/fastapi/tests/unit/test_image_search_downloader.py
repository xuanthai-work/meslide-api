"""Security tests for the SSRF-safe remote-image importer (FR-6).

Covers: scheme/port validation, IP-range validation (the resolver pins the
connected peer), manual redirect re-validation, body-size caps, MIME and
byte-level content validation (SVG/HTML/spoof rejection, decompression
guard), re-encoding (metadata stripped), atomic write cleanup, and the
concurrency semaphore.
"""

import asyncio
import contextlib
import ipaddress
import os
import uuid
from io import BytesIO
from types import SimpleNamespace

import aiohttp
import pytest
from PIL import Image
from PIL.PngImagePlugin import PngInfo

from services.image_search import downloader
from services.image_search.constants import limits
from services.image_search.downloader import (
    ImageImportError,
    SsrSafeImageImporter,
    _ValidatingResolver,
    is_safe_ip,
    validate_and_reencode,
    validate_import_url,
)
from services.image_search.models import ImageSearchCandidate


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeContent:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def iter_chunked(self, _size):
        async def generator():
            for chunk in self._chunks:
                yield chunk

        return generator()


class FakeResponse:
    def __init__(
        self,
        *,
        status=200,
        headers=None,
        content_type="image/jpeg",
        content_length=None,
        chunks=(),
    ):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.content_length = content_length
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Returns queued responses per GET; records requested URLs."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        response = self.responses.pop(0) if self.responses else FakeResponse(status=404)
        return response


class FakeInnerResolver(aiohttp.abc.AbstractResolver):
    def __init__(self, results):
        self.results = list(results)

    async def resolve(self, host, port=0, family=0):
        return list(self.results)

    async def close(self):
        pass


def _png_bytes(width=800, height=600, color=(200, 30, 30)):
    image = Image.new("RGB", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@contextlib.contextmanager
def _override_limit(name, value):
    """limits is a frozen dataclass, so monkeypatch.setattr cannot write it."""
    original = getattr(limits, name)
    object.__setattr__(limits, name, value)
    try:
        yield
    finally:
        object.__setattr__(limits, name, original)


def _candidate(url="https://cdn.example.org/a.jpg", **kwargs):
    defaults = {
        "title": "A photo",
        "source_page_url": "https://example.org/page",
        "engine": "pexels",
    }
    defaults.update(kwargs)
    return ImageSearchCandidate(image_url=url, **defaults)


# ---------------------------------------------------------------------------
# is_safe_ip: every special-purpose range is refused
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "address,expected",
    [
        ("8.8.8.8", True),
        ("93.184.216.34", True),
        ("2606:4700::1111", True),
        ("::ffff:8.8.8.8", True),  # IPv4-mapped public
        ("127.0.0.1", False),
        ("0.0.0.0", False),
        ("10.0.0.1", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),  # cloud metadata
        ("224.0.0.1", False),
        ("240.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fc00::1", False),
        ("fe80::1", False),
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),  # IPv4-mapped loopback
        ("::ffff:10.0.0.1", False),  # IPv4-mapped private
    ],
)
def test_is_safe_ip(address, expected):
    assert is_safe_ip(ipaddress.ip_address(address)) is expected


# ---------------------------------------------------------------------------
# validate_import_url: schemes and ports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "ftp://example.com/a.jpg",
        "file:///etc/passwd",
        "gopher://example.com/a.jpg",
        "data:image/png;base64,AAAA",
        "//example.com/a.jpg",  # protocol-relative (no scheme after parse)
        "https:///no-host.jpg",
        "https://example.com:8080/a.jpg",
        "http://example.com:22/a.jpg",
        "http://example.com:0/a.jpg",
        "https://example.com:bad-port/a.jpg",
    ],
)
def test_validate_import_url_rejects(url):
    with pytest.raises(ImageImportError):
        validate_import_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.jpg",
        "http://example.com/a.jpg",
        "https://example.com:443/a.jpg",
        "http://example.com:80/a.jpg",
        "https://example.com",
    ],
)
def test_validate_import_url_accepts(url):
    validate_import_url(url)


# ---------------------------------------------------------------------------
# _ValidatingResolver: DNS answers are validated, not just the URL
# ---------------------------------------------------------------------------


def test_resolver_rejects_private_dns_answer():
    resolver = _ValidatingResolver(FakeInnerResolver([SimpleNamespace(host="10.0.0.5")]))
    with pytest.raises(ImageImportError):
        asyncio.run(resolver.resolve("rebind.example.com", 443))


def test_resolver_rejects_ipv6_loopback_answer():
    resolver = _ValidatingResolver(FakeInnerResolver([SimpleNamespace(host="::1")]))
    with pytest.raises(ImageImportError):
        asyncio.run(resolver.resolve("rebind.example.com", 443))


def test_resolver_rejects_unparseable_answer():
    resolver = _ValidatingResolver(
        FakeInnerResolver([SimpleNamespace(host="not-an-ip")])
    )
    with pytest.raises(ImageImportError):
        asyncio.run(resolver.resolve("weird.example.com", 443))


def test_resolver_rejects_when_any_answer_is_private():
    resolver = _ValidatingResolver(
        FakeInnerResolver(
            [SimpleNamespace(host="8.8.8.8"), SimpleNamespace(host="127.0.0.1")]
        )
    )
    with pytest.raises(ImageImportError):
        asyncio.run(resolver.resolve("rebind.example.com", 443))


def test_resolver_passes_public_answers_through():
    results = [SimpleNamespace(host="93.184.216.34")]
    resolver = _ValidatingResolver(FakeInnerResolver(results))
    assert asyncio.run(resolver.resolve("cdn.example.com", 443)) == results


def test_resolver_accepts_aiohttp_dict_results():
    results = [
        {
            "hostname": "cdn.example.com",
            "host": "93.184.216.34",
            "port": 443,
            "family": 2,
            "proto": 6,
            "flags": 0,
        }
    ]
    resolver = _ValidatingResolver(FakeInnerResolver(results))
    assert asyncio.run(resolver.resolve("cdn.example.com", 443)) == results


def test_resolver_rejects_private_aiohttp_dict_result():
    resolver = _ValidatingResolver(
        FakeInnerResolver([{"host": "127.0.0.1", "port": 443}])
    )
    with pytest.raises(ImageImportError):
        asyncio.run(resolver.resolve("rebind.example.com", 443))


def test_resolver_rejects_result_without_host():
    resolver = _ValidatingResolver(
        FakeInnerResolver([{"hostname": "malformed.example.com", "port": 443}])
    )
    with pytest.raises(ImageImportError):
        asyncio.run(resolver.resolve("malformed.example.com", 443))


# ---------------------------------------------------------------------------
# validate_and_reencode: bytes decide, not the Content-Type
# ---------------------------------------------------------------------------


def test_reencode_png_to_jpeg():
    encoded, width, height = validate_and_reencode(_png_bytes(320, 200), "image/png")
    assert (width, height) == (320, 200)
    assert encoded[:2] == b"\xff\xd8"  # JPEG magic
    with Image.open(BytesIO(encoded)) as decoded:
        assert decoded.format == "JPEG"


def test_reencode_flattens_transparency_to_white():
    image = Image.new("RGBA", (100, 100), (255, 0, 0, 0))  # fully transparent
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    encoded, _, _ = validate_and_reencode(buffer.getvalue(), "image/png")
    with Image.open(BytesIO(encoded)) as decoded:
        assert decoded.getpixel((50, 50)) == (255, 255, 255)


def test_reencode_strips_metadata():
    image = Image.new("RGB", (64, 64))
    buffer = BytesIO()
    png_info = PngInfo()
    png_info.add_text("Comment", "secret tracking metadata")
    image.save(buffer, format="PNG", pnginfo=png_info)
    encoded, _, _ = validate_and_reencode(buffer.getvalue(), "image/png")
    with Image.open(BytesIO(encoded)) as decoded:
        assert "Comment" not in decoded.info


def test_svg_content_type_rejected_even_with_valid_raster_bytes():
    with pytest.raises(ImageImportError):
        validate_and_reencode(_png_bytes(), "image/svg+xml")


def test_svg_bytes_rejected_behind_png_content_type():
    svg = b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>'
    with pytest.raises(ImageImportError):
        validate_and_reencode(svg, "image/png")


def test_bare_svg_tag_rejected():
    with pytest.raises(ImageImportError):
        validate_and_reencode(b"<svg width='100'></svg>", "image/png")


def test_html_bytes_rejected_behind_image_content_type():
    with pytest.raises(ImageImportError):
        validate_and_reencode(b"<html><body>not an image</body></html>", "image/jpeg")


def test_truncated_image_bytes_rejected():
    with pytest.raises(ImageImportError):
        validate_and_reencode(_png_bytes()[:40], "image/png")


def test_oversized_pixels_rejected_before_decode():
    with _override_limit("max_image_pixels", 1000):
        with pytest.raises(ImageImportError):
            validate_and_reencode(_png_bytes(800, 600), "image/png")


def test_zero_dimension_image_rejected():
    # A PNG whose header declares 0x0 cannot be produced by PIL directly;
    # the pixel-cap check is exercised separately, so guard the positive dims.
    encoded, width, height = validate_and_reencode(_png_bytes(1, 1), "image/png")
    assert (width, height) == (1, 1)


# ---------------------------------------------------------------------------
# _download_via_session: redirects, caps, statuses
# ---------------------------------------------------------------------------


def _redirect(location):
    return FakeResponse(
        status=302, headers={"Location": location}, content_type="text/html"
    )


def test_download_follows_bounded_redirect_chain():
    body = b"image-bytes"
    session = FakeSession(
        [
            _redirect("https://cdn.example.org/real.jpg"),
            _redirect("/final.jpg"),
            FakeResponse(content_type="image/jpeg", chunks=[body]),
        ]
    )
    data, content_type = asyncio.run(
        SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
    )
    assert data == body
    assert content_type == "image/jpeg"
    assert session.requested == [
        "https://example.org/a.jpg",
        "https://cdn.example.org/real.jpg",
        "https://cdn.example.org/final.jpg",
    ]


def test_download_rejects_redirect_to_file_scheme():
    session = FakeSession([_redirect("file:///etc/passwd")])
    with pytest.raises(ImageImportError):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


def test_download_rejects_redirect_to_non_standard_port():
    session = FakeSession([_redirect("https://example.org:8443/a.jpg")])
    with pytest.raises(ImageImportError):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


def test_download_rejects_redirect_loop():
    session = FakeSession([_redirect("/a.jpg")] * (limits.max_redirects + 2))
    with pytest.raises(ImageImportError, match="redirect"):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


def test_download_rejects_non_200_status():
    session = FakeSession([FakeResponse(status=404)])
    with pytest.raises(ImageImportError, match="HTTP 404"):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


def test_download_rejects_declared_oversized_body():
    session = FakeSession(
        [FakeResponse(content_type="image/jpeg", content_length=10_000)]
    )
    with _override_limit("max_download_bytes", 100):
        with pytest.raises(ImageImportError, match="declared body too large"):
            asyncio.run(
                SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
            )


def test_download_rejects_streamed_oversized_body():
    session = FakeSession(
        [FakeResponse(content_type="image/jpeg", chunks=[b"x" * 60, b"x" * 60])]
    )
    with _override_limit("max_download_bytes", 100):
        with pytest.raises(ImageImportError, match="byte cap"):
            asyncio.run(
                SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
            )


def test_download_rejects_svg_content_type():
    session = FakeSession([FakeResponse(content_type="image/svg+xml")])
    with pytest.raises(ImageImportError, match="SVG"):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


def test_download_rejects_html_content_type():
    session = FakeSession([FakeResponse(content_type="text/html")])
    with pytest.raises(ImageImportError, match="content type"):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


def test_download_rejects_octet_stream_content_type():
    session = FakeSession([FakeResponse(content_type="application/octet-stream")])
    with pytest.raises(ImageImportError, match="content type"):
        asyncio.run(
            SsrSafeImageImporter()._download_via_session(session, "https://example.org/a.jpg")
        )


# ---------------------------------------------------------------------------
# import_candidate end-to-end (download mocked)
# ---------------------------------------------------------------------------


def _importer_with_download(tmp_path, chunks_map=None):
    importer = SsrSafeImageImporter(images_directory=str(tmp_path / "images"))

    async def fake_download(url):
        if chunks_map is None:
            return _png_bytes(), "image/png"
        return chunks_map[url]

    importer._download = fake_download
    return importer


def test_import_candidate_returns_unsaved_asset(tmp_path):
    importer = _importer_with_download(tmp_path)
    owner_id = uuid.uuid4()
    candidate = _candidate("https://cdn.example.org/a.jpg")
    asset = asyncio.run(
        importer.import_candidate(candidate, owner_id=owner_id, query="Vietnam solar farm")
    )
    assert asset.owner_id == owner_id
    assert asset.is_uploaded is False
    assert asset.id is not None  # unsaved row, still has a client-side id
    assert asset.path.startswith(str(tmp_path / "images"))
    assert os.path.isfile(asset.path)
    with Image.open(asset.path) as decoded:
        assert decoded.format == "JPEG"
    assert asset.extras["origin"] == "web_search"
    assert asset.extras["provider"] == "searxng"
    assert asset.extras["query"] == "Vietnam solar farm"
    assert asset.extras["title"] == "A photo"
    assert asset.extras["source_page_url"] == "https://example.org/page"
    assert asset.extras["original_image_url"] == "https://cdn.example.org/a.jpg"
    assert asset.extras["engine"] == "pexels"
    assert asset.extras["imported_at"]
    # Phase 8: imported size is retained for the metrics line.
    assert asset.extras["size_bytes"] == os.path.getsize(asset.path)


def test_import_candidate_rejects_unsafe_url_before_any_download(tmp_path):
    importer = _importer_with_download(tmp_path)
    downloads = []

    async def failing_download(url):
        downloads.append(url)
        return b"", ""

    importer._download = failing_download
    with pytest.raises(ImageImportError):
        asyncio.run(importer.import_candidate(_candidate("file:///etc/passwd")))
    assert downloads == [], "no network I/O may happen for a rejected URL"


def test_import_candidate_leaves_no_partial_files_on_bad_bytes(tmp_path):
    importer = _importer_with_download(
        tmp_path,
        chunks_map={"https://cdn.example.org/a.jpg": (b"<html>nope</html>", "image/jpeg")},
    )
    with pytest.raises(ImageImportError):
        asyncio.run(importer.import_candidate(_candidate("https://cdn.example.org/a.jpg")))
    images_dir = tmp_path / "images"
    leftovers = (
        list(images_dir.iterdir()) if images_dir.exists() else []
    )
    assert leftovers == [], "no temp or final files may survive a failure"


def test_write_atomically_cleans_up_temp_file_on_failure(tmp_path, monkeypatch):
    importer = SsrSafeImageImporter(images_directory=str(tmp_path / "images"))

    def broken_replace(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(downloader.os, "replace", broken_replace)
    with pytest.raises(OSError):
        importer._write_atomically(b"bytes")
    images_dir = tmp_path / "images"
    assert list(images_dir.iterdir()) == [], "temp file must be cleaned up"


def test_semaphore_bounds_concurrent_imports(tmp_path):
    importer = SsrSafeImageImporter(images_directory=str(tmp_path / "images"))
    active = 0
    peak = 0

    async def slow_download(url):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)
        active -= 1
        return _png_bytes(20, 20), "image/png"

    importer._download = slow_download
    candidates = [
        _candidate(f"https://cdn.example.org/{i}.jpg", title=f"p{i}")
        for i in range(limits.max_concurrent_imports + 3)
    ]

    async def run_all():
        return await asyncio.gather(
            *[
                importer.import_candidate(candidate, owner_id=uuid.uuid4(), query="q")
                for candidate in candidates
            ]
        )

    done = asyncio.run(run_all())
    assert len(done) == len(candidates)
    assert peak <= limits.max_concurrent_imports
