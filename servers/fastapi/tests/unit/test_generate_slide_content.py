import asyncio
import unicodedata

import pytest

from models.presentation_layout import SlideLayoutModel
from models.presentation_outline_model import SlideOutlineModel
from utils.llm_calls import generate_slide_content

_NOTE = "A sufficiently useful speaker note for the slide presentation."
_COVER_OUTLINE = (
    "Vật chất là gì? Vận động, đứng im, không gian, thời gian là gì?"
)
_CUT_COVER_TITLE = "Vật chất là gì? Vận"


def _outline() -> SlideOutlineModel:
    return SlideOutlineModel(content="Slide outline")


def _title_layout(max_length: int, min_length: int = 4) -> SlideLayoutModel:
    return SlideLayoutModel(
        id="title-layout",
        json_schema={
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": min_length,
                    "maxLength": max_length,
                }
            },
            "required": ["title"],
        },
    )


def _patch_slide_llm(monkeypatch, responder):
    calls = []

    async def fake_generate_structured_with_schema_retries(
        _client,
        _model,
        *,
        messages,
        **kwargs,
    ):
        calls.append(
            {
                "messages": messages,
                "json_schema": kwargs.get("json_schema"),
            }
        )
        if callable(responder):
            return responder(len(calls), kwargs.get("json_schema"), messages)
        if isinstance(responder, list):
            index = min(len(calls) - 1, len(responder) - 1)
            return responder[index]
        return dict(responder)

    monkeypatch.setattr(generate_slide_content, "get_llm_client", lambda: object())
    monkeypatch.setattr(generate_slide_content, "get_model", lambda: "test-model")
    monkeypatch.setattr(
        generate_slide_content,
        "generate_structured_with_schema_retries",
        fake_generate_structured_with_schema_retries,
    )
    return calls


def _generate(layout, outline, language="Vietnamese"):
    return asyncio.run(
        generate_slide_content.get_slide_content_from_type_and_outline(
            layout,
            outline,
            language,
        )
    )


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
                "title": "Giáo dục AI",
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

    assert result["title"] == "Giáo dục AI"
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


