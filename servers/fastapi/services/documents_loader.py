import asyncio
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pdfplumber
from fastapi import HTTPException

from constants.documents import (
    IMAGE_EXTENSIONS,
    OFFICE_EXTENSIONS,
    PDF_EXTENSIONS,
    TEXT_EXTENSIONS,
)
from services.document_text import (
    _clean_extracted_one_pass as _clean_extracted_one_pass,
    _json_unescape_quoted_value as _json_unescape_quoted_value,
    _try_extract_liteparse_text_value_from_malformed_json as _try_extract_liteparse_text_value_from_malformed_json,
    _unwrap_liteparse_json_line_if_stored as _unwrap_liteparse_json_line_if_stored,
    clean_extracted_document_text,
)

from services.document_conversion_service import (
    DocumentConversionError,
    DocumentConversionService,
)
from services.liteparse_service import LiteParseError, LiteParseService
from services.office_document_service import (
    OfficeDocumentError,
    extract_office_document_text,
)
from services.temp_file_service import TEMP_FILE_SERVICE
from utils.ocr_language import presentation_language_to_ocr_code

# Optional fallback converter (primarily useful on Windows)
try:
    from services.lightweight_document_service import DocumentService as DocumentServiceCls
except Exception:
    DocumentServiceCls = None

LOGGER = logging.getLogger(__name__)


