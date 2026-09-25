from fastapi import HTTPException

from utils.llm_client_error_handler import handle_llm_client_exceptions


def test_http_exception_keeps_identity_and_status(monkeypatch):
    monkeypatch.setattr(
        handle_llm_client_exceptions.__globals__["traceback"],
        "print_exc",
        lambda: None,
    )
    original = HTTPException(status_code=429, detail="slow down")

    normalized = handle_llm_client_exceptions(original)

    assert normalized is original
    assert normalized.status_code == 429
    assert normalized.detail == "slow down"
