"""Deterministic image-search query normalization (FR-4).

`image_query` must be optimized for finding an EXISTING image on the web,
not for generating one. This module converts generator-style prompts into
searchable phrases — deterministically, with NO extra LLM call:

- generation-only modifiers ("cinematic", "highly detailed", "4k", ...) are
  noise for a search engine and are removed;
- imperative commands ("generate", "create", "Tạo hình ảnh ...") are removed;
- named entities, places, products, and dates are never touched (the
  blocklist only removes known boilerplate, so anything unlisted survives);
- whitespace is collapsed and length is bounded.
"""

import re

from services.image_search.constants import limits

# ---------------------------------------------------------------------------
# Boilerplate patterns. Order matters only within removal; all are applied.
# ---------------------------------------------------------------------------

# Genre+preposition openers: "a photo of", "an illustration of",
# "hình ảnh về", "ảnh của", ...
_GENRE_OPENERS = [
    r"\b(?:an?\s+|the\s+)?(?:photo(?:graph)?|image|picture|illustration|"
    r"drawing|painting|render(?:ing)?|depiction|graphic)s?\s+(?:of|about)\b",
    r"\bhình ảnh\s+(?:về|của)\b",
    r"\b(?:bức\s+)?ảnh\s+(?:về|của)\b",
    r"\b(?:bức\s+)?vẽ\s+(?:về|của)\b",
]

# Imperative / generation commands in English and Vietnamese.
_COMMANDS = [
    r"\b(?:generate|create|draw|render|paint|depict|illustrate|make|produce|"
    r"show)\b",
    r"\btạo\s+ra\b",
    r"\btạo\b",
    r"\bvẽ\b",
    r"\bminh\s+họa\b",
    r"\btrực\s+quan\s+hóa\b",
    # Bare "hình ảnh" (image[s]) is genre noise in Vietnamese prompts.
    r"\bhình\s+ảnh\b",
]

# Generation-only style/quality modifiers that poison search queries.
_GENERATION_MODIFIERS = [
    r"\b(?:cinematic|photorealistic|ultra[- ]realistic|hyper[- ]realistic|"
    r"realistic)\b",
    r"\b(?:4k|8k|uhd|hdr|hd)\b",
    r"\bhigh(?:ly)?[- ](?:resolution|quality|detailed)\b",
    r"\bhigh[- ]res\b",
    r"\b(?:detailed|intricate|ornate)\b",
    r"\b(?:masterpiece|award[- ]winning|professional|studio[- ]quality)\b",
    r"\b(?:stunning|beautiful|breathtaking|gorgeous|majestic|đẹp|tuyệt vời|"
    r"tuyệt đẹp|nghệ thuật)\b",
    r"\b(?:vibrant|vivid|rich)\s+(?:colors?|colours?|màu sắc)\b",
    r"\bsắc\s+nét\b",
    r"\b(?:chất lượng cao|độ phân giải cao|siêu thực|chi tiết cao|"
    r"cực kỳ chi tiết)\b",
    r"\b(?:dramatic|volumetric|soft|golden[- ]hour|studio)\s+lighting\b",
    r"\bánh sáng\s+\S+\b",
    r"\b(?:bokeh|depth of field|wide[- ]angle|close[- ]up|macro)\b",
    r"\b(?:conceptual|abstract)\b",
    r"\b(?:digital art|digital painting|oil painting|watercolor|vector art)\b",
    r"\bin\s+the\s+style\s+of\b",
    r"\b(?:artstation|pixiv|trending)\b",
]

_ALL_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for group in (_GENRE_OPENERS, _COMMANDS, _GENERATION_MODIFIERS)
    for pattern in group
]

_WHITESPACE = re.compile(r"\s+")
_TRAILING_PUNCTUATION = re.compile(r"[\s,;:.!–—-]+$")


def normalize_image_query(prompt: str) -> str:
    """Convert a generator-style prompt into a bounded search phrase.

    Empty in → empty out; the caller decides how to handle an unusable
    query (the resolver never invents one).
    """
    text = _WHITESPACE.sub(" ", str(prompt or "")).strip()
    if not text:
        return ""
    for pattern in _ALL_PATTERNS:
        text = pattern.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _TRAILING_PUNCTUATION.sub("", text).strip()
    return text[: limits.max_query_length].strip()


def image_prompt_to_image_query(image_prompt: str) -> str:
    """Compatibility fallback (FR-4): derive `image_query` from
    `image_prompt` deterministically during rollout, when old structured
    output has no `image_query` key. Must not trigger an extra LLM call —
    it is exactly `normalize_image_query`.
    """
    return normalize_image_query(image_prompt)