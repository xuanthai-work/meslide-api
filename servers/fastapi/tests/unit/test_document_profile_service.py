"""Unit tests for the cached document-profile service."""

import asyncio
import json
import logging
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from api.v1.auth.context import reset_current_owner_id, set_current_owner_id
from models.document_profile import DocumentProfile
from services.document_profile_service import (
    CACHE_DIR_NAME,
    DOCUMENT_PROFILE_VERSION,
    DOCUMENT_INVALID_ERROR_CODE,
    DOCUMENT_NOT_FOUND_ERROR_CODE,
    DOCUMENTS_REQUIRED_ERROR_CODE,
    PROFILE_BUILD_FAILED_ERROR_CODE,
    DocumentProfileService,
)
from utils.coded_exception import CodedHTTPException

PROFILE_PAYLOAD = {
    "source_names": ["Document 1"],
    "primary_topic": "Q3 2026 retention decline",
    "stated_purpose": "Report the verified retention decline and recovery plan",
    "stated_audience": "Finance leaders",
    "sections": [{"title": "Decline", "summary": "Retention fell 12% year over year"}],
    "entities": ["OrbitDB"],
    "claims": [
        {
            "text": "The document states retention fell 12% in Q3 2026",
            "source_name": "Document 1",
        }
    ],
    "numbers_and_dates": [
        {"value": "12%", "context": "Retention decline", "source_name": "Document 1"}
    ],
    "constraints": ["Use only verified figures from the report"],
    "conflicts": [],
    "ambiguity": [],
    "missing_information": ["Baseline cohort size"],
    "anchor_terms": ["Q3 2026", "OrbitDB"],
}


@pytest.fixture(autouse=True)
def _silence_error_tracebacks(monkeypatch):
    """Keep test output pristine: handle_llm_client_exceptions prints tracebacks."""
    monkeypatch.setattr("traceback.print_exc", lambda: None)


@pytest.fixture
def owner_context():
    """Pin the temp-file owner for the duration of a test."""
    owner_id = uuid.uuid4()
    token = set_current_owner_id(owner_id)
    yield owner_id
    reset_current_owner_id(token)


