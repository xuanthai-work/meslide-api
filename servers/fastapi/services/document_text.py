import json
import re
from typing import Optional


def _unwrap_liteparse_json_line_if_stored(text: str) -> str:
    """If the whole JSON line from the LiteParse runner was stored as the document, keep only the text field."""
    if not text:
        return text
    s = text.lstrip()
    if not s.startswith("{"):
        return text
    try:
        payload = json.loads(s)
    except (json.JSONDecodeError, TypeError, ValueError):
        return text
    if not isinstance(payload, dict):
        return text
    if (
        payload.get("ok") is True
        and "filePath" in payload
        and isinstance(payload.get("text"), str)
    ):
        return payload["text"]
    return text


_RE_TEXT_KEY = re.compile(r'"text"\s*:\s*"')


def _json_unescape_quoted_value(s: str, content_start: int) -> str:
    """
    Unescape a JSON string value. `content_start` is the index of the first character
    *inside* the value (immediately after the opening quote of the "text" field).
    If the closing quote is missing (truncated), returns the unescaped rest of the string.
    """
    out: list[str] = []
    i = content_start
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            e = s[i + 1]
            if e in '"\\':
                out.append(e)
                i += 2
            elif e == "/":
                out.append("/")
                i += 2
            elif e == "b":
                out.append("\b")
                i += 2
            elif e == "f":
                out.append("\f")
                i += 2
            elif e == "n":
                out.append("\n")
                i += 2
            elif e == "r":
                out.append("\r")
                i += 2
            elif e == "t":
                out.append("\t")
                i += 2
            elif e == "u" and i + 5 < n:
                try:
                    out.append(chr(int(s[i + 2 : i + 6], 16)))
                except (ValueError, OverflowError):
                    out.append(s[i : i + 6])
                i += 6
            else:
                out.append(e)
                i += 2
        elif c == '"':
            return "".join(out)
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _try_extract_liteparse_text_value_from_malformed_json(s: str) -> Optional[str]:
    """
    When json.loads failed (e.g. truncated or corrupt), find the "text" field value
    in a LiteParse-shaped object and return only the unescaped string body.
    """
    if not s.startswith("{"):
        return None
    head = s[:10000] if len(s) > 10000 else s
    if not ("ok" in head and "filePath" in head):
        return None
    m = _RE_TEXT_KEY.search(s)
    if not m:
        return None
    return _json_unescape_quoted_value(s, m.end())


def _clean_extracted_one_pass(t: str) -> str:
    for _ in range(3):
        nxt = _unwrap_liteparse_json_line_if_stored(t)
        if nxt == t:
            break
        t = nxt
    s = t.lstrip()
    if s.startswith("{"):
        m = _try_extract_liteparse_text_value_from_malformed_json(s)
        if m is not None:
            return m
    return t


def clean_extracted_document_text(text: str) -> str:
    """
    Return only the document body: strip LiteParse JSON wrappers, then drop any
    leading payload before the "text" value (handles truncated/invalid JSON).
    Multiple passes in case the inner body is again JSON-shaped.
    """
    if not text:
        return text
    t = text
    for _ in range(4):
        nxt = _clean_extracted_one_pass(t)
        if nxt == t:
            return t
        t = nxt
    return t
