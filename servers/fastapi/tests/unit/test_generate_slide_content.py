import asyncio

import pytest

from models.presentation_layout import SlideLayoutModel
from models.presentation_outline_model import SlideOutlineModel
from utils.llm_calls import generate_slide_content


def _outline() -> SlideOutlineModel:
    return SlideOutlineModel(content="Slide outline")


def test_slide_content_prompt_keeps_visual_commands_out_of_output_fields():
    prompt = generate_slide_content.get_system_prompt(
        instructions="Create a bar chart on slide 5"
    )

    assert "visual instructions as production" in prompt
    assert "never emit those instructions" in prompt
    assert "populate the requested labels, series, and values" in prompt


def test_slide_content_prompt_requires_complete_text_below_character_ceiling():
    prompt = generate_slide_content.get_system_prompt()

    assert "hard ceiling, not a writing target" in prompt
    assert "aim for about 80%" in prompt
    assert "partial word" in prompt


def test_slide_content_user_prompt_includes_one_indexed_slide_number():
    prompt = generate_slide_content.get_user_prompt(
        "Slide outline",
        language="English",
        slide_number=1,
    )

    assert "# Slide Number:\n1" in prompt


def test_slide_content_generation_skips_schema_without_content_fields(monkeypatch):
    monkeypatch.setattr(
        generate_slide_content,
        "get_client",
        lambda **_kwargs: pytest.fail("LLM client should not be created"),
    )

    result = asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            SlideLayoutModel(
                id="decorative",
                json_schema={"title": "Decorative only"},
            ),
            _outline(),
            language="English",
        )
    )

    assert result == {}


def test_slide_content_generation_skips_asset_only_schema(monkeypatch):
    monkeypatch.setattr(
        generate_slide_content,
        "get_client",
        lambda **_kwargs: pytest.fail("LLM client should not be created"),
    )

    result = asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            SlideLayoutModel(
                id="asset-only",
                json_schema={
                    "type": "object",
                    "properties": {
                        "__image_url__": {"type": "string"},
                        "__icon_url__": {"type": "string"},
                    },
                    "required": ["__image_url__", "__icon_url__"],
                },
            ),
            _outline(),
            language="English",
        )
    )

    assert result == {}


def test_slide_content_generation_normalizes_object_schema_and_calls_llm(
    monkeypatch,
):
    captured = {}

    async def fake_generate_structured_with_schema_retries(
        _client,
        _model,
        *,
        response_format,
        json_schema,
        **_kwargs,
    ):
        captured["response_format"] = response_format
        captured["json_schema"] = json_schema
        captured["messages"] = _kwargs["messages"]
        return {
            "title": "Generated title",
            "__speaker_note__": "Speaker note",
        }

    monkeypatch.setattr(generate_slide_content, "get_llm_client", lambda **_kwargs: object())
    monkeypatch.setattr(generate_slide_content, "get_model", lambda: "test-model")
    monkeypatch.setattr(
        generate_slide_content,
        "generate_structured_with_schema_retries",
        fake_generate_structured_with_schema_retries,
    )

    result = asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            SlideLayoutModel(
                id="content",
                json_schema={
                    "title": "Content",
                    "properties": {
                        "title": {"type": "string"},
                    },
                    "required": ["title"],
                },
            ),
            _outline(),
            language="English",
            slide_number=2,
        )
    )

    assert result["title"] == "Generated title"
    assert captured["json_schema"]["type"] == "object"
    assert "__speaker_note__" in captured["json_schema"]["properties"]
    assert captured["response_format"].json_schema == captured["json_schema"]
    assert "# Slide Number:\n2" in captured["messages"][1].content


def test_content_fit_issues_find_saturated_nested_text_but_ignore_notes():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 5, "maxLength": 10},
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "body": {"type": "string", "maxLength": 20},
                    },
                },
            },
            "__speaker_note__": {"type": "string", "maxLength": 10},
            "fixed_code": {"type": "string", "minLength": 4, "maxLength": 4},
        },
    }

    issues = generate_slide_content._content_fit_issues(
        schema,
        {
            "title": "0123456789",
            "cards": [{"body": "short"}, {"body": "x" * 20}],
            "__speaker_note__": "0123456789",
            "fixed_code": "ABCD",
        },
    )

    assert issues == [
        "$.title: 10/10 characters; rewrite as a complete phrase of about 8 characters or fewer",
        "$.cards[1].body: 20/20 characters; rewrite as a complete phrase of about 16 characters or fewer",
    ]


