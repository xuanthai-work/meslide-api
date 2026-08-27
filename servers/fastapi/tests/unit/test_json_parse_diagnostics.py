import re

import dirtyjson

from utils.json_parse_diagnostics import describe_json_parse_failure


def _parse_error(text: str) -> Exception:
    try:
        dirtyjson.loads(text)
    except Exception as error:
        return error
    raise AssertionError("expected dirtyjson to fail")


def test_describe_json_parse_failure_shows_snippet_around_error_position():
    text = '{\n  "slides": [\n    {"content": "ok"}\n    {"content": "next"}\n  ]\n}'
    error = _parse_error(text)

    report = describe_json_parse_failure(text, error, context_chars=40, tail_lines=5)

    assert "chars=" in report
    assert "lines=" in report
    assert "ok" in report
    assert "next" in report
    assert "Expecting" in report or "delimiter" in report


def test_describe_json_parse_failure_includes_last_lines_for_newline_heavy_payload():
    slides = "\n".join(f"- bullet {index}" for index in range(30))
    text = '{\n  "slides": [\n    {"content": "## Intro\n' + slides + "\n"
    error = _parse_error(text)

    report = describe_json_parse_failure(text, error, context_chars=80, tail_lines=8)

    line_count = re.search(r"lines=(\d+)", report)
    assert line_count is not None
    assert int(line_count.group(1)) >= 20
    assert "last_lines:" in report
    assert "bullet 29" in report
    assert "chars=" in report
