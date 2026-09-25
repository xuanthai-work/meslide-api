import asyncio
import json
from pathlib import Path
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from api.v1.ppt.endpoints import prompt as prompt_endpoint
from api.v1.ppt.endpoints.prompt import PROMPT_ROUTER
from models.sql.template_v2 import TemplateV2
from models.template_selection import TemplateSelectionRequest
from services.database import get_async_session
from utils.coded_exception import CodedHTTPException, register_coded_exception_handler
from utils.llm_calls.select_presentation_template import TemplateCandidate
import utils.llm_calls.select_presentation_template as selector_module

load_template_candidates = prompt_endpoint.load_template_candidates
fallback_template_id = prompt_endpoint.fallback_template_id

class FakeResult:
    def __init__(self, rows):
        self.rows = rows
    def scalars(self):
        return self
    def all(self):
        return self.rows

class FakeSession:
    def __init__(self, rows):
        self.rows = rows
    async def execute(self, _query):
        return FakeResult(self.rows)

def endpoint_request():
    return Request({"type": "http", "method": "POST", "path": "/"})

def default_rows():
    return [
        TemplateV2(id="executive", name="Executive", is_default=True, layout_count=2),
        TemplateV2(id="general", name="General", is_default=True, layout_count=2),
    ]

def test_request_normalizes_ids_in_first_seen_order():
    value = TemplateSelectionRequest(
        prompt="  Quarterly board update  ",
        candidate_ids=["executive", "general", "executive"],
    )
    assert value.prompt == "Quarterly board update"
    assert value.candidate_ids == ["executive", "general"]

@pytest.mark.parametrize("prompt", ["   ", "x" * 8001])
def test_request_rejects_blank_or_oversized_prompt(prompt):
    with pytest.raises(ValidationError):
        TemplateSelectionRequest(prompt=prompt, candidate_ids=["general"])

def test_fallback_prefers_general_then_request_order():
    assert fallback_template_id([
        TemplateCandidate("executive", "Executive", "Board updates"),
        TemplateCandidate("general", "General", "Simple decks"),
    ]) == "general"
    assert fallback_template_id([
        TemplateCandidate("executive", "Executive", "Board updates"),
    ]) == "executive"

def test_eligible_candidates_exclude_custom_missing_and_layoutless():
    rows = [
        TemplateV2(id="executive", name="Executive", is_default=True, layout_count=2),
        TemplateV2(id="personal", name="Personal", is_default=False, layout_count=2),
        TemplateV2(id="empty", name="Empty", is_default=True, layout_count=0),
    ]
    result = asyncio.run(load_template_candidates(
        FakeSession(rows), ["personal", "missing", "executive", "empty"],
    ))
    assert [item.id for item in result] == ["executive"]

def test_invalid_model_id_falls_back(monkeypatch):
    async def invalid(*_args, **_kwargs):
        return "personal"
    monkeypatch.setattr(prompt_endpoint, "select_presentation_template", invalid)
    result = asyncio.run(prompt_endpoint.select_template(
        TemplateSelectionRequest(prompt="Board update", candidate_ids=["executive", "general"]),
        Request({"type": "http", "method": "POST", "path": "/"}),
        FakeSession([
            TemplateV2(id="executive", name="Executive", is_default=True, layout_count=2),
            TemplateV2(id="general", name="General", is_default=True, layout_count=2),
        ]),
    ))
    assert result.template_id == "general"

def test_valid_model_id_is_returned(monkeypatch):
    async def choose(*_args, **_kwargs):
        return "executive"
    monkeypatch.setattr(prompt_endpoint, "select_presentation_template", choose)
    result = asyncio.run(prompt_endpoint.select_template(
        TemplateSelectionRequest(prompt="Board update", candidate_ids=["executive", "general"]),
        endpoint_request(), FakeSession(default_rows()),
    ))
    assert result.template_id == "executive"

@pytest.mark.parametrize("bad_choice", [None, "", "personal"])
def test_bad_model_output_uses_general(monkeypatch, bad_choice):
    async def choose(*_args, **_kwargs):
        return bad_choice
    monkeypatch.setattr(prompt_endpoint, "select_presentation_template", choose)
    result = asyncio.run(prompt_endpoint.select_template(
        TemplateSelectionRequest(prompt="Board update", candidate_ids=["executive", "general"]),
        endpoint_request(), FakeSession(default_rows()),
    ))
    assert result.template_id == "general"

def test_provider_failure_uses_general(monkeypatch):
    async def broken(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")
    monkeypatch.setattr(prompt_endpoint, "select_presentation_template", broken)
    result = asyncio.run(prompt_endpoint.select_template(
        TemplateSelectionRequest(prompt="Board update", candidate_ids=["executive", "general"]),
        endpoint_request(), FakeSession(default_rows()),
    ))
    assert result.template_id == "general"

def test_empty_eligible_intersection_returns_coded_error():
    with pytest.raises(CodedHTTPException) as raised:
        asyncio.run(prompt_endpoint.select_template(
            TemplateSelectionRequest(prompt="Board update", candidate_ids=["missing"]),
            endpoint_request(), FakeSession(default_rows()),
        ))
    assert raised.value.error_code == "error.template_selection.no_candidates"

def test_legacy_layout_and_missing_description_are_eligible():
    row = TemplateV2(
        id="editorial", name="Editorial", is_default=True,
        layout_count=None, layouts={"layouts": [{"id": "cover"}]}, description=None,
    )
    candidates = asyncio.run(load_template_candidates(FakeSession([row]), ["editorial"]))
    assert candidates == [TemplateCandidate("editorial", "Editorial", "")]

def test_selector_sends_candidate_metadata_and_schema(monkeypatch):
    captured = {}
    async def fake_generate(_client, _model, **kwargs):
        captured.update(kwargs)
        return {"template_id": "executive"}
    monkeypatch.setattr(selector_module, "get_llm_client", lambda: object())
    monkeypatch.setattr(selector_module, "get_model", lambda: "test-model")
    monkeypatch.setattr(selector_module, "generate_structured_with_schema_retries", fake_generate)
    chosen = asyncio.run(selector_module.select_presentation_template(
        "Board update", [TemplateCandidate("executive", "Executive", "For leaders")],
    ))
    assert chosen == "executive"
    assert json.loads(captured["messages"][1].content)["candidates"][0] == {
        "id": "executive", "name": "Executive", "description": "For leaders",
    }
    assert captured["json_schema"]["additionalProperties"] is False

def test_http_route_returns_selected_id_and_rejects_empty_candidates(monkeypatch):
    async def choose(*_args, **_kwargs):
        return "executive"
    async def session_override():
        return FakeSession(default_rows())
    monkeypatch.setattr(prompt_endpoint, "select_presentation_template", choose)
    app = FastAPI()
    app.include_router(PROMPT_ROUTER, prefix="/api/v1/ppt")
    app.dependency_overrides[get_async_session] = session_override
    register_coded_exception_handler(app)
    client = TestClient(app)
    response = client.post("/api/v1/ppt/prompt/select-template", json={
        "prompt": "Board update", "candidate_ids": ["executive", "general"],
    })
    assert response.status_code == 200
    assert response.json() == {"template_id": "executive"}
    rejected = client.post("/api/v1/ppt/prompt/select-template", json={
        "prompt": "Board update", "candidate_ids": [],
    })
    assert rejected.status_code == 422


def test_editorial_bundle_exposes_a_description_for_selection():
    bundle = Path(__file__).resolve().parents[4] / "templates/editorial/template.json"
    metadata = json.loads(bundle.read_text(encoding="utf-8"))
    assert metadata["description"].strip()
