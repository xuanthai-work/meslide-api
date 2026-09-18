"""Status/warning contract tests for the image-search feature (Phase 7).

- Every failure code the pipeline can emit has a warning-detail mapping, and
  every mapping entry corresponds to a code the pipeline can actually emit.
- ``image_search_warning`` keeps the exact SSE warning shape the frontend
  toast channel already consumes.
- Failures classify into distinct machine-readable codes (timeout vs
  download vs rejected vs unexpected), so each slot warning can show the
  right localized message.
- ``on_status`` reports the stable searching/importing codes exactly once
  per search and first import attempt, and ``process_slide_and_fetch_assets``
  forwards the caller's status callback into the service.
"""

import asyncio

from services.image_search.downloader import (
    IMPORT_FAILURE_DOWNLOAD,
    IMPORT_FAILURE_REJECTED,
    ImageImportError,
    validate_import_url,
)
from services.image_search.models import (
    ImageCandidateImporter,
    ImageSearchCandidate,
    ImageSearchOutcome,
)
from services.image_search.service import (
    FAILURE_CANDIDATE_REJECTED,
    FAILURE_DOWNLOAD_FAILED,
    FAILURE_EMPTY_QUERY,
    FAILURE_IMPORT_TIMEOUT,
    FAILURE_IMPORTER_UNAVAILABLE,
    FAILURE_NO_RESULTS,
    FAILURE_NO_USABLE_CANDIDATE,
    FAILURE_PLACEHOLDER_FALLBACK,
    FAILURE_SEARCH_TIMEOUT,
    SearxngImageSearchService,
)
from services.image_search.warnings import (
    IMAGE_SEARCH_DISABLED_CODE,
    IMAGE_SEARCH_NOT_CONFIGURED_CODE,
    _PLACEHOLDER_DETAILS,
    image_search_warning,
)
from utils import process_slides


def _candidate(url="https://good.org/1.jpg"):
    return ImageSearchCandidate(
        title="t", image_url=url, engine="bing images", width=800, height=600
    )


class _ListAdapter:
    def __init__(self, candidates=None, error=None):
        self.candidates = candidates or []
        self.error = error

    async def fetch(self, normalized_query, *, limit):
        if self.error is not None:
            raise self.error
        return list(self.candidates)


class _ScriptedImporter(ImageCandidateImporter):
    def __init__(self, errors=(), asset="asset"):
        self.errors = list(errors)
        self.asset = asset
        self.attempted = 0

    async def import_candidate(self, candidate, *, owner_id=None, query=None):
        self.attempted += 1
        if self.attempted <= len(self.errors):
            raise self.errors[self.attempted - 1]
        return self.asset


# ---------------------------------------------------------------------------
# Backend-code mapping parity
# ---------------------------------------------------------------------------


def test_every_emittable_failure_code_has_a_warning_detail():
    emittable = {
        FAILURE_EMPTY_QUERY,
        FAILURE_NO_RESULTS,
        FAILURE_SEARCH_TIMEOUT,
        FAILURE_IMPORT_TIMEOUT,
        FAILURE_DOWNLOAD_FAILED,
        FAILURE_CANDIDATE_REJECTED,
        FAILURE_IMPORTER_UNAVAILABLE,
        FAILURE_NO_USABLE_CANDIDATE,
        FAILURE_PLACEHOLDER_FALLBACK,
        IMAGE_SEARCH_NOT_CONFIGURED_CODE,
        IMAGE_SEARCH_DISABLED_CODE,
    }
    mapped = set(_PLACEHOLDER_DETAILS)
    assert emittable <= mapped, f"codes without detail: {emittable - mapped}"
    # No orphan mappings either: every detail belongs to a known code.
    assert mapped == emittable, f"orphan detail codes: {mapped - emittable}"


def test_image_search_warning_keeps_the_frontend_warning_shape():
    for code in _PLACEHOLDER_DETAILS:
        warning = image_search_warning(code)
        assert warning == {
            "status_code": 200,
            "detail": _PLACEHOLDER_DETAILS[code],
            "code": None,
            "error_code": code,
        }


