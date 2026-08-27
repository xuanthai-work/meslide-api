import copy

from utils.llm_utils import extract_structured_content, serialize_structured_content
from utils.schema_utils import (
    ensure_array_schemas_have_items,
    get_schema_definition_errors,
    get_schema_validation_errors,
    validate_response_schema_definition,
)


def test_extract_structured_content_from_json_text():
    payload = extract_structured_content('{"slides": [{"content": "A"}]}')
    assert payload == {"slides": [{"content": "A"}]}
    assert type(payload) is dict
    assert type(payload["slides"]) is list
    assert type(payload["slides"][0]) is dict


def test_extract_structured_content_nested_values_are_deepcopyable():
    payload = extract_structured_content(
        '{"component": {"title": "Hello", "items": [{"x": 1}]}}'
    )

    assert type(payload) is dict
    assert type(payload["component"]) is dict
    assert type(payload["component"]["items"]) is list
    assert type(payload["component"]["items"][0]) is dict
    assert copy.deepcopy(payload) == payload


def test_serialize_structured_content_prefers_json_serialization():
    serialized = serialize_structured_content({"slides": [{"content": "A"}]})
    assert serialized == '{"slides": [{"content": "A"}]}'


def test_get_schema_validation_errors_reports_path_and_message():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "maxLength": 5},
        },
        "required": ["title"],
        "additionalProperties": False,
    }
    errors = get_schema_validation_errors(schema, {"title": "too long title"}, strict=False)
    assert errors
    assert any("too long" in e.lower() for e in errors)


def test_get_schema_definition_errors_detects_min_length_greater_than_max_length():
    schema = {
        "type": "object",
        "properties": {
            "header_section_part_1": {
                "type": "object",
                "properties": {
                    "main_heading_start": {
                        "type": "string",
                        "minLength": 27,
                        "maxLength": 25,
                    }
                },
                "required": ["main_heading_start"],
            }
        },
        "required": ["header_section_part_1"],
    }

    errors = get_schema_definition_errors(schema)
    assert len(errors) == 1
    assert "main_heading_start" in errors[0]
    assert "minLength (27)" in errors[0]
    assert "maxLength (25)" in errors[0]


def test_validate_response_schema_definition_raises_for_unsatisfiable_schema():
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 10, "maxLength": 5},
        },
    }

    try:
        validate_response_schema_definition(schema)
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "Invalid response schema" in str(exc)
        assert "minLength (10)" in str(exc)


def test_ensure_array_schemas_have_items_adds_missing_items_recursively():
    schema = {
        "type": "object",
        "properties": {
            "slides": {
                "type": "array",
                "items": {"type": "object", "properties": {"tags": {"type": "array"}}},
            }
        },
    }

    fixed = ensure_array_schemas_have_items(schema)

    assert fixed["properties"]["slides"]["items"]["properties"]["tags"]["items"] == {
        "type": "string"
    }