def test_content_fit_issues_flag_vietnamese_under_max_length():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 24},
            "body": {"type": "string", "minLength": 4, "maxLength": 80},
            "__speaker_note__": {"type": "string", "maxLength": 10},
        },
    }
    issues = generate_slide_content._content_fit_issues(
        schema,
        {
            "title": "Phủ định của phủ định",
            "body": "Short body copy.",
            "__speaker_note__": "0123456789",
        },
    )
    assert any("$.title" in issue and "visually" in issue for issue in issues)
    assert all("$.body" not in issue for issue in issues)
    assert all("__speaker_note__" not in issue for issue in issues)


def test_content_fit_issues_ignore_short_english():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 40},
        },
    }
    issues = generate_slide_content._content_fit_issues(
        schema,
        {"title": "Hello world"},
    )
    assert issues == []


def test_slide_content_generation_repairs_strings_at_max_length(monkeypatch):
    calls = []
    responses = iter(
        [
            {
                "title": "DeepLearning.AI & Giáo Dụ",
                "__speaker_note__": "A sufficiently useful speaker note.",
            },
            {
                "title": "Giáo dục AI chuyên sâu",
                "__speaker_note__": "A sufficiently useful speaker note.",
            },
        ]
    )

    async def fake_generate_structured_with_schema_retries(
        _client,
        _model,
        *,
        messages,
        **_kwargs,
    ):
        calls.append(messages)
        return next(responses)

    monkeypatch.setattr(generate_slide_content, "get_llm_client", lambda: object())
    monkeypatch.setattr(generate_slide_content, "get_model", lambda: "test-model")
    monkeypatch.setattr(
        generate_slide_content,
        "generate_structured_with_schema_retries",
        fake_generate_structured_with_schema_retries,
    )

    result = asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            SlideLayoutModel(
                id="title-layout",
                json_schema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "minLength": 5,
                            "maxLength": 25,
                        }
                    },
                    "required": ["title"],
                },
            ),
            _outline(),
            language="Vietnamese",
        )
    )

    assert result["title"] == "Giáo dục AI chuyên sâu"
    assert len(calls) == 2
    assert "$.title: 25/25 characters" in calls[1][-1].content
    assert "return JSON only" in calls[1][-1].content


def test_restore_complete_outline_title_when_generated_text_is_a_cut_prefix():
    title = "Rèn Luyện Bản Thân"
    fragment = generate_slide_content._restore_cut_prefix(title[:16], [title])

    assert fragment == title
    assert "Bản Thân" in fragment


def test_restore_outline_title_when_cut_falls_on_a_word_boundary():
    method_title = "Ý nghĩa phương pháp"
    practice_title = "Vận dụng thực tiễn"

    assert (
        generate_slide_content._restore_cut_prefix(
            method_title.rsplit(" ", 1)[0],
            [method_title],
        )
        == method_title
    )
    assert (
        generate_slide_content._restore_cut_prefix(
            practice_title.rsplit(" ", 1)[0],
            [practice_title],
        )
        == practice_title
    )


def test_keep_complete_rewrite_that_is_not_an_outline_prefix():
    rewritten = generate_slide_content._restore_cut_prefix(
        "Thói quen mới",
        ["Rèn Luyện Bản Thân"],
    )

    assert rewritten == "Thói quen mới"