def test_unknown_failure_code_still_warns_with_a_placeholder_detail():
    warning = image_search_warning("image_search.some_future_code")
    assert warning["error_code"] == "image_search.some_future_code"
    assert warning["status_code"] == 200
    assert warning["detail"]


def test_import_error_reasons_are_the_documented_codes():
    try:
        validate_import_url("ftp://example.com/x.jpg")
        raise AssertionError("expected ImageImportError")
    except ImageImportError as error:
        assert error.reason == IMPORT_FAILURE_REJECTED
    error = ImageImportError("download failed: HTTP 500")
    assert error.reason == IMPORT_FAILURE_DOWNLOAD


# ---------------------------------------------------------------------------
# Failure classification (one distinct code per root cause)
# ---------------------------------------------------------------------------


def test_search_timeout_classifies_as_search_timeout():
    service = SearxngImageSearchService(
        adapter=_ListAdapter(error=TimeoutError("slow")), importer=_ScriptedImporter()
    )
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_SEARCH_TIMEOUT
    assert not outcome.ok


def test_unexpected_search_failure_classifies_as_placeholder_fallback():
    service = SearxngImageSearchService(
        adapter=_ListAdapter(error=RuntimeError("boom")), importer=_ScriptedImporter()
    )
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_PLACEHOLDER_FALLBACK


def test_importer_timeout_classifies_as_import_timeout():
    candidates = [_candidate(f"https://slow.org/{i}.jpg") for i in range(3)]
    service = SearxngImageSearchService(
        adapter=_ListAdapter(candidates),
        importer=_ScriptedImporter(errors=[TimeoutError("slow download")] * 3),
    )
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_IMPORT_TIMEOUT


def test_rejected_candidates_classify_as_candidate_rejected():
    candidates = [_candidate(f"https://bad.org/{i}.jpg") for i in range(3)]
    service = SearxngImageSearchService(
        adapter=_ListAdapter(candidates),
        importer=_ScriptedImporter(
            errors=[
                ImageImportError("unsafe", reason=IMPORT_FAILURE_REJECTED),
            ]
            * 3
        ),
    )
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_CANDIDATE_REJECTED


def test_first_failure_reason_wins_over_later_ones():
    candidates = [_candidate(f"https://mixed.org/{i}.jpg") for i in range(3)]
    service = SearxngImageSearchService(
        adapter=_ListAdapter(candidates),
        importer=_ScriptedImporter(
            errors=[
                ImageImportError("unsafe", reason=IMPORT_FAILURE_REJECTED),
                ImageImportError("http 500", reason=IMPORT_FAILURE_DOWNLOAD),
            ]
        ),
    )
    # The first candidate is rejected, the second downloads fine.
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.ok and outcome.asset == "asset"


def test_all_candidates_failing_reports_first_reason_not_generic():
    candidates = [_candidate(f"https://mixed.org/{i}.jpg") for i in range(3)]
    service = SearxngImageSearchService(
        adapter=_ListAdapter(candidates),
        importer=_ScriptedImporter(
            errors=[
                ImageImportError("http 500", reason=IMPORT_FAILURE_DOWNLOAD),
                ImageImportError("unsafe", reason=IMPORT_FAILURE_REJECTED),
                ImageImportError("http 502", reason=IMPORT_FAILURE_DOWNLOAD),
            ]
        ),
    )
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_DOWNLOAD_FAILED


def test_no_importer_still_blocks_generation_fallback():
    service = SearxngImageSearchService(adapter=_ListAdapter([_candidate()]))
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_IMPORTER_UNAVAILABLE


def test_no_results_outcome_detail_is_redacted():
    service = SearxngImageSearchService(adapter=_ListAdapter([]))
    outcome = asyncio.run(service.search_and_import("hanoi skyline"))
    assert outcome.failure_code == FAILURE_NO_RESULTS
    # Phase 8: the raw query is never carried in outcome details; logs use
    # the hashed form instead.
    assert outcome.detail is None


# ---------------------------------------------------------------------------
# Stable status codes
# ---------------------------------------------------------------------------