@pytest.fixture
def fresh_temp_service(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP_DIRECTORY", str(tmp_path / "temp-root"))
    monkeypatch.setattr(
        "services.temp_file_service._temp_file_service_instance", None
    )
    from services.temp_file_service import get_temp_file_service

    return get_temp_file_service()


@pytest.fixture
def document_file(fresh_temp_service, owner_context):
    """One decomposed document text file inside the owner temp directory."""
    return fresh_temp_service.create_temp_file(
        "report.txt", "SECRET-CONTENT-7Q4 retention fell 12% in Q3 2026"
    )


def _patch_llm(monkeypatch, build_result=None, build_error=None):
    """Patch the provider layer with a recording fake."""
    calls = []

    async def fake_profile_documents(_client, _model, documents, **kwargs):
        calls.append({"documents": list(documents), "kwargs": kwargs})
        if build_error is not None:
            raise build_error
        return build_result or PROFILE_PAYLOAD

    monkeypatch.setattr(
        "services.document_profile_service.profile_documents", fake_profile_documents
    )
    monkeypatch.setattr(
        "services.document_profile_service.get_llm_provider",
        lambda: type("P", (), {"value": "openai"})(),
    )
    monkeypatch.setattr(
        "services.document_profile_service.get_model", lambda: "test-model"
    )
    monkeypatch.setattr(
        "services.document_profile_service.get_llm_client", lambda: object()
    )
    return calls


def _cache_paths_for(owner_dir, cache_key):
    return (
        owner_dir / CACHE_DIR_NAME / f"{cache_key}.json",
        owner_dir / CACHE_DIR_NAME / f"{cache_key}.tmp",
    )


def test_first_call_builds_and_caches_the_profile(
    monkeypatch, fresh_temp_service, document_file, owner_context, tmp_path
):
    calls = _patch_llm(monkeypatch)

    profile = asyncio.run(
        DocumentProfileService().get_or_build_profile([document_file])
    )

    assert isinstance(profile, DocumentProfile)
    assert profile.primary_topic == PROFILE_PAYLOAD["primary_topic"]
    assert len(calls) == 1
    assert calls[0]["documents"] == [
        ("Document 1", "SECRET-CONTENT-7Q4 retention fell 12% in Q3 2026")
    ]
    owner_dir = tmp_path / "temp-root" / str(owner_context)
    cached = list((owner_dir / CACHE_DIR_NAME).glob("*.json"))
    assert len(cached) == 1
    assert json.loads(cached[0].read_text(encoding="utf-8"))["primary_topic"] == (
        PROFILE_PAYLOAD["primary_topic"]
    )
    # No leftover staging file from the atomic write.
    assert not list((owner_dir / CACHE_DIR_NAME).glob("*.tmp"))


def test_profile_reads_decomposed_utf8_text(
    monkeypatch, fresh_temp_service, owner_context
):
    calls = _patch_llm(monkeypatch)
    source_text = "Doanh thu quý 3 tăng 12% nhờ nhóm khách hàng mới."
    document_path = fresh_temp_service.create_temp_file_path("bao-cao.txt")
    Path(document_path).write_text(source_text, encoding="utf-8")

    asyncio.run(DocumentProfileService().get_or_build_profile([document_path]))

    assert calls[0]["documents"] == [("Document 1", source_text)]


def test_second_call_with_same_content_and_language_hits_the_cache(
    monkeypatch, fresh_temp_service, document_file, owner_context
):
    calls = _patch_llm(monkeypatch)
    service = DocumentProfileService()

    first = asyncio.run(service.get_or_build_profile([document_file], language="Vietnamese (Tiếng Việt)"))
    second = asyncio.run(service.get_or_build_profile([document_file], language="Vietnamese (Tiếng Việt)"))

    assert first == second
    assert len(calls) == 1


def test_cache_key_normalizes_language(monkeypatch, fresh_temp_service, document_file):
    calls = _patch_llm(monkeypatch)
    service = DocumentProfileService()

    asyncio.run(service.get_or_build_profile([document_file], language="  VIETNAMESE  "))
    asyncio.run(service.get_or_build_profile([document_file], language="vietnamese"))

    assert len(calls) == 1


def test_different_language_rebuilds_the_profile(
    monkeypatch, fresh_temp_service, document_file
):
    calls = _patch_llm(monkeypatch)
    service = DocumentProfileService()

    asyncio.run(service.get_or_build_profile([document_file], language="Vietnamese"))
    asyncio.run(service.get_or_build_profile([document_file], language="English"))

    assert len(calls) == 2


def test_changed_content_rebuilds_the_profile(
    monkeypatch, fresh_temp_service, owner_context
):
    calls = _patch_llm(monkeypatch)
    service = DocumentProfileService()
    first_file = fresh_temp_service.create_temp_file("one.txt", "first content")
    second_file = fresh_temp_service.create_temp_file("two.txt", "second content")

    asyncio.run(service.get_or_build_profile([first_file]))
    asyncio.run(service.get_or_build_profile([second_file]))

    assert len(calls) == 2


def test_corrupt_cache_entry_is_rebuilt_not_surfaced(
    monkeypatch, fresh_temp_service, document_file, owner_context, tmp_path
):
    calls = _patch_llm(monkeypatch)
    service = DocumentProfileService()
    asyncio.run(service.get_or_build_profile([document_file]))

    owner_dir = tmp_path / "temp-root" / str(owner_context)
    cache_file = next((owner_dir / CACHE_DIR_NAME).glob("*.json"))
    cache_file.write_text("{ not valid json", encoding="utf-8")

    profile = asyncio.run(service.get_or_build_profile([document_file]))

    assert isinstance(profile, DocumentProfile)
    assert len(calls) == 2


def test_cache_version_change_invalidates_previous_entries(
    monkeypatch, fresh_temp_service, document_file
):
    calls = _patch_llm(monkeypatch)
    service = DocumentProfileService()
    asyncio.run(service.get_or_build_profile([document_file]))

    from services import document_profile_service as service_module

    monkeypatch.setattr(
        service_module, "DOCUMENT_PROFILE_VERSION", DOCUMENT_PROFILE_VERSION + 1
    )
    asyncio.run(service.get_or_build_profile([document_file]))

    assert len(calls) == 2


def test_concurrent_callers_build_once_and_share_the_result(
    monkeypatch, fresh_temp_service, document_file
):
    calls = _patch_llm(monkeypatch)
    # Slow the fake build down so the second caller truly waits on the lock.
    build_started = asyncio.Event()
    release_build = asyncio.Event()

    async def fake_profile_documents(_client, _model, documents, **kwargs):
        calls.append({"documents": list(documents), "kwargs": kwargs})
        build_started.set()
        await release_build.wait()
        return PROFILE_PAYLOAD

    monkeypatch.setattr(
        "services.document_profile_service.profile_documents", fake_profile_documents
    )
    service = DocumentProfileService()

    async def run_both():
        first = asyncio.create_task(service.get_or_build_profile([document_file]))
        await build_started.wait()
        second = asyncio.create_task(service.get_or_build_profile([document_file]))
        await asyncio.sleep(0)
        release_build.set()
        return await asyncio.gather(first, second)

    first, second = asyncio.run(run_both())

    assert first == second
    assert len(calls) == 1


def test_display_names_are_used_as_document_labels(
    monkeypatch, fresh_temp_service, document_file
):
    calls = _patch_llm(monkeypatch)

    asyncio.run(
        DocumentProfileService().get_or_build_profile(
            [document_file], display_names=["Q3 report.pdf"]
        )
    )

    assert calls[0]["documents"] == [
        ("Q3 report.pdf", "SECRET-CONTENT-7Q4 retention fell 12% in Q3 2026")
    ]


def test_missing_file_raises_the_coded_not_found_error(monkeypatch, fresh_temp_service):
    _patch_llm(monkeypatch)
    missing = str(Path(fresh_temp_service.base_dir) / "no-such-file.txt")

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(DocumentProfileService().get_or_build_profile([missing]))

    assert excinfo.value.status_code == 404
    assert excinfo.value.error_code == DOCUMENT_NOT_FOUND_ERROR_CODE


def test_other_owners_file_raises_the_coded_invalid_error(
    monkeypatch, fresh_temp_service, owner_context
):
    _patch_llm(monkeypatch)
    other_owner = uuid.uuid4()
    token = set_current_owner_id(other_owner)
    try:
        other_file = fresh_temp_service.create_temp_file("secret.txt", "x")
    finally:
        reset_current_owner_id(token)

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(DocumentProfileService().get_or_build_profile([other_file]))

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == DOCUMENT_INVALID_ERROR_CODE


def test_empty_file_paths_raise_a_coded_error(monkeypatch, fresh_temp_service):
    _patch_llm(monkeypatch)

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(DocumentProfileService().get_or_build_profile([]))

    assert excinfo.value.status_code == 400
    assert excinfo.value.error_code == DOCUMENTS_REQUIRED_ERROR_CODE


def test_invalid_llm_output_raises_the_coded_build_failure(
    monkeypatch, fresh_temp_service, document_file
):
    _patch_llm(monkeypatch, build_result={"primary_topic": "missing required fields"})

    with pytest.raises(CodedHTTPException) as excinfo:
        asyncio.run(DocumentProfileService().get_or_build_profile([document_file]))

    assert excinfo.value.status_code == 502
    assert excinfo.value.error_code == PROFILE_BUILD_FAILED_ERROR_CODE


def test_no_cache_is_written_when_the_build_fails(
    monkeypatch, fresh_temp_service, document_file, owner_context, tmp_path
):
    _patch_llm(
        monkeypatch,
        build_error=HTTPException(status_code=429, detail="LLM rate limited"),
    )

    with pytest.raises(HTTPException):
        asyncio.run(DocumentProfileService().get_or_build_profile([document_file]))

    owner_dir = tmp_path / "temp-root" / str(owner_context)
    assert not list((owner_dir / CACHE_DIR_NAME).glob("*"))


def test_logs_metadata_but_never_document_text(
    monkeypatch, fresh_temp_service, document_file, caplog
):
    _patch_llm(monkeypatch)

    with caplog.at_level(
        logging.INFO, logger="services.document_profile_service"
    ):
        asyncio.run(DocumentProfileService().get_or_build_profile([document_file]))
        asyncio.run(DocumentProfileService().get_or_build_profile([document_file]))

    assert "SECRET-CONTENT-7Q4" not in caplog.text
    assert "cache_hit" in caplog.text
    assert "built file_count=1" in caplog.text
    assert "total_characters=" in caplog.text
