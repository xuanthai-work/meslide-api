from pydantic import BaseModel, ConfigDict

from enums.tone import Tone
from enums.verbosity import Verbosity


class CreatePresentationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str
    n_slides: int | None = None
    language: str | None = None
    file_paths: list[str] | None = None
    tone: Tone = Tone.DEFAULT
    verbosity: Verbosity = Verbosity.STANDARD
    instructions: str | None = None
    include_table_of_contents: bool = True
    include_title_slide: bool = True
    web_search: bool = False
