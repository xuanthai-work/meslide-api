"""Strict, bounded profile describing what uploaded documents state.

The profile is the compact document context handed to prompt rewrite. It
records claims AS STATED BY THE DOCUMENTS with source provenance — it never
relabels them as independently verified facts. Field and list bounds keep
the profile small enough to embed in a rewrite prompt and are part of the
versioned cache contract: changing them changes what a cached profile can
contain, so bump ``DOCUMENT_PROFILE_VERSION`` in the profile service when
they change materially.
"""

from typing import Annotated, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bounded string primitives shared by profile fields. Kept module-level so the
# limits are reviewable in one place and referenced from tests.
_SOURCE_NAME = Annotated[str, Field(min_length=1, max_length=120)]
_SHORT_TEXT = Annotated[str, Field(min_length=1, max_length=200)]
_MEDIUM_TEXT = Annotated[str, Field(min_length=1, max_length=300)]
_TERM = Annotated[str, Field(min_length=1, max_length=100)]


def _reject_blank_string(value):
    """Reject whitespace-only strings: ``min_length`` cannot catch them."""
    if isinstance(value, str) and not value.strip():
        raise ValueError("String values must not be blank")
    return value


class _BlankStringGuard(BaseModel):
    """Shared validation: no whitespace-only strings, anywhere in the model."""

    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_blank_strings(cls, value):
        return _reject_blank_string(value)


class DocumentSection(_BlankStringGuard):
    """One entry of the document's section/topic map."""

    title: _SHORT_TEXT
    summary: Annotated[str, Field(min_length=1, max_length=400)]


class DocumentClaim(_BlankStringGuard):
    """A claim the document states, with the source it came from."""

    text: _MEDIUM_TEXT
    source_name: _SOURCE_NAME


class DocumentNumber(_BlankStringGuard):
    """A number or date the document states, with provenance and context."""

    value: Annotated[str, Field(min_length=1, max_length=80)]
    context: _SHORT_TEXT
    source_name: _SOURCE_NAME


class DocumentProfile(_BlankStringGuard):
    """Compact, structured summary of what a set of documents states."""

    source_names: Annotated[List[_SOURCE_NAME], Field(min_length=1, max_length=8)]
    primary_topic: _SHORT_TEXT
    stated_purpose: _MEDIUM_TEXT
    stated_audience: Optional[_SHORT_TEXT] = None
    sections: Annotated[List[DocumentSection], Field(max_length=12)]
    entities: Annotated[List[_TERM], Field(max_length=24)]
    claims: Annotated[List[DocumentClaim], Field(max_length=16)]
    numbers_and_dates: Annotated[List[DocumentNumber], Field(max_length=16)]
    constraints: Annotated[List[_MEDIUM_TEXT], Field(max_length=10)]
    conflicts: Annotated[List[_MEDIUM_TEXT], Field(max_length=10)]
    ambiguity: Annotated[List[_MEDIUM_TEXT], Field(max_length=10)]
    missing_information: Annotated[List[_MEDIUM_TEXT], Field(max_length=10)]
    anchor_terms: Annotated[List[_TERM], Field(max_length=12)]

    @field_validator(
        "source_names",
        "entities",
        "constraints",
        "conflicts",
        "ambiguity",
        "missing_information",
        "anchor_terms",
        mode="before",
    )
    @classmethod
    def reject_blank_list_items(cls, value):
        if isinstance(value, list):
            for item in value:
                _reject_blank_string(item)
        return value