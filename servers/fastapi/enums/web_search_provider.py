from enum import Enum


class WebSearchProvider(Enum):
    AUTO = "auto"
    NATIVE = "native"
    SEARXNG = "searxng"
