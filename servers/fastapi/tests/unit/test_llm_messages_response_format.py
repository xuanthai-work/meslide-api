import json

from pydantic import BaseModel

from enums.llm_provider import LLMProvider
from utils.llm_messages import JSONSchemaResponse, build_response_format


class _SampleLayout(BaseModel):
    title: str


def test_build_response_format_serializes_pydantic_class_for_openai_compat():
    built = build_response_format(
        JSONSchemaResponse(name="SampleLayoutResponse", json_schema=_SampleLayout),
        provider=LLMProvider.CUSTOM,
    )

    parsed = json.loads(json.dumps(built))

    assert parsed["type"] == "json_schema"
    assert parsed["json_schema"]["name"] == "SampleLayoutResponse"
    schema = parsed["json_schema"]["schema"]
    assert schema["type"] == "object"
    assert "title" in schema["properties"]


def test_build_response_format_serializes_pydantic_class_for_google():
    built = build_response_format(
        JSONSchemaResponse(name="SampleLayoutResponse", json_schema=_SampleLayout),
        provider=LLMProvider.GOOGLE,
    )

    parsed = json.loads(json.dumps(built))

    assert parsed["response_mime_type"] == "application/json"
    assert parsed["response_schema"]["type"] == "object"
    assert "title" in parsed["response_schema"]["properties"]
