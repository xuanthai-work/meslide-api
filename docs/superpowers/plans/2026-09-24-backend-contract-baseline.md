# Backend Contract Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Khóa các bề mặt API backend đang thiếu test contract trước khi refactor presentation, template, chat và các adapter.

**Architecture:** Đây là đợt 0 của spec backend-only. Chỉ thêm test hồi quy ở `servers/fastapi/tests`; so sánh app đang chạy với `openai_spec.json` và kiểm tra các response thực qua router/hàm stream. Không đổi production code, DB, S3 hay Web.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, pytest, httpx/TestClient, SQLite test runner hiện có; Node 20 cho gate ở root.

**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

## Global Constraints

- Chỉ làm trong `gslide-api`; không sửa `gslide-web` hoặc thiết kế lại API contract.
- Không đổi schema, Alembic, SQL model, query, owner scope, session, thứ tự commit, object-storage implementation, hoặc template-bundle protocol.
- Không đổi auth, middleware, task runner, cách lưu async task, dependency version hoặc deployment.
- Giữ 78 OpenAPI operations, 119 schemas và toàn bộ request/response/status/header hiện tại.
- Giữ nguyên `detail`, `error_code`, `error_params`, frame SSE, progress/task data và public asset URL hiện tại, kể cả hành vi lỗi chưa hợp lý.
- Test dùng runner SQLite cô lập và fake; không đòi Postgres, S3, LLM, OCR hoặc Chromium thật ở local.
- Đây là characterization tests: chạy trên baseline phải PASS ngay. Không cố tạo lỗi ở production code chỉ để có bước red của TDD; giá trị của test là chặn hồi quy trong các đợt sau.

## Review Focus

1. Một thay đổi decorator làm lệch alias/default/operation ID của route: Task 1 so toàn bộ OpenAPI app với bản đã lưu.
2. Refactor bắt nhầm lỗi HTTP hoặc vô tình sửa image-delete 404→500: Task 2 khóa status và body của hai route thật.
3. Stream presentation đổi frame đầu/cuối hoặc thứ tự slide khi resume: Task 3 đọc frame từ route stream.
4. Progress tạo template hoàn thành layout không theo thứ tự: Task 4 khóa danh sách trạng thái theo `index`.
5. URL ảnh/icon khác nhau giữa same-origin và public API origin: Task 5 khóa hai cấu hình và URL ngoài.

## File map

| File | Vai trò |
| --- | --- |
| `servers/fastapi/tests/regression/test_openapi_contract.py` | So OpenAPI sinh từ `api.main.app` với `openai_spec.json`. |
| `servers/fastapi/tests/regression/test_http_error_contract.py` | Kiểm tra status/body qua template và image router thật với fake session. |
| `servers/fastapi/tests/unit/test_presentation_stream_resume.py` | Bổ sung một test về wire format và slide order vào fixture stream đã có. |
| `servers/fastapi/tests/regression/test_template_task_contract.py` | Khóa progress payload khi layout hoàn thành lệch thứ tự. |
| `servers/fastapi/tests/regression/test_asset_url_contract.py` | Khóa URL asset khi public origin có/không có. |

Không tạo helper dùng chung cho các test nhỏ này; fixture `FakeAsyncSession` và `_consume` hiện có được dùng lại đúng nơi.

Sau khi người dùng duyệt plan và trước khi tạo worktree để thực hiện, đưa bản spec và plan đã duyệt vào nhánh làm việc để worktree đọc được hai tài liệu. Hiện chúng là bản nháp chưa commit; bước chuẩn bị này không sửa product code.

---

### Task 1: OpenAPI parity

**Files:**
- Create: `servers/fastapi/tests/regression/test_openapi_contract.py`
- Read: `servers/fastapi/api/main.py`, `servers/fastapi/openai_spec.json`

**Interfaces:**
- Consumes: `api.main.app.openapi()` và JSON trong `openai_spec.json`.
- Produces: test gate `test_runtime_openapi_matches_recorded_contract`; không có runtime interface mới.

- [ ] **Step 1: Tạo test so contract app và bản lưu.**

```python
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
    assert len(operations) == 78
    assert len(recorded["components"]["schemas"]) == 119
```

- [ ] **Step 2: Chạy test tập trung.** Từ `servers/fastapi`, chạy `uv run --locked python tests/run_suite.py -q tests/regression/test_openapi_contract.py`. Kỳ vọng `1 passed`. Nếu khác, điều tra drift trước khi chấp nhận baseline; không sửa `openai_spec.json` để làm test pass.
- [ ] **Step 3: Review diff và commit test.** Chạy `git add servers/fastapi/tests/regression/test_openapi_contract.py`, `git diff --cached --check`, rồi `git commit -m "test: pin runtime OpenAPI contract"`.

### Task 2: HTTP error qua route thật

