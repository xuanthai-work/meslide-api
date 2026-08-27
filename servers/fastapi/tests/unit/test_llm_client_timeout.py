import httpx

from utils.llm_provider import get_llm_client


def test_openai_client_uses_180s_timeout(monkeypatch):
    monkeypatch.setenv("LLM", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    captured = {}

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("utils.llm_provider.OpenAI", FakeOpenAI)
    get_llm_client()
    timeout = captured["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == 180.0 or timeout.timeout == 180.0