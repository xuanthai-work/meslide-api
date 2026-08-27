import asyncio

from utils.llm_calls.generate_web_search_query import generate_web_search_query


def test_generate_web_search_query_returns_normalized_query(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    captured = {}

    async def fake_generate(_client, _model, **kwargs):
        captured.update(kwargs)
        return {"query": "  latest   Nepal economy statistics 2026  "}

    monkeypatch.setattr(
        "utils.llm_calls.generate_web_search_query.generate_structured_with_schema_retries",
        fake_generate,
    )

    query = asyncio.run(
        generate_web_search_query(
            object(),
            "fake-model",
            "Create a presentation about Nepal's economy",
            "Use current statistics",
        )
    )

    assert query == "latest Nepal economy statistics 2026"
    assert captured["response_format"].name == "web_search_query"
    assert "TODAY'S DATE:" in str(captured["messages"][1].content)


def test_generate_web_search_query_returns_none_for_invalid_empty_result(monkeypatch):
    monkeypatch.setenv("LLM", "openai")

    async def fake_generate(_client, _model, **_kwargs):
        return {"query": None}

    monkeypatch.setattr(
        "utils.llm_calls.generate_web_search_query.generate_structured_with_schema_retries",
        fake_generate,
    )

    query = asyncio.run(
        generate_web_search_query(
            object(),
            "fake-model",
            "A complete supplied factual report",
        )
    )

    assert query is None