**Files:**
- Create: `servers/fastapi/tests/regression/test_http_error_contract.py`
- Read: `servers/fastapi/api/v1/ppt/endpoints/template.py:1756`, `servers/fastapi/api/v1/ppt/endpoints/images.py:191`, `servers/fastapi/utils/coded_exception.py`

**Interfaces:**
- Consumes: `TEMPLATE_ROUTER`, `IMAGES_ROUTER`, `get_async_session`, `register_coded_exception_handler`, `FakeAsyncSession`.
- Produces: hai test gate cho HTTP status/body hiện tại; không có runtime interface mới.

- [ ] **Step 1: Tạo test thực hiện request qua hai router.**

```python
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.v1.ppt.endpoints.images import IMAGES_ROUTER
from api.v1.ppt.endpoints.template import TEMPLATE_ROUTER
from services.database import get_async_session
from tests.conftest import FakeAsyncSession
from utils.coded_exception import register_coded_exception_handler


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(TEMPLATE_ROUTER)
    app.include_router(IMAGES_ROUTER)
    app.dependency_overrides[get_async_session] = lambda: FakeAsyncSession()
    register_coded_exception_handler(app)
    return TestClient(app)


def test_missing_template_keeps_coded_404_body():
    response = _client().get("/template/missing")
    assert response.status_code == 404
    assert response.json() == {
        "detail": "Template not found",
        "error_code": "error.template.not_found",
    }


def test_missing_image_delete_keeps_current_wrapped_error_body():
    image_id = uuid.UUID("11111111-2222-3333-4444-555555555555")
    response = _client().delete(f"/images/{image_id}")
    assert response.status_code == 500
    assert response.json() == {
        "detail": "Failed to delete image: 404: Image not found",
        "error_code": "error.image.delete_failed",
        "error_params": {"reason": "404: Image not found"},
    }
```

- [ ] **Step 2: Chạy test tập trung.** `uv run --locked python tests/run_suite.py -q tests/regression/test_http_error_contract.py`; kỳ vọng `2 passed`. Test thứ hai ghi lại hành vi hiện tại; việc đổi sang 404 phải là bugfix riêng ngoài plan.
- [ ] **Step 3: Review diff và commit.** Chạy `git add servers/fastapi/tests/regression/test_http_error_contract.py`, `git diff --cached --check`, rồi `git commit -m "test: pin backend HTTP error responses"`.

### Task 3: Wire format của presentation stream

**Files:**
- Modify: `servers/fastapi/tests/unit/test_presentation_stream_resume.py`
- Read: `servers/fastapi/api/v1/ppt/endpoints/presentation.py:273`, `servers/fastapi/models/sse_response.py`

**Interfaces:**
- Consumes: `_presentation`, `_saved_slide`, `_SessionWithSlides`, `_consume` đã nằm trong file test.
- Produces: test gate `test_resumed_stream_keeps_first_chunk_last_complete_and_slide_order`.

- [ ] **Step 1: Thêm `import json` và test sau `test_stream_reuses_saved_slide_and_generates_only_the_remainder`.**

```python
import json


def test_resumed_stream_keeps_first_chunk_last_complete_and_slide_order():
    presentation_id = uuid.uuid4()
    session = _SessionWithSlides(
        _presentation(presentation_id),
        [_saved_slide(presentation_id)],
    )

    async def next_slide(*_args, **_kwargs):
        return {"hero": {"headline": "Effects"}, "__speaker_note__": ""}

    frames = _consume(session, next_slide)
    assert all(frame.startswith("event: response\ndata: ") for frame in frames)
    events = [json.loads(frame.split("data: ", 1)[1].split("\n", 1)[0]) for frame in frames]

    assert events[0] == {"type": "chunk", "chunk": '{ "slides": [ '}
    assert {"type": "chunk", "chunk": " ] }"} in events
    assert events[-1]["type"] == "complete"
    assert [slide["index"] for slide in events[-1]["presentation"]["slides"]] == [0, 1]
```

- [ ] **Step 2: Chạy test tập trung.** `uv run --locked python tests/run_suite.py -q tests/unit/test_presentation_stream_resume.py::test_resumed_stream_keeps_first_chunk_last_complete_and_slide_order`; kỳ vọng `1 passed`. Sau đó chạy cả `tests/unit/test_presentation_stream_resume.py`; kỳ vọng không có regression.
- [ ] **Step 3: Review diff và commit.** Chạy `git add servers/fastapi/tests/unit/test_presentation_stream_resume.py`, `git diff --cached --check`, rồi `git commit -m "test: pin presentation stream wire order"`.

### Task 4: Progress template theo layout index

**Files:**
- Create: `servers/fastapi/tests/regression/test_template_task_contract.py`
- Read: `servers/fastapi/api/v1/ppt/endpoints/template.py:255`

**Interfaces:**
- Consumes: `_template_task_progress_data` hiện có.
- Produces: test gate `test_template_progress_keeps_indexed_statuses_and_nullable_metadata`.

