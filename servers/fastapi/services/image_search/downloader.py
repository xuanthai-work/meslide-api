"""Secure remote-image import (FR-6).

The ONLY component allowed to fetch remote image bytes. Standard/Smart
resolvers must call the reviewed importer service through
``SearxngImageSearchService.search_and_import``; no presentation path may
implement its own downloader, and no endpoint accepts arbitrary client
URLs — candidates come from the internal search service only.

Security properties:

- http(s) only, standard ports only;
- every DNS answer is validated (loopback/private/link-local/multicast/
  reserved/unspecified/metadata ranges rejected) through a resolver the
  connector actually connects to, closing the DNS-rebinding window;
- redirects are followed manually: bounded, each hop re-validated;
- connect/sock-read/total timeouts;
- streamed body with a hard byte cap (declared Content-Length checked
  first);
- bytes (not the claimed Content-Type) are validated with Pillow: SVG
  rejected, format whitelist, header-level pixel cap before decode
  (decompression-bomb guard);
- re-encoded to JPEG (metadata stripped by re-encoding), written through a
  temp file and atomically promoted inside the owner-scoped images
  directory; partial files never survive a failure or cancellation;
- the ``ImageAsset`` row is only constructed after the final file exists.
"""

import asyncio
import ipaddress
import logging
import os
import tempfile
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from io import BytesIO
from typing import Optional
from urllib.parse import urljoin, urlparse

import aiohttp
from aiohttp.abc import AbstractResolver
from PIL import Image

from models.sql.image_asset import ImageAsset
from services.image_search.constants import (
    APPROVED_RASTER_FORMAT,
    IMPORTABLE_IMAGE_MIME_TYPES,
    RE_ENCODE_QUALITY,
    limits,
)
from services.image_search.models import ImageCandidateImporter, ImageSearchCandidate
from utils.asset_directory_utils import get_images_directory

LOGGER = logging.getLogger(__name__)

# Only these destination ports may be connected to.
_ALLOWED_PORTS = frozenset({80, 443})
# Pillow formats matching IMPORTABLE_IMAGE_MIME_TYPES (SVG deliberately absent).
_ALLOWED_PIL_FORMATS = frozenset({"JPEG", "PNG", "GIF", "BMP", "TIFF", "WEBP"})
_SVG_CONTENT_TYPE = "image/svg+xml"
_CHUNK_SIZE = 64 * 1024

# Machine-readable warning codes carried on ImageImportError (Phase 7):
# the candidate itself is unusable versus the fetch failing.
IMPORT_FAILURE_REJECTED = "image_search.candidate_rejected"
IMPORT_FAILURE_DOWNLOAD = "image_search.download_failed"


class ImageImportError(Exception):
    """One candidate is unusable; try the next one. Log-safe detail only.

    ``reason`` is the machine-readable Phase 7 warning code deciding which
    localized message the slot warning shows: ``IMPORT_FAILURE_REJECTED``
    for unsafe or invalid candidates, ``IMPORT_FAILURE_DOWNLOAD`` for
    transport failures. Timeouts are not raised as ``ImageImportError``;
    they surface as ``TimeoutError`` and are classified by the service.
    """

    def __init__(self, message: str, reason: str = IMPORT_FAILURE_DOWNLOAD):
        super().__init__(message)
        self.reason = reason


