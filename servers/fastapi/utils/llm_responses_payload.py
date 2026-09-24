from typing import Any


def _to_responses_text_format(response_format: Any) -> Any:
    """Unwrap Chat Completions ``response_format`` into Responses ``text.format``."""
    if not isinstance(response_format, dict):
        return response_format
    inner = response_format.get("json_schema")
    if response_format.get("type") == "json_schema" and isinstance(inner, dict):
        return {
            "type": "json_schema",
            "name": inner.get("name"),
            "schema": inner.get("schema"),
            "strict": inner.get("strict", False),
        }
    return response_format


def _tools_for_responses_api(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Chat Completions function-tool wrappers for Responses."""
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            converted.append(tool)
            continue
        nested = tool.get("function")
        if tool.get("type") == "function" and isinstance(nested, dict):
            converted.append(
                {
                    "type": "function",
                    "name": nested.get("name"),
                    "description": nested.get("description"),
                    "parameters": nested.get("parameters"),
                    "strict": nested.get("strict", False),
                }
            )
            continue
        converted.append(tool)
    return converted