- [ ] **Step 1: Tạo test cho kết quả hoàn thành không theo thứ tự index.**

```python
from api.v1.ppt.endpoints.template import _template_task_progress_data


def test_template_progress_keeps_indexed_statuses_and_nullable_metadata():
    data = _template_task_progress_data(
        created_layouts=1,
        remaining_layouts=2,
        name="Deck",
        thumbnail=None,
        completed_layout_indices={2},
    )
    assert data == {
        "created_layouts": 1,
        "remaining_layouts": 2,
        "slide_layout_statuses": [
            {"index": 0, "status": "pending"},
            {"index": 1, "status": "pending"},
            {"index": 2, "status": "completed"},
        ],
        "name": "Deck",
        "thumbnail": None,
    }
```

- [ ] **Step 2: Chạy test tập trung.** `uv run --locked python tests/run_suite.py -q tests/regression/test_template_task_contract.py`; kỳ vọng `1 passed`.
- [ ] **Step 3: Review diff và commit.** Chạy `git add servers/fastapi/tests/regression/test_template_task_contract.py`, `git diff --cached --check`, rồi `git commit -m "test: pin template task progress contract"`.

### Task 5: Public asset URL theo origin

**Files:**
- Create: `servers/fastapi/tests/regression/test_asset_url_contract.py`
- Read: `servers/fastapi/utils/asset_directory_utils.py:38`, `servers/fastapi/utils/get_env.py:29`

**Interfaces:**
- Consumes: `filesystem_image_path_to_app_data_url`, `normalize_slide_asset_url` và hai biến môi trường hiện có.
- Produces: test gate `test_asset_urls_keep_same_origin_and_public_api_shapes`.

- [ ] **Step 1: Tạo test cho same-origin, public API origin và URL ngoài.**

```python
from utils.asset_directory_utils import (
    filesystem_image_path_to_app_data_url,
    normalize_slide_asset_url,
)


def test_asset_urls_keep_same_origin_and_public_api_shapes(monkeypatch, tmp_path):
    app_data = tmp_path / "app_data"
    image = app_data / "images" / "users" / "owner" / "slide.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"image")
    monkeypatch.setenv("APP_DATA_DIRECTORY", str(app_data))
    monkeypatch.delenv("NEXT_PUBLIC_FAST_API", raising=False)

    path_only = "/app_data/images/users/owner/slide.png"
    assert filesystem_image_path_to_app_data_url(str(image)) == path_only

    monkeypatch.setenv("NEXT_PUBLIC_FAST_API", "https://api.example.test/")
    assert filesystem_image_path_to_app_data_url(str(image)) == (
        "https://api.example.test" + path_only
    )
    assert normalize_slide_asset_url("/static/icons/placeholder.svg") == (
        "https://api.example.test/static/icons/placeholder.svg"
    )
    remote = "https://cdn.example.test/slide.png"
    assert normalize_slide_asset_url(remote) == remote
```

- [ ] **Step 2: Chạy test tập trung.** `uv run --locked python tests/run_suite.py -q tests/regression/test_asset_url_contract.py`; kỳ vọng `1 passed`.
- [ ] **Step 3: Chạy gate toàn đợt.** Từ `servers/fastapi`, chạy `uv run --locked ruff check .`, `uv run --locked vulture`, `uv run --locked deptry .`, `uv run --locked python tests/run_suite.py -q`; từ repo root chạy `npm test` và `node --test tests/*.test.mjs`. Kỳ vọng tất cả pass. Chạy Pyright trong môi trường CI Linux chuẩn `.venv`; không nới phạm vi Pyright hoặc đổi cấu hình venv trong đợt này chỉ để né lỗi máy Windows.
- [ ] **Step 4: Kiểm tra phạm vi và commit.** Chạy `git status --short`; kiểm tra các commit Task 1–4 và phần đang chờ commit để xác nhận chỉ có năm file test cùng tài liệu spec/plan, không có production code/DB/S3/Web đổi. Chạy `git add servers/fastapi/tests/regression/test_asset_url_contract.py`, `git diff --cached --check`, rồi `git commit -m "test: pin public asset URL contract"`.

## Self-review trước khi thực hiện

- Task 1 khóa toàn bộ OpenAPI; Task 2, 3, 4, 5 lần lượt khóa lỗi HTTP, SSE, template progress và URL mà OpenAPI không mô tả đầy đủ.
- Task 2 cố ý assert hình dạng lỗi image-delete hiện tại; không được tự sửa hành vi trong PR này.
- Tất cả code block chỉ thêm test. Không có thay đổi DB, S3, middleware, provider config hoặc runtime.
- Mỗi task có một test file hoặc một phần test hiện có, lệnh kiểm tra cụ thể và commit riêng; toàn bộ đợt có gate full suite trước khi merge.