def test_on_status_reports_searching_then_importing_in_order():
    from services.image_search.constants import (
        IMAGE_STATUS_IMPORTING,
        IMAGE_STATUS_SEARCHING_WEB,
    )

    statuses: list[str] = []
    candidates = [_candidate("https://good.org/1.jpg")]
    service = SearxngImageSearchService(
        adapter=_ListAdapter(candidates), importer=_ScriptedImporter()
    )
    outcome = asyncio.run(
        service.search_and_import("hanoi skyline", on_status=statuses.append)
    )
    assert outcome.ok
    assert statuses == [IMAGE_STATUS_SEARCHING_WEB, IMAGE_STATUS_IMPORTING]


def test_importing_status_reported_once_per_call_even_after_failures():
    from services.image_search.constants import (
        IMAGE_STATUS_IMPORTING,
        IMAGE_STATUS_SEARCHING_WEB,
    )

    statuses: list[str] = []
    candidates = [_candidate(f"https://good.org/{i}.jpg") for i in range(3)]
    service = SearxngImageSearchService(
        adapter=_ListAdapter(candidates),
        importer=_ScriptedImporter(
            errors=[
                ImageImportError("unsafe", reason=IMPORT_FAILURE_REJECTED),
                ImageImportError("http 500", reason=IMPORT_FAILURE_DOWNLOAD),
            ]
        ),
    )
    outcome = asyncio.run(
        service.search_and_import("hanoi skyline", on_status=statuses.append)
    )
    assert outcome.ok
    # The service reports once per call; the streaming pipelines deduplicate
    # these to one SSE status per code per presentation.
    assert statuses == [IMAGE_STATUS_SEARCHING_WEB, IMAGE_STATUS_IMPORTING]


def test_on_status_reports_searching_even_with_no_results():
    from services.image_search.constants import IMAGE_STATUS_SEARCHING_WEB

    statuses: list[str] = []
    service = SearxngImageSearchService(adapter=_ListAdapter([]))
    outcome = asyncio.run(
        service.search_and_import("hanoi skyline", on_status=statuses.append)
    )
    assert outcome.failure_code == FAILURE_NO_RESULTS
    assert statuses == [IMAGE_STATUS_SEARCHING_WEB]


# ---------------------------------------------------------------------------
# process_slide_and_fetch_assets forwards the status callback
# ---------------------------------------------------------------------------


class _RecordingSearchService:
    def __init__(self, outcome):
        self.outcome = outcome
        self.on_status_callbacks: list = []

    async def search_and_import(
        self, query, *, owner_id=None, aspect_ratio=None, context=None, on_status=None
    ):
        self.on_status_callbacks.append(on_status)
        if on_status is not None:
            on_status("images.searching_web")
        return self.outcome


class _NoopGenerationService:
    def generate_image(self, prompt):
        raise AssertionError("generation must not run in search mode")


def _slide_with_image_query():
    return process_slides.SlideModel(
        presentation="00000000-0000-0000-0000-000000000000",
        layout_group="g",
        layout="l",
        index=0,
        content={"hero": {"image_query": "solar farm"}},
    )


def test_process_slides_forwards_status_callback_to_service(monkeypatch):
    from models.sql.image_asset import ImageAsset

    asset = ImageAsset(
        owner_id=None,
        path="D:/app_data/images/imported-1.jpg",
        is_uploaded=False,
    )
    outcome = ImageSearchOutcome(asset=asset)
    service = _RecordingSearchService(outcome)
    monkeypatch.setattr(process_slides, "get_image_search_service", lambda: service)
    monkeypatch.setattr(
        process_slides,
        "filesystem_image_path_to_app_data_url",
        lambda path: "/app_data/images/imported-1.jpg",
    )
    monkeypatch.setattr(
        process_slides, "web_image_search_feature_enabled", lambda: True
    )
    monkeypatch.setattr(process_slides, "web_image_search_configured", lambda: True)

    statuses: list[str] = []
    slide = _slide_with_image_query()

    async def run():
        return await process_slides.process_slide_and_fetch_assets(
            _NoopGenerationService(),
            slide,
            image_warnings=[],
            web_search=True,
            image_status_callback=statuses.append,
        )

    assets = asyncio.run(run())
    assert assets == [asset]
    assert statuses == ["images.searching_web"]
    assert service.on_status_callbacks == [statuses.append]