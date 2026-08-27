from utils.latex_text import (
    parse_latex_tags,
    replace_text_runs,
    text_runs_to_tagged_text,
)
from utils.llm_calls.generate_slide_content import get_system_prompt


def test_parse_latex_tags_splits_mixed_content():
    assert parse_latex_tags(r"Area is <latex>\pi r^2</latex> square units.") == [
        {"text": "Area is "},
        {"type": "latex", "latex": r"\pi r^2"},
        {"text": " square units."},
    ]


def test_replace_text_runs_preserves_matching_styles():
    runs = replace_text_runs(
        [
            {"text": "Old", "font": {"bold": True}},
            {
                "type": "latex",
                "latex": "x",
                "display_mode": True,
                "font": {"color": "#123456"},
            },
        ],
        r"Result: <latex>\frac{x}{n}</latex>",
    )

    assert runs == [
        {"text": "Result: ", "font": {"bold": True}},
        {
            "type": "latex",
            "latex": r"\frac{x}{n}",
            "display_mode": True,
            "font": {"color": "#123456"},
        },
    ]


def test_text_runs_to_tagged_text_round_trips():
    runs = [
        {"text": "Result: "},
        {"type": "latex", "latex": r"x^2"},
        {"text": "."},
    ]

    tagged = text_runs_to_tagged_text(runs)

    assert tagged == r"Result: <latex>x^2</latex>."
    assert parse_latex_tags(tagged) == runs


def test_malformed_latex_tag_is_kept_as_plain_text():
    value = r"Result: <latex>\frac{x}{n}"

    assert parse_latex_tags(value) is None
    assert replace_text_runs(None, value) == [{"text": value}]


def test_slide_content_prompt_requests_latex_tags_in_string_fields():
    prompt = get_system_prompt()

    assert "Wrap every LaTeX expression in `<latex>`" in prompt
    assert r"The area is <latex>\pi r^2</latex>." in prompt
    assert "text lists and table cells" in prompt
