"""Unit tests for the bounded DocumentProfile model."""

import pytest
from pydantic import ValidationError

from models.document_profile import DocumentProfile

VALID_PROFILE = {
    "source_names": ["Document 1"],
    "primary_topic": "Q3 2026 retention decline",
    "stated_purpose": "Report the verified retention decline and recovery plan",
    "stated_audience": "Finance leaders",
    "sections": [{"title": "Decline", "summary": "Retention fell 12% year over year"}],
    "entities": ["OrbitDB", "Retention Team"],
    "claims": [
        {
            "text": "The document states retention fell 12% in Q3 2026",
            "source_name": "Document 1",
        }
    ],
    "numbers_and_dates": [
        {"value": "12%", "context": "Year-over-year retention decline", "source_name": "Document 1"}
    ],
    "constraints": ["Use only verified figures from the report"],
    "conflicts": ["Document 1 states a 12% decline; Document 2 states 9%"],
    "ambiguity": ["The report does not define which cohort the 12% covers"],
    "missing_information": ["Baseline cohort size"],
    "anchor_terms": ["Q3 2026", "OrbitDB"],
}


def test_valid_profile_parses():
    profile = DocumentProfile(**VALID_PROFILE)

    assert profile.source_names == ["Document 1"]
    assert profile.stated_audience == "Finance leaders"
    assert profile.claims[0].source_name == "Document 1"


def test_stated_audience_is_optional():
    payload = {**VALID_PROFILE, "stated_audience": None}

    assert DocumentProfile(**payload).stated_audience is None


def test_profile_rejects_unknown_fields():
    payload = {**VALID_PROFILE, "invented_field": "value"}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


def test_profile_requires_source_names():
    payload = {**VALID_PROFILE, "source_names": []}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


def test_profile_rejects_more_than_eight_sources():
    payload = {**VALID_PROFILE, "source_names": [f"Document {i}" for i in range(9)]}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


def test_claim_requires_source_name():
    payload = {
        **VALID_PROFILE,
        "claims": [{"text": "A stated claim"}],
    }

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


@pytest.mark.parametrize(
    "field", ("sections", "entities", "claims", "numbers_and_dates", "anchor_terms")
)
def test_profile_bounds_list_lengths(field):
    if field == "sections":
        items = [{"title": "S", "summary": "S"}] * 13
    elif field == "claims":
        items = [{"text": "C", "source_name": "Document 1"}] * 17
    elif field == "numbers_and_dates":
        items = [{"value": "1", "context": "C", "source_name": "Document 1"}] * 17
    elif field == "entities":
        items = ["Entity"] * 25
    else:
        items = ["Anchor"] * 13
    payload = {**VALID_PROFILE, field: items}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


def test_profile_rejects_oversized_strings():
    payload = {**VALID_PROFILE, "primary_topic": "a" * 201}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


def test_profile_rejects_blank_strings():
    payload = {**VALID_PROFILE, "primary_topic": "   "}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


@pytest.mark.parametrize(
    "field", ("source_names", "entities", "constraints", "anchor_terms")
)
def test_profile_rejects_blank_strings_inside_lists(field):
    payload = {**VALID_PROFILE, field: ["   "]}

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)


def test_profile_rejects_oversized_section_summary():
    payload = {
        **VALID_PROFILE,
        "sections": [{"title": "S", "summary": "s" * 401}],
    }

    with pytest.raises(ValidationError):
        DocumentProfile(**payload)