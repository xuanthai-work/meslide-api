"""Cached document-profile construction for document-aware prompt rewrite.

Reads the user's decomposed document texts (through the owner-scoped
``TempFileService`` validation methods), builds a compact ``DocumentProfile``
once per (content, language) pair, and caches it on disk inside the current
owner's temporary directory. Cache entries are content-hash keyed and
version-tagged, written atomically, and guarded per key by an in-process
single-flight lock; concurrent or cross-process duplicate computation can
still occur but remains safe because every writer replaces the cache file
atomically and readers re-check after acquiring the lock.

Logging discipline: this module logs metadata only (file counts, character
counts, cache hits, durations, error types). Raw document text, generated
profiles, and prompt content are never logged.
"""

import asyncio
import hashlib
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Optional, Sequence

from fastapi import HTTPException
from pydantic import ValidationError

from models.document_profile import DocumentProfile
from services.temp_file_service import TempFileService, get_temp_file_service
from utils.coded_exception import CodedHTTPException
from utils.llm_client_error_handler import handle_llm_client_exceptions
from utils.llm_calls.generate_document_profile import (
    DIRECT_PROFILE_MAX_INPUT_CHARACTERS,
    profile_documents,
)
from utils.llm_provider import get_llm_client, get_llm_provider, get_model
from utils.llm_utils import DisconnectChecker

LOGGER = logging.getLogger(__name__)

# Bump when the profile shape, profiling prompts, or bounds change in a way
# that should invalidate previously cached profiles.
DOCUMENT_PROFILE_VERSION = 2

CACHE_DIR_NAME = "document-profiles"

DOCUMENTS_REQUIRED_ERROR_CODE = "error.document_profile.documents_required"
DOCUMENT_NOT_FOUND_ERROR_CODE = "error.document_profile.document_not_found"
DOCUMENT_INVALID_ERROR_CODE = "error.document_profile.document_invalid"
PROFILE_BUILD_FAILED_ERROR_CODE = "error.document_profile.build_failed"

# Per-key single-flight registry. Best effort within this process; safety of
# duplicate computation relies on atomic cache writes plus the post-lock
# cache re-check.
_flight_locks: dict[str, asyncio.Lock] = {}
_flight_registry_guard = asyncio.Lock()


class DocumentProfileService:
    """Builds and caches ``DocumentProfile`` instances for decomposed files."""

    def __init__(self, temp_file_service: Optional[TempFileService] = None):
        self._temp_file_service = temp_file_service

    @property
    def temp_file_service(self) -> TempFileService:
        if self._temp_file_service is None:
            self._temp_file_service = get_temp_file_service()
        return self._temp_file_service

    async def get_or_build_profile(
        self,
        file_paths: Sequence[str],
        language: Optional[str] = None,
        display_names: Optional[Sequence[str]] = None,
        disconnect_checker: Optional[DisconnectChecker] = None,
    ) -> DocumentProfile:
        """Return the cached profile for ``file_paths`` or build it once.

        ``file_paths`` are the user's decomposed document text files. Every
        read goes through ``TempFileService`` validation, so missing,
        out-of-root, and cross-owner paths fail with the service's coded
        4xx errors. ``display_names`` are optional friendly labels aligned
        with ``file_paths``; without them documents are labeled
        ``Document 1..N``.
        """
        started = time.perf_counter()
        if not file_paths:
            raise CodedHTTPException(
                status_code=400,
                detail="Documents are required",
                error_code=DOCUMENTS_REQUIRED_ERROR_CODE,
            )

        # Reads go through the owner-scoped validation methods; a stale or
        # inaccessible file surfaces as a coded error clients can detect to
        # re-prepare their local documents (no silent document-less rewrite).
        try:
            contents = [
                self.temp_file_service.read_temp_file(path, binary=False)
                for path in file_paths
            ]
        except HTTPException as exc:
            if exc.status_code == 404:
                raise CodedHTTPException(
                    status_code=404,
                    detail=(
                        "One of the documents could not be found on the "
                        "server. Please re-attach your files and try again."
                    ),
                    error_code=DOCUMENT_NOT_FOUND_ERROR_CODE,
                ) from exc
            raise CodedHTTPException(
                status_code=400,
                detail=(
                    "One of the documents is not accessible from this "
                    "session. Please re-attach your files and try again."
                ),
                error_code=DOCUMENT_INVALID_ERROR_CODE,
            ) from exc
        labels = list(display_names or []) or [
            f"Document {index + 1}" for index in range(len(contents))
        ]
        documents = list(zip(labels, contents))

        cache_key = self._profile_cache_key(contents, language)
        cached = self._load_cached_profile(cache_key)
        if cached is not None:
            LOGGER.info(
                "document_profile cache_hit file_count=%d total_characters=%d",
                len(contents),
                sum(len(content) for content in contents),
            )
            return cached

        async with self._single_flight(cache_key):
            # Another caller may have finished while this one waited.
            cached = self._load_cached_profile(cache_key)
            if cached is not None:
                LOGGER.info(
                    "document_profile cache_hit_after_wait file_count=%d "
                    "total_characters=%d",
                    len(contents),
                    sum(len(content) for content in contents),
                )
                return cached

            profile = await self._build_profile(
                documents, language, disconnect_checker
            )
            self._store_cached_profile(cache_key, profile)
            LOGGER.info(
                "document_profile built file_count=%d total_characters=%d "
                "duration_seconds=%.3f",
                len(contents),
                sum(len(content) for content in contents),
                time.perf_counter() - started,
            )
            return profile

    async def _build_profile(
        self,
        documents: Sequence[tuple[str, str]],
        language: Optional[str],
        disconnect_checker: Optional[DisconnectChecker],
    ) -> DocumentProfile:
        provider_label = "unknown"
        model = "unknown"
        started = time.perf_counter()
        try:
            provider_label = get_llm_provider().value
            model = get_model()
            client = get_llm_client()
            content = await profile_documents(
                client,
                model,
                documents,
                language=language,
                disconnect_checker=disconnect_checker,
            )
            profile = DocumentProfile(**content)
            LOGGER.info(
                "document_profile llm_success provider=%s model=%s "
                "chunked=%s duration_seconds=%.3f",
                provider_label,
                model,
                sum(len(text) for _, text in documents)
                > DIRECT_PROFILE_MAX_INPUT_CHARACTERS,
                time.perf_counter() - started,
            )
            return profile
        except ValidationError as e:
            LOGGER.warning(
                "document_profile invalid_output provider=%s model=%s "
                "duration_seconds=%.3f error_type=ValidationError",
                provider_label,
                model,
                time.perf_counter() - started,
            )
            raise CodedHTTPException(
                status_code=502,
                detail=(
                    "The document analysis did not match the expected format. "
                    "Please try again."
                ),
                error_code=PROFILE_BUILD_FAILED_ERROR_CODE,
            ) from e
        except Exception as e:
            LOGGER.warning(
                "document_profile failure provider=%s model=%s "
                "duration_seconds=%.3f error_type=%s",
                provider_label,
                model,
                time.perf_counter() - started,
                type(e).__name__,
            )
            raise handle_llm_client_exceptions(e)

    @staticmethod
    def _profile_cache_key(contents: Sequence[str], language: Optional[str]) -> str:
        """Hash profile-relevant inputs: content, normalized language, version."""
        normalized_language = (language or "").strip().casefold()
        hasher = hashlib.sha256()
        hasher.update(str(DOCUMENT_PROFILE_VERSION).encode("utf-8"))
        hasher.update(b"\x00")
        hasher.update(normalized_language.encode("utf-8"))
        for content in contents:
            hasher.update(b"\x00")
            hasher.update(hashlib.sha256(content.encode("utf-8")).digest())
        return hasher.hexdigest()

    def _cache_file_path(self, cache_key: str) -> str:
        cache_dir = self.temp_file_service.create_temp_dir(CACHE_DIR_NAME)
        return self.temp_file_service.create_temp_file_path(
            f"{cache_key}.json", cache_dir
        )

    def _load_cached_profile(self, cache_key: str) -> Optional[DocumentProfile]:
        """Load a cached profile, treating any corruption as a miss."""
        cache_path = self._cache_file_path(cache_key)
        if not os.path.exists(cache_path):
            return None
        try:
            with open(cache_path, "r", encoding="utf-8") as cache_file:
                data = json.load(cache_file)
            return DocumentProfile(**data)
        except (OSError, ValueError, TypeError, ValidationError):
            # A truncated or unreadable entry is rebuilt, not surfaced: the
            # caller simply pays one extra profiling call.
            LOGGER.info(
                "document_profile cache_corrupt cache_key_prefix=%s",
                cache_key[:12],
            )
            return None

    def _store_cached_profile(self, cache_key: str, profile: DocumentProfile) -> None:
        """Persist a profile atomically (temp file + replace in one dir)."""
        cache_path = self._cache_file_path(cache_key)
        staging_path = f"{cache_path}.tmp"
        with open(staging_path, "w", encoding="utf-8") as staging_file:
            json.dump(
                profile.model_dump(mode="json"),
                staging_file,
                ensure_ascii=False,
            )
        os.replace(staging_path, cache_path)

    @staticmethod
    @asynccontextmanager
    async def _single_flight(cache_key: str):
        """Serialize profile builds for one cache key within this process.

        Registry cleanup is opportunistic: if another waiter already queued
        on this lock, the entry stays for it. A raced removal can at worst
        duplicate one build, which stays safe through atomic cache writes
        and the post-lock re-check.
        """
        async with _flight_registry_guard:
            lock = _flight_locks.get(cache_key)
            if lock is None:
                lock = asyncio.Lock()
                _flight_locks[cache_key] = lock
        try:
            async with lock:
                yield
        finally:
            if not lock.locked() and _flight_locks.get(cache_key) is lock:
                _flight_locks.pop(cache_key, None)