def is_safe_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """True only for public, non-special-purpose destinations."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped:
            address = address.ipv4_mapped
    return not (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or not address.is_global
    )


class _ValidatingResolver(AbstractResolver):
    """Resolver that refuses to return non-public addresses.

    The TCP connector connects to exactly the addresses this resolver
    returns, so validating here (rather than in a pre-flight check) pins
    the connected peer and closes the DNS-rebinding window.
    """

    def __init__(self, inner: AbstractResolver):
        self._inner = inner

    async def resolve(self, host, port, family=0):
        results = await self._inner.resolve(host, port, family)
        for result in results:
            resolved_host = (
                result.get("host")
                if isinstance(result, Mapping)
                else getattr(result, "host", None)
            )
            if not isinstance(resolved_host, str):
                raise ImageImportError(
                    f"unresolvable host: {host}",
                    reason=IMPORT_FAILURE_REJECTED,
                )
            try:
                address = ipaddress.ip_address(resolved_host)
            except (TypeError, ValueError):
                raise ImageImportError(
                    f"unresolvable host: {host}",
                    reason=IMPORT_FAILURE_REJECTED,
                )
            if not is_safe_ip(address):
                raise ImageImportError(
                    f"refusing non-public destination: {host}",
                    reason=IMPORT_FAILURE_REJECTED,
                )
        return results

    async def close(self) -> None:
        await self._inner.close()


def validate_import_url(url: str) -> None:
    """Reject non-http(s) URLs and non-standard ports before any I/O."""
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError as error:
        raise ImageImportError(
            f"malformed URL: {url!r}", reason=IMPORT_FAILURE_REJECTED
        ) from error
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ImageImportError(f"invalid scheme or host: {url!r}", reason=IMPORT_FAILURE_REJECTED)
    if port is not None and port not in _ALLOWED_PORTS:
        raise ImageImportError(f"refusing non-standard port: {port}", reason=IMPORT_FAILURE_REJECTED)


def _looks_like_svg(data: bytes, content_type: str) -> bool:
    if content_type == _SVG_CONTENT_TYPE:
        return True
    head = data[:2048].lstrip().lower()
    if head.startswith(b"<?xml"):
        return b"<svg" in head
    return b"<svg" in head[:512]


def validate_and_reencode(data: bytes, content_type: str) -> tuple[bytes, int, int]:
    """Validate image bytes and return (jpeg_bytes, width, height).

    The Content-Type header is advisory only: bytes decide. Header-level
    dimensions are checked before any decode, so a decompression bomb is
    rejected without ever expanding its pixels.
    """
    if _looks_like_svg(data, (content_type or "").split(";")[0].strip().lower()):
        raise ImageImportError("SVG is not importable in the first release", reason=IMPORT_FAILURE_REJECTED)

    try:
        with Image.open(BytesIO(data)) as probe:
            fmt = (probe.format or "").upper()
            if fmt not in _ALLOWED_PIL_FORMATS:
                raise ImageImportError(f"unsupported image format: {fmt}", reason=IMPORT_FAILURE_REJECTED)
            width, height = probe.size
    except OSError as error:
        # UnidentifiedImageError (and friends) are OSError subclasses.
        raise ImageImportError(
            "bytes are not a decodable image", reason=IMPORT_FAILURE_REJECTED
        ) from error

    if width <= 0 or height <= 0:
        raise ImageImportError("image has no pixels", reason=IMPORT_FAILURE_REJECTED)
    if width * height > limits.max_image_pixels:
        raise ImageImportError(
            f"image exceeds pixel limit: {width}x{height}",
            reason=IMPORT_FAILURE_REJECTED,
        )

    try:
        with Image.open(BytesIO(data)) as image:
            if fmt == "GIF":
                image.seek(0)
            decoded = image.convert("RGBA")
            if decoded.width != width or decoded.height != height:
                raise ImageImportError("dimensions changed during decode", reason=IMPORT_FAILURE_REJECTED)
            flattened = Image.new("RGB", decoded.size, (255, 255, 255))
            flattened.paste(decoded, mask=decoded.getchannel("A"))
            output = BytesIO()
            flattened.save(
                output,
                format=APPROVED_RASTER_FORMAT.upper(),
                quality=RE_ENCODE_QUALITY,
            )
    except ImageImportError:
        raise
    except Exception as error:
        raise ImageImportError(
            f"image decode failed: {error}", reason=IMPORT_FAILURE_REJECTED
        ) from error
    return output.getvalue(), width, height


class SsrSafeImageImporter(ImageCandidateImporter):
    """Downloads one candidate into a validated, owner-scoped local asset."""

    def __init__(self, images_directory: Optional[str] = None):
        # Default resolves per-import through get_images_directory() so
        # files land in the calling owner's directory.
        self._images_directory = images_directory
        self._semaphore: asyncio.Semaphore | None = None

    def _get_semaphore(self) -> asyncio.Semaphore:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(limits.max_concurrent_imports)
        return self._semaphore

    def _client_timeout(self) -> aiohttp.ClientTimeout:
        return aiohttp.ClientTimeout(
            connect=limits.connect_timeout_seconds,
            sock_read=limits.read_timeout_seconds,
            total=limits.import_total_timeout_seconds,
        )

    async def import_candidate(
        self,
        candidate: ImageSearchCandidate,
        *,
        owner_id=None,
        query: str | None = None,
    ) -> ImageAsset:
        validate_import_url(candidate.image_url)
        async with self._get_semaphore():
            data, content_type = await self._download(candidate.image_url)
        encoded, width, height = validate_and_reencode(data, content_type)
        final_path = self._write_atomically(encoded)
        LOGGER.info(
            "Image import succeeded: engine=%s width=%d height=%d bytes=%d",
            candidate.engine,
            width,
            height,
            len(encoded),
        )
        return ImageAsset(  # pyright: ignore[reportCallIssue] -- SQLModel fills the database default.
            owner_id=owner_id,
            path=final_path,
            is_uploaded=False,
            extras={
                "origin": "web_search",
                "provider": "searxng",
                "query": query or "",
                "title": candidate.title,
                "source_page_url": candidate.source_page_url,
                "original_image_url": candidate.image_url,
                "engine": candidate.engine,
                "size_bytes": len(encoded),
                "imported_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    async def _download(self, url: str) -> tuple[bytes, str]:
        """Fetch the body with validated destinations and bounded redirects."""
        connector = aiohttp.TCPConnector(
            # DefaultResolver is thread-based: aiodns is not a dependency.
            resolver=_ValidatingResolver(aiohttp.DefaultResolver()),
        )
        try:
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=self._client_timeout(),
                headers={"User-Agent": "Presenton/1.0"},
            ) as session:
                return await self._download_via_session(session, url)
        finally:
            await connector.close()

    async def _download_via_session(
        self, session: aiohttp.ClientSession, url: str
    ) -> tuple[bytes, str]:
        current_url = url
        for _hop in range(limits.max_redirects + 1):
            async with session.get(current_url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise ImageImportError("redirect without Location", reason=IMPORT_FAILURE_DOWNLOAD)
                    current_url = urljoin(current_url, location)
                    validate_import_url(current_url)
                    continue
                if response.status != 200:
                    raise ImageImportError(f"download failed: HTTP {response.status}", reason=IMPORT_FAILURE_DOWNLOAD)
                content_type = (response.content_type or "").lower()
                if content_type == _SVG_CONTENT_TYPE:
                    raise ImageImportError("remote SVG rejected", reason=IMPORT_FAILURE_REJECTED)
                if content_type and (
                    not content_type.startswith("image/")
                    or content_type not in IMPORTABLE_IMAGE_MIME_TYPES
                ):
                    raise ImageImportError(f"unexpected content type: {content_type}", reason=IMPORT_FAILURE_REJECTED)
                declared = response.content_length
                if declared is not None and declared > limits.max_download_bytes:
                    raise ImageImportError(
                        f"declared body too large: {declared}"
                    )
                body = bytearray()
                async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                    body.extend(chunk)
                    if len(body) > limits.max_download_bytes:
                        raise ImageImportError("streamed body exceeded byte cap", reason=IMPORT_FAILURE_REJECTED)
                return bytes(body), content_type
        raise ImageImportError("too many redirects", reason=IMPORT_FAILURE_DOWNLOAD)

    def _write_atomically(self, encoded: bytes) -> str:
        images_directory = self._images_directory or get_images_directory()
        os.makedirs(images_directory, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix="search-import-", suffix=f".{APPROVED_RASTER_FORMAT}", dir=images_directory
        )
        try:
            with os.fdopen(fd, "wb") as temp_file:
                temp_file.write(encoded)
            final_path = os.path.join(
                images_directory, f"{uuid.uuid4()}.{APPROVED_RASTER_FORMAT}"
            )
            os.replace(temp_path, final_path)
        except BaseException:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise
        return final_path