def test_slide_content_generation_restores_outline_title_when_repair_stays_cut(
    monkeypatch,
):
    full_title = "Rèn Luyện Bản Thân"
    saturated = full_title[:16]
    captured_schemas = []

    async def fake_generate_structured_with_schema_retries(
        _client,
        _model,
        *,
        messages,
        **kwargs,
    ):
        del messages
        captured_schemas.append(kwargs.get("json_schema"))
        return {
            "title": saturated,
            "__speaker_note__": "A sufficiently useful speaker note.",
        }

    monkeypatch.setattr(generate_slide_content, "get_llm_client", lambda: object())
    monkeypatch.setattr(generate_slide_content, "get_model", lambda: "test-model")
    monkeypatch.setattr(
        generate_slide_content,
        "generate_structured_with_schema_retries",
        fake_generate_structured_with_schema_retries,
    )

    result = asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            SlideLayoutModel(
                id="title-layout",
                json_schema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "minLength": 4,
                            "maxLength": 16,
                        }
                    },
                    "required": ["title"],
                },
            ),
            SlideOutlineModel(
                content=f"## {full_title}\nLặp lại hàng ngày để thành thói quen."
            ),
            language="Vietnamese",
        )
    )

    assert result["title"] == full_title[:16]
    assert len(result["title"]) <= 16
    assert len(captured_schemas) == 2
    assert captured_schemas[0]["properties"]["title"]["maxLength"] == 16
    assert captured_schemas[1]["properties"]["title"]["maxLength"] == 12


def test_schema_with_fit_ceilings_uses_tighter_visual_budget():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 24},
        },
    }
    content = {"title": "Phủ định của phủ định"}
    tightened = generate_slide_content._schema_with_fit_ceilings(schema, content)
    visual_max = tightened["properties"]["title"]["maxLength"]
    assert visual_max < 19  # 80% of 24 is 19; visual target must be tighter
    assert visual_max >= 4


def test_slide_content_generation_repairs_visual_overflow(monkeypatch):
    calls = []

    async def fake_generate_structured_with_schema_retries(
        _client,
        _model,
        *,
        json_schema,
        **_kwargs,
    ):
        calls.append(json_schema)
        if len(calls) == 1:
            return {
                "title": "Phủ định của phủ định",
                "__speaker_note__": "A sufficiently useful speaker note.",
            }
        return {
            "title": "Phủ định biện chứng",
            "__speaker_note__": "A sufficiently useful speaker note.",
        }

    monkeypatch.setattr(generate_slide_content, "get_llm_client", lambda: object())
    monkeypatch.setattr(generate_slide_content, "get_model", lambda: "test-model")
    monkeypatch.setattr(
        generate_slide_content,
        "generate_structured_with_schema_retries",
        fake_generate_structured_with_schema_retries,
    )

    result = asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            SlideLayoutModel(
                id="title-layout",
                json_schema={
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "minLength": 4,
                            "maxLength": 24,
                        }
                    },
                    "required": ["title"],
                },
            ),
            _outline(),
            language="Vietnamese",
        )
    )

    assert len(calls) == 2
    assert calls[0]["properties"]["title"]["maxLength"] == 24
    assert calls[1]["properties"]["title"]["maxLength"] < 19
    assert result["title"] == "Phủ định biện chứng"


def test_slide_content_prompt_mentions_latin_width_ceiling():
    prompt = generate_slide_content.get_system_prompt()
    assert "Latin-width" in prompt or "latin-width" in prompt.lower()


def test_schema_with_fit_ceilings_tightens_array_items_once():
    schema = {
        "type": "array",
        "items": {
            "type": "string",
            "minLength": 4,
            "maxLength": 24,
        },
    }
    content = [
        "Phủ định của phủ định",
        "Biện chứng của phủ định",
    ]

    tightened = generate_slide_content._schema_with_fit_ceilings(schema, content)

    assert tightened["items"]["maxLength"] == 16


def test_schema_with_fit_ceilings_does_not_visually_squeeze_short_sibling():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 24},
            "body": {"type": "string", "minLength": 4, "maxLength": 80},
        },
    }
    content = {
        "title": "Phủ định của phủ định",
        "body": "Résumé café",
    }

    tightened = generate_slide_content._schema_with_fit_ceilings(schema, content)

    assert tightened["properties"]["title"]["maxLength"] < 19
    assert tightened["properties"]["body"]["maxLength"] == 64
