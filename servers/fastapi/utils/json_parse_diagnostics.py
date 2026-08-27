import re

_CHAR_POS_RE = re.compile(r"\(char (\d+)\)")
_LINE_COL_RE = re.compile(r"line (\d+) column (\d+)")


def describe_json_parse_failure(
    text: str,
    error: BaseException,
    *,
    context_chars: int = 80,
    tail_lines: int = 20,
) -> str:
    """Return a compact log message describing where JSON parsing failed."""
    payload = text or ""
    char_count = len(payload)
    lines = payload.splitlines()
    line_count = len(lines) if payload else 0
    position = _error_char_index(payload, error)

    parts = [
        str(error).strip() or error.__class__.__name__,
        f"chars={char_count}",
        f"lines={line_count}",
    ]
    if position is not None:
        line_no, column_no = _line_and_column(payload, position)
        parts.extend(
            [
                f"pos={position}",
                f"line={line_no}",
                f"column={column_no}",
                f"around_pos={_snippet(payload, position, context_chars)}",
            ]
        )
    else:
        line_no, column_no = _line_column_from_message(str(error))
        if line_no is not None:
            parts.append(f"line={line_no}")
        if column_no is not None:
            parts.append(f"column={column_no}")

    report = " ".join(parts)
    if tail_lines > 0 and lines:
        tail = lines[-min(tail_lines, len(lines)) :]
        report += "\nlast_lines:\n" + "\n".join(tail)
    return report


def _error_char_index(text: str, error: BaseException) -> int | None:
    pos = getattr(error, "pos", None)
    if isinstance(pos, int) and 0 <= pos <= len(text):
        return pos

    match = _CHAR_POS_RE.search(str(error))
    if match:
        return min(int(match.group(1)), len(text))
    return None


def _line_column_from_message(message: str) -> tuple[int | None, int | None]:
    match = _LINE_COL_RE.search(message)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


def _line_and_column(text: str, position: int) -> tuple[int, int]:
    prefix = text[: max(0, min(position, len(text)))]
    line_no = prefix.count("\n") + 1
    column_no = len(prefix) - prefix.rfind("\n")
    return line_no, column_no


def _snippet(text: str, position: int, radius: int) -> str:
    if radius <= 0 or not text:
        return "''"
    start = max(0, position - radius)
    end = min(len(text), position + radius)
    return repr(text[start:end])
