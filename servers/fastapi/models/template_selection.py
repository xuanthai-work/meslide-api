"""Request and response contract for choosing a built-in presentation template."""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TemplateSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1, max_length=8000)
    candidate_ids: list[str] = Field(min_length=1)

    @field_validator("prompt", mode="before")
    @classmethod
    def trim_prompt(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("candidate_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        ids = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not ids:
            raise ValueError("At least one candidate ID is required")
        return ids


class TemplateSelectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = Field(min_length=1)