def test_slide_content_generation_does_not_persist_sliced_outline_title(
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

    assert result["title"] != full_title[:16]
    assert result["title"] != full_title
    assert len(result["title"]) <= 16
    assert result["title"][-1].isalnum()
    assert full_title.startswith(result["title"])
    assert result["title"] in {"Rèn", "Rèn Luyện", "Rèn Luyện Bản"}
    assert len(captured_schemas) == 1 + generate_slide_content.MAX_FIT_REPAIR_ATTEMPTS
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
            "title": "Phủ định",
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
    assert result["title"] == "Phủ định"


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


def test_restore_cut_prefix_does_not_slice_source_that_exceeds_max_length():
    restored = generate_slide_content._restore_cut_prefix(
        _CUT_COVER_TITLE,
        [_COVER_OUTLINE],
        max_length=19,
    )

    assert restored != _COVER_OUTLINE
    assert restored == _CUT_COVER_TITLE
    assert len(restored) <= 19


def test_restore_cut_prefix_restores_source_when_it_fits_max_length():
    title = "Rèn Luyện Bản Thân"
    restored = generate_slide_content._restore_cut_prefix(
        title[:10],
        [title],
        max_length=40,
    )

    assert restored == title


def test_cover_title_fallback_is_complete_phrase_within_max_length(monkeypatch):
    assert len(_CUT_COVER_TITLE) == 19

    def responder(_call, _schema, _messages):
        return {"title": _CUT_COVER_TITLE, "__speaker_note__": _NOTE}

    _patch_slide_llm(monkeypatch, responder)
    result = _generate(
        _title_layout(19),
        SlideOutlineModel(content=f"## {_COVER_OUTLINE}\nTriết học Mác."),
    )

    assert len(result["title"]) <= 19
    assert result["title"] != _CUT_COVER_TITLE
    assert result["title"].endswith("?")
    assert "Vận" not in result["title"]
    assert result["title"] == "Vật chất là gì?"


def test_partial_word_title_is_not_persisted(monkeypatch):
    source = "Hoàn cảnh ra đời của chủ nghĩa duy vật"
    cut = "Hoàn cảnh ra đ"

    def responder(_call, _schema, _messages):
        return {"title": cut, "__speaker_note__": _NOTE}

    _patch_slide_llm(monkeypatch, responder)
    result = _generate(
        _title_layout(24),
        SlideOutlineModel(content=f"## {source}\nBối cảnh lịch sử."),
    )

    assert result["title"] != cut
    assert not result["title"].endswith("đ")
    assert len(result["title"]) <= 24
    assert result["title"] in {
        "Hoàn cảnh",
        "Hoàn cảnh ra",
        "Hoàn cảnh ra đời",
    }


def test_repair_response_at_tightened_max_length_is_retried(monkeypatch):
    original_max = 25
    repair_max = 20
    saturated_original = "A" * original_max
    saturated_repair = "B" * repair_max
    rewritten = "Compact AI title"

    responses = [
        {"title": saturated_original, "__speaker_note__": _NOTE},
        {"title": saturated_repair, "__speaker_note__": _NOTE},
        {"title": rewritten, "__speaker_note__": _NOTE},
    ]
    calls = _patch_slide_llm(monkeypatch, responses)
    result = _generate(
        _title_layout(original_max),
        SlideOutlineModel(content="## Deep learning education\nBody."),
        language="English",
    )

    assert result["title"] == rewritten
    assert len(calls) == 3
    assert calls[0]["json_schema"]["properties"]["title"]["maxLength"] == original_max
    assert calls[1]["json_schema"]["properties"]["title"]["maxLength"] == repair_max
    assert calls[2]["json_schema"]["properties"]["title"]["maxLength"] == repair_max


def test_repair_rewrite_that_fits_is_accepted_without_extra_retry(monkeypatch):
    calls = _patch_slide_llm(
        monkeypatch,
        [
            {"title": "A" * 25, "__speaker_note__": _NOTE},
            {"title": "Giáo dục AI", "__speaker_note__": _NOTE},
        ],
    )
    result = _generate(_title_layout(25), _outline())

    assert result["title"] == "Giáo dục AI"
    assert len(calls) == 2


def test_bounded_retry_then_safe_fallback_when_model_keeps_cutting(monkeypatch):
    cut = "Định nghĩa Lên"
    source = "Định nghĩa Lênin về giai cấp công nhân"

    def responder(_call, _schema, _messages):
        return {"title": cut, "__speaker_note__": _NOTE}

    calls = _patch_slide_llm(monkeypatch, responder)
    result = _generate(
        _title_layout(20),
        SlideOutlineModel(content=f"## {source}\nGiai cấp."),
    )

    assert len(calls) == 1 + generate_slide_content.MAX_FIT_REPAIR_ATTEMPTS
    assert result["title"] != cut
    assert len(result["title"]) <= 20
    assert not result["title"].endswith("Lên")
    assert " " not in result["title"] or result["title"].split()[-1] != "Lên"


def test_overflowing_outline_source_is_not_restored(monkeypatch):
    full_title = "Rèn Luyện Bản Thân Qua Việc Lặp Lại Hàng Ngày"
    cut = full_title[:16]

    def responder(_call, _schema, _messages):
        return {"title": cut, "__speaker_note__": _NOTE}

    _patch_slide_llm(monkeypatch, responder)
    result = _generate(
        _title_layout(16),
        SlideOutlineModel(content=f"## {full_title}\nThói quen."),
    )

    assert result["title"] != full_title
    assert result["title"] != cut
    assert len(result["title"]) <= 16
    assert len(full_title) > 16


def test_non_prefix_rewrite_is_kept_when_complete_and_within_limit(monkeypatch):
    calls = _patch_slide_llm(
        monkeypatch,
        [
            {"title": _CUT_COVER_TITLE, "__speaker_note__": _NOTE},
            {"title": "Duy vật", "__speaker_note__": _NOTE},
        ],
    )
    result = _generate(
        _title_layout(24),
        SlideOutlineModel(content=f"## {_COVER_OUTLINE}\nNội dung."),
    )

    assert result["title"] == "Duy vật"
    assert not _COVER_OUTLINE.startswith(result["title"])
    assert len(calls) == 2


def test_speaker_note_is_not_shortened_by_visual_fit_repair(monkeypatch):
    long_note = (
        "Speaker notes may be long because they are read aloud and never "
        "render inside the Template V2 text boxes on the slide canvas."
    )
    assert len(long_note) > 80
    captured = []

    def responder(call, schema, _messages):
        captured.append(schema["properties"]["__speaker_note__"]["maxLength"])
        if call == 1:
            return {"title": _CUT_COVER_TITLE, "__speaker_note__": long_note}
        return {"title": "Vật chất là gì?", "__speaker_note__": long_note}

    _patch_slide_llm(monkeypatch, responder)
    result = _generate(
        _title_layout(19),
        SlideOutlineModel(content=f"## {_COVER_OUTLINE}\nNội dung."),
    )

    assert result["__speaker_note__"] == long_note
    assert result["title"] == "Vật chất là gì?"
    assert captured
    assert all(max_length == 500 for max_length in captured)


def test_nested_card_and_agenda_cuts_are_not_persisted(monkeypatch):
    sources = [
        "Hoàn cảnh ra đời của chủ nghĩa duy vật.",
        "Định nghĩa Lênin về giai cấp.",
        "Các yếu tố tự dưỡng tạo nên sự sống.",
        "Hình thành thông qua ngưng tụ và loãng khí.",
        "Thiếu sự phát triển biến-đổi liên tục.",
    ]
    cut_payload = {
        "title": "Triết học",
        "cards": [
            {
                "title": "Hoàn cảnh ra đ",
                "description": "Các yếu tố tự dư",
            },
            {
                "title": "Định nghĩa Lên",
                "description": "Giai cấp công nhân.",
            },
        ],
        "agenda": [
            "Hình thành thông qua ngưng tụ và lo",
            "Thiếu sự phát triển biến-đ",
        ],
        "__speaker_note__": _NOTE,
    }

    def responder(_call, _schema, _messages):
        return cut_payload

    layout = SlideLayoutModel(
        id="nested-layout",
        json_schema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "minLength": 4, "maxLength": 24},
                "cards": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {
                                "type": "string",
                                "minLength": 4,
                                "maxLength": 24,
                            },
                            "description": {
                                "type": "string",
                                "minLength": 4,
                                "maxLength": 40,
                            },
                        },
                    },
                },
                "agenda": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "minLength": 4,
                        "maxLength": 36,
                    },
                },
            },
            "required": ["title", "cards", "agenda"],
        },
    )
    _patch_slide_llm(monkeypatch, responder)
    result = _generate(
        layout,
        SlideOutlineModel(content="## Nested\n" + " ".join(sources)),
    )

    assert result["cards"][0]["title"] != "Hoàn cảnh ra đ"
    assert result["cards"][1]["title"] != "Định nghĩa Lên"
    assert result["cards"][0]["description"] != "Các yếu tố tự dư"
    assert not result["cards"][0]["description"].endswith("tự dư")
    assert result["agenda"][0] != "Hình thành thông qua ngưng tụ và lo"
    assert not result["agenda"][0].endswith(" lo")
    assert not result["agenda"][1].endswith("biến-đ")
    assert "-" not in result["agenda"][1][-2:]
    assert len(result["cards"][0]["title"]) <= 24
    assert len(result["agenda"][0]) <= 36


