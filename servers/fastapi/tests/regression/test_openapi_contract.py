import json
from pathlib import Path

from api.main import app


def test_runtime_openapi_matches_recorded_contract():
    spec_path = Path(__file__).resolve().parents[2] / "openai_spec.json"
    recorded = json.loads(spec_path.read_text(encoding="utf-8"))

    assert app.openapi() == recorded
    operations = {
        (path, method)
        for path, methods in recorded["paths"].items()
        for method in methods
        if method in {"get", "post", "put", "patch", "delete"}
    }
    assert len(operations) == 80
    assert len(recorded["components"]["schemas"]) == 123
