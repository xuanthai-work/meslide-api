import asyncio
import json
from types import SimpleNamespace

from utils.llm_messages import (
    JSONSchemaResponse,
    ReasoningConfig,
    SystemMessage,
    Tool,
    UserMessage,
    WebSearchTool,
)
from utils.llm_utils import get_generate_kwargs, stream_generate_events


def _schema():
    return {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }


def _messages():
    return [
        SystemMessage(content="You are a slide generator."),
        UserMessage(content="Make a title slide"),
    ]


async def _collect(client, **kwargs):
    events = []
    async for event in stream_generate_events(client, **kwargs):
        events.append(event)
    return events


class _ChatCompletions:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"title":"Hello"}',
                        tool_calls=None,
                    )
                )
            ],
            usage=None,
        )


class _Responses:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text='{"title":"Hello"}',
            output=[],
            usage=None,
        )


def _openai_client(chat=None, responses=None):
    chat = chat or _ChatCompletions()
    responses = responses or _Responses()
    return SimpleNamespace(
        chat=SimpleNamespace(completions=chat),
        responses=responses,
        _chat=chat,
        _responses=responses,
    )


def test_openai_without_web_search_uses_chat_completions(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = _openai_client()

    events = asyncio.run(
        _collect(
            client,
            **get_generate_kwargs(
                model="gpt-4.1",
                messages=_messages(),
                response_format=JSONSchemaResponse(
                    name="SlideTitle",
                    json_schema=_schema(),
                ),
                stream=False,
            ),
        )
    )

    assert client._responses.calls == []
    assert len(client._chat.calls) == 1
    kwargs = client._chat.calls[0]
    assert kwargs["messages"][0]["role"] == "system"
    assert kwargs["response_format"]["type"] == "json_schema"
    assert "json_schema" in kwargs["response_format"]
    json.dumps(kwargs["response_format"])
    assert events[-1].content == '{"title":"Hello"}'


def test_openai_web_search_uses_responses_with_unwrapped_schema(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = _openai_client()

    events = asyncio.run(
        _collect(
            client,
            **get_generate_kwargs(
                model="gpt-4.1",
                messages=_messages(),
                response_format=JSONSchemaResponse(
                    name="SlideTitle",
                    json_schema=_schema(),
                ),
                tools=[WebSearchTool()],
                stream=False,
            ),
        )
    )

    assert client._chat.calls == []
    assert len(client._responses.calls) == 1
    kwargs = client._responses.calls[0]
    assert kwargs["tools"] == [{"type": "web_search"}]
    assert "input" in kwargs
    fmt = kwargs["text"]["format"]
    assert fmt["type"] == "json_schema"
    assert fmt["name"] == "SlideTitle"
    assert fmt["schema"] == _schema()
    assert "json_schema" not in fmt
    json.dumps(fmt)
    assert events[-1].content == '{"title":"Hello"}'


def test_openai_reasoning_uses_responses_api(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    client = _openai_client()

    asyncio.run(
        _collect(
            client,
            **get_generate_kwargs(
                model="gpt-5",
                messages=_messages(),
                reasoning=ReasoningConfig(enabled=True, effort="low"),
                stream=False,
            ),
        )
    )

    assert client._chat.calls == []
    assert len(client._responses.calls) == 1
    assert client._responses.calls[0]["reasoning"] == {"effort": "low"}


class _AsyncChunkStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)

    def __aiter__(self):
        self._iter = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _google_chunk(text: str):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            text=text,
                            thought=False,
                            function_call=None,
                        )
                    ]
                )
            )
        ],
        usage_metadata=None,
        text=text,
    )


class _GoogleModels:
    def __init__(self):
        self.stream_calls = []

    async def generate_content_stream(self, **kwargs):
        self.stream_calls.append(kwargs)
        return _AsyncChunkStream([_google_chunk("Hello from Gemini")])


def _google_client():
    models = _GoogleModels()
    return SimpleNamespace(
        aio=SimpleNamespace(models=models),
        models=models,
        _models=models,
    )


def test_google_stream_awaits_sdk_and_yields_text(monkeypatch):
    monkeypatch.setenv("LLM", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client = _google_client()

    events = asyncio.run(
        _collect(
            client,
            **get_generate_kwargs(
                model="gemini-2.0-flash",
                messages=_messages(),
                stream=True,
            ),
        )
    )

    assert len(client._models.stream_calls) == 1
    assert [event.chunk for event in events if event.type == "content"] == [
        "Hello from Gemini"
    ]


def test_google_function_tools_and_system_go_in_config(monkeypatch):
    monkeypatch.setenv("LLM", "google")
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    client = _google_client()

    asyncio.run(
        _collect(
            client,
            **get_generate_kwargs(
                model="gemini-2.0-flash",
                messages=_messages(),
                tools=[
                    Tool(
                        name="previewSlide",
                        description="Preview a slide",
                        input_schema={"type": "object", "properties": {}},
                    )
                ],
                max_tokens=8192,
                stream=True,
            ),
        )
    )

    kwargs = client._models.stream_calls[0]
    assert "tools" not in kwargs
    config = kwargs["config"]
    assert config["system_instruction"] == "You are a slide generator."
    assert config["max_output_tokens"] == 8192
    assert kwargs["contents"][0]["role"] == "user"
    function_tools = [
        item for item in config["tools"] if "function_declarations" in item
    ]
    assert function_tools[0]["function_declarations"][0]["name"] == "previewSlide"