def test_slide_text_issues_flag_repair_schema_saturation():
    original = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 25},
        },
    }
    repair = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 20},
        },
    }
    value = {"title": "B" * 20}

    original_issues = generate_slide_content._slide_text_issues(original, value)
    repair_issues = generate_slide_content._slide_text_issues(repair, value)

    assert original_issues == []
    assert any("$.title" in issue and "20/20" in issue for issue in repair_issues)


def test_slide_text_issues_flag_partial_word_under_max_length():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 4, "maxLength": 40},
        },
    }
    issues = generate_slide_content._slide_text_issues(
        schema,
        {"title": "Hoàn cảnh ra đ"},
        ["Hoàn cảnh ra đời của chủ nghĩa duy vật"],
    )

    assert any("$.title" in issue for issue in issues)


def test_safe_fit_text_uses_complete_sentence_not_raw_slice():
    result = generate_slide_content._safe_fit_text(
        _CUT_COVER_TITLE,
        19,
        [_COVER_OUTLINE],
    )

    assert result == "Vật chất là gì?"
    assert len(result) <= 19
    assert result != _COVER_OUTLINE[:19]


def test_long_vietnamese_statement_over_small_ceiling_is_not_cut():
    text = "Triển khai các biện pháp kỹ thuật nhằm ổn định lại hệ thống"
    assert len(text) == 59
    schema = {"type": "string", "minLength": 20, "maxLength": 52}

    result = generate_slide_content._apply_safe_fit_fallback(schema, text, [])

    assert result == text
    assert result.endswith("hệ thống")
    assert result != "Triển khai các biện pháp kỹ thuật nhằm ổn định lại hệ"


def test_vietnamese_heading_within_widened_ceiling_is_not_cut():
    schema = {"type": "string", "minLength": 2, "maxLength": 20}

    result = generate_slide_content._apply_safe_fit_fallback(
        schema, "Cách xử lý", []
    )

    assert result == "Cách xử lý"
    assert result != "Cách xử"


def test_safe_fit_text_does_not_split_combining_marks():
    source = "Cafe\u0301 latte is a tasty drink for the whole table"
    result = generate_slide_content._safe_fit_text(source, 12, [source])

    assert len(unicodedata.normalize("NFC", result)) <= 12 or len(result) <= 12
    if result:
        assert not unicodedata.combining(result[0])
        if unicodedata.combining(result[-1]):
            assert not unicodedata.combining(result[-2])
    assert result != source[:12] or not unicodedata.combining(source[12])
