from typing import List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from constants.documents import MAX_PROMPT_REWRITE_FILE_PATHS
from constants.presentation import MAX_NUMBER_OF_SLIDES


class PromptRewriteRequest(BaseModel):
    prompt: str = Field(
        ...,
        min_length=3,
        max_length=8000,
        description="The raw user prompt to rewrite.",
    )
    language: Optional[str] = Field(
        default=None,
        description="Target language for the rewritten prompt. If omitted, the prompt keeps its original language.",
    )
    n_slides: Optional[int] = Field(
        default=None,
        ge=1,
        le=MAX_NUMBER_OF_SLIDES,
        description="Target slide count for the rewritten prompt. If omitted, no slide-count constraint is applied.",
    )
    file_paths: Optional[List[str]] = Field(
        default=None,
        max_length=MAX_PROMPT_REWRITE_FILE_PATHS,
        description=(
            "Decomposed document text paths supplying rewrite context. "
            "Each path is resolved within the current owner's temp directory; "
            "an empty list means no document context."
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("prompt", mode="before")
    @classmethod
    def strip_outer_whitespace(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("file_paths", mode="before")
    @classmethod
    def normalize_empty_file_paths(cls, value):
        if isinstance(value, list) and not value:
            return None
        return value


class PromptRewriteResponse(BaseModel):
    rewritten_prompt: str = Field(
        ...,
        min_length=1,
        max_length=3000,
        description="The rewritten, presentation-ready prompt.",
    )

    model_config = ConfigDict(extra="forbid")
