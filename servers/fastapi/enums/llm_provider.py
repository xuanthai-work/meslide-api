from enum import Enum


class LLMProvider(Enum):
    OPENAI = "openai"
    GOOGLE = "google"
    CUSTOM = "custom"