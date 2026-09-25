from typing import Any


def chat_completion_kwargs(
    model: str,
    messages: list[dict[str, Any]],
    stream: bool,
    tools: list[dict[str, Any]] | None,
    response_format: Any,
    max_tokens: int | None,
    extra_body: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build kwargs shared by OpenAI-compatible Chat Completions calls."""
    chat_kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        chat_kwargs["tools"] = tools
    if response_format is not None:
        chat_kwargs["response_format"] = response_format
    if max_tokens is not None:
        chat_kwargs["max_tokens"] = max_tokens
    if extra_body:
        chat_kwargs["extra_body"] = extra_body
    return chat_kwargs