class DocumentsLoader:
    DECOMPOSE_TIMEOUT_SECONDS = 600

    def __init__(
        self,
        file_paths: List[str],
        presentation_language: Optional[str] = None,
    ):
        self._file_paths = TEMP_FILE_SERVICE.resolve_existing_temp_paths(file_paths)
        self._ocr_language = presentation_language_to_ocr_code(presentation_language)
        self.liteparse_service = LiteParseService(
            timeout_seconds=self.DECOMPOSE_TIMEOUT_SECONDS
        )
        self.document_conversion_service = DocumentConversionService()
        self.document_service: Any = (
            DocumentServiceCls() if DocumentServiceCls is not None else None
        )

        self._documents: List[str] = []
        self._images: List[List[str]] = []

    @property
    def documents(self):
        return self._documents

    @property
    def images(self):
        return self._images

    async def load_documents(
        self,
        temp_dir: Optional[str] = None,
        load_text: bool = True,
        load_images: bool = False,
    ):
        """If load_images is True, temp_dir must be provided"""

        documents: List[str] = []
        images: List[List[str]] = []

        for file_path in self._file_paths:
            if not os.path.exists(file_path):
                raise HTTPException(
                    status_code=404, detail=f"File {file_path} not found"
                )

            extension = Path(file_path).suffix.lower()
            LOGGER.info(
                "[DocumentsLoader] Processing file=%s extension=%s",
                file_path,
                extension,
            )

            document, imgs = await self._load_one_document(
                file_path, load_text, load_images, temp_dir
            )
            document = clean_extracted_document_text(document)
            documents.append(document)
            images.append(imgs)

        self._documents = documents
        self._images = images

    async def _load_one_document(
        self,
        file_path: str,
        load_text: bool,
        load_images: bool,
        temp_dir: str | None,
    ) -> tuple[str, list[str]]:
        document = ""
        imgs: list[str] = []
        extension = Path(file_path).suffix.lower()

        if extension in PDF_EXTENSIONS:
            document, imgs = await self.load_pdf(
                file_path, load_text, load_images, temp_dir
            )
        elif extension in TEXT_EXTENSIONS:
            document = await self.load_text(file_path)
        elif extension in OFFICE_EXTENSIONS:
            document = await asyncio.to_thread(
                self.load_office_document,
                file_path,
            )
        elif extension in IMAGE_EXTENSIONS:
            document = await asyncio.to_thread(
                self.load_image,
                file_path,
                temp_dir,
            )
        else:
            document = await asyncio.to_thread(self._parse_with_liteparse, file_path)

        return document, imgs

    async def load_pdf(
        self,
        file_path: str,
        load_text: bool,
        load_images: bool,
        temp_dir: Optional[str] = None,
    ) -> Tuple[str, List[str]]:
        image_paths: List[str] = []
        document: str = ""

        if load_text:
            is_scanned = await asyncio.to_thread(self._is_scanned_pdf, file_path)
            dpi = 300 if is_scanned else None
            document = await asyncio.to_thread(self._parse_with_liteparse, file_path, dpi)

        if load_images:
            if temp_dir is None:
                raise HTTPException(
                    status_code=400,
                    detail="temp_dir is required when load_images is true",
                )
            image_paths = await self.get_page_images_from_pdf_async(file_path, temp_dir)

        return document, image_paths

    @staticmethod
    def _is_scanned_pdf(file_path: str, sample_pages: int = 5, threshold: int = 50) -> bool:
        """Check if a PDF is scanned (image-only) by sampling pages for text content."""
        try:
            with pdfplumber.open(file_path) as pdf:
                total_chars = 0
                for i, page in enumerate(pdf.pages[:sample_pages]):
                    text = page.extract_text() or ""
                    total_chars += len(text.strip())
                return total_chars < threshold
        except Exception:
            return False

    async def load_text(self, file_path: str) -> str:
        with open(file_path, "r", encoding="utf-8") as file:
            return await asyncio.to_thread(file.read)

    @staticmethod
    def load_office_document(file_path: str) -> str:
        try:
            return extract_office_document_text(file_path)
        except OfficeDocumentError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    def load_image(self, file_path: str, temp_dir: Optional[str] = None) -> str:
        if temp_dir:
            converted_path = self.document_conversion_service.convert_image_to_png(
                file_path,
                temp_dir,
                timeout_seconds=self.DECOMPOSE_TIMEOUT_SECONDS,
            )
            return self._parse_with_liteparse(converted_path, dpi=300)

        with tempfile.TemporaryDirectory(prefix="image-convert-") as conversion_dir:
            converted_path = self.document_conversion_service.convert_image_to_png(
                file_path,
                conversion_dir,
                timeout_seconds=self.DECOMPOSE_TIMEOUT_SECONDS,
            )
            return self._parse_with_liteparse(converted_path, dpi=300)

    def _parse_with_liteparse(self, file_path: str, dpi: int = None) -> str:
        try:
            LOGGER.info("[DocumentsLoader] LiteParse start file=%s", file_path)
            return self.liteparse_service.parse_to_markdown(
                file_path,
                ocr_enabled=True,
                ocr_language=self._ocr_language,
                dpi=dpi,
            )
        except (LiteParseError, DocumentConversionError, OfficeDocumentError) as exc:
            LOGGER.warning(
                "[DocumentsLoader] Primary parse failed file=%s error=%s",
                file_path,
                exc,
            )
            if self.document_service is not None:
                try:
                    LOGGER.info("[DocumentsLoader] Trying fallback parser file=%s", file_path)
                    return self.document_service.parse_to_markdown(file_path)
                except Exception:
                    LOGGER.exception(
                        "[DocumentsLoader] Fallback parser failed file=%s",
                        file_path,
                    )
                    pass
            raise HTTPException(
                status_code=500,
                detail=f"Failed to parse document {os.path.basename(file_path)}: {exc}",
            ) from exc

    @classmethod
    def get_page_images_from_pdf(cls, file_path: str, temp_dir: str) -> List[str]:
        with pdfplumber.open(file_path) as pdf:
            images = []
            for page in pdf.pages:
                img = page.to_image(resolution=150)
                image_path = os.path.join(temp_dir, f"page_{page.page_number}.png")
                img.save(image_path)
                images.append(image_path)
            return images

    @classmethod
    async def get_page_images_from_pdf_async(cls, file_path: str, temp_dir: str):
        return await asyncio.to_thread(
            cls.get_page_images_from_pdf, file_path, temp_dir
        )
