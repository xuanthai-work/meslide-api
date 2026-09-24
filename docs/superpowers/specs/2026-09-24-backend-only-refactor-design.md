# Spec refactor riêng backend `gslide-api`

**Trạng thái:** Đã duyệt, ngày 2026-09-24.
**Phạm vi:** Chỉ `gslide-api`. Tài liệu này chốt ranh giới thiết kế và tiêu chí nghiệm thu; chưa triển khai refactor.

## Mục tiêu

Refactor backend FastAPI hiện có thành các phần nhỏ, có thể kiểm thử và review độc lập. Tập trung vào luồng presentation, template, chat, LLM, tài liệu, export và media để giảm trách nhiệm trộn lẫn, logic trùng lặp và độ khó bảo trì. Giữ nguyên hành vi backend và API contract hiện tại. Công việc chung giữa `gslide-api` và `gslide-web` sẽ là một giai đoạn khác.

Một đợt đạt yêu cầu khi ranh giới trách nhiệm rõ hơn, kết quả quan sát được không đổi, test của đợt và test toàn repo đều pass, và đợt đó có thể merge riêng. Căn cứ thiết kế là code, OpenAPI sinh từ app và test đang có; spec này không đặt thêm tính năng mới.

## Quyết định phạm vi

- Chỉ sửa trong `gslide-api`. Không sửa `gslide-web`, package client dùng chung hay thiết kế lại hợp đồng frontend/backend.
- **Chưa refactor DB:** không đổi schema, Alembic, SQL model, câu query, cơ chế owner scope, vòng đời session, thứ tự commit, hay tạo repository abstraction mới. Phát hiện audit về đăng ký model `UserProviderSettings` được giữ lại cho đợt DB riêng.
- **Chưa refactor lưu trữ S3/local:** không đổi implementation object storage hoặc giao thức ghi template bundle. Giữ nguyên thứ tự và cách xử lý lỗi khi gọi `TemplateBundleRepository`. Staging dùng Postgres và S3; spec này không đòi dựng hai dịch vụ đó ở local và không thay đổi dữ liệu staging.
- Giữ nguyên auth, middleware, owner `ContextVar`, startup/migration, nơi lưu provider settings và cơ chế lưu trạng thái async task. Việc cần thay các ranh giới này sẽ có spec riêng.
- Không nâng version dependency, thay task runner, thêm queue, hoặc đổi topology deploy. `presentation-export` tiếp tục là runtime ngoài repo.
- Các sai lệch hành vi đã biết, chẳng hạn lỗi 404 ở image delete bị bọc thành 500, sẽ được xử lý như bugfix riêng. Đợt refactor này không âm thầm sửa chúng.

Giới hạn trên áp dụng cho code được thay. Test vẫn có thể đi qua route dùng DB bằng SQLite cô lập sẵn có và dùng fake cho cổng S3/Postgres; không cần credentials hoặc dịch vụ thật ở local.

## Kiến trúc hiện tại và contract cần giữ

`api.main` gắn health, các router `/api/v1` và static assets. `SessionAuthMiddleware` nhận diện principal và đặt owner/provider context. Nhiều endpoint đồng thời làm validation, chuyển đổi dữ liệu, gọi hệ thống ngoài, thao tác DB và dựng response. Các điểm tập trung nhất là generation/stream presentation, tạo và sửa template, cùng chỉnh slide bằng chat. Các module đã tách như `presentation_layout.py`, `presentation_template_content.py`, `templates/v2/generation.py`, `services/chat/*` và `services/image_search/*` là ranh giới để tiếp tục cải thiện; không thay toàn bộ cấu trúc package.

Baseline xác nhận trong worktree: **78 OpenAPI operations, 119 schemas**; `openai_spec.json` bằng `app.openapi()`; 1.277 test FastAPI pass với Python từ `.venv-windows` và SQLite cô lập. Lần audit trước cũng ghi nhận `npm test` và 7 test contract CI/Docker/Compose pass. Pyright trên `.venv-windows` hiện báo import không tìm thấy vì `pyproject.toml` trỏ tới `.venv`; đây là giới hạn môi trường local, chưa được xem là lỗi type của code.

Mọi đợt phải giữ:

1. Path, method, operation ID, tên và alias của field, default, validation, response schema, status code và header.
2. `detail`, `error_code`, `error_params` và nơi xuất hiện lỗi: HTTP JSON, dữ liệu task hay frame SSE.
3. Tên, cấu trúc và thứ tự SSE event; mã progress/status; heartbeat, disconnect, retry và resume.
4. Cookie, auth, phạm vi owner, hình dạng URL `/app_data` và `/static`, cùng đường dẫn file export.
5. Các lần ghi DB và thứ tự commit, chuyển trạng thái background task, thao tác object storage và fallback khi gọi dịch vụ ngoài.

So sánh OpenAPI là cần thiết nhưng chưa bao phủ SSE, nội dung lỗi, task progress và asset URL; phải có test riêng cho các bề mặt này.

## Thiết kế đích

Giữ router là ranh giới API ổn định. Đưa phép tính, ánh xạ payload, chuẩn hóa nội dung và điều phối các lời gọi ngoài vào module nhỏ nằm gần feature đang dùng. Giữ SQL, session, transaction và lời gọi storage ở ranh giới hiện tại. Nếu một endpoint dài chưa thể tách an toàn mà không di chuyển phần persistence, chỉ tách các bước thuần và để chuỗi persistence hiển thị rõ trong endpoint. Giữ các symbol đang được re-export cho test hoặc caller hiện tại cho tới khi có đợt dọn tương thích riêng.

| Ranh giới | Trách nhiệm sau refactor | Phần phải giữ tại ranh giới |
| --- | --- | --- |
| `api/v1/ppt/endpoints/*` | Khai báo FastAPI, bind dependency, chuỗi persistence hiện tại, tạo HTTP/SSE response | Path, response model, DB calls/commits |
| `presentation_*`, `outline_utils`, `process_slides`, `templates/v2/*` | Chọn layout, chuyển đổi content/UI, kiểm tra dữ liệu, các bước generation | Layout ID, template mặc định, hình dạng kết quả |
| `services/chat/*` | Điều phối chat/tool và chuyển đổi slide/UI theo thao tác | SQL history, Mem0 và hành vi tool đã công khai |
| `utils/llm_*`, `utils/llm_calls/*` | Chọn provider, đổi message, sửa structured output | Cách chọn provider, nội dung prompt, retry/timeout |
| Document services | Chọn parser theo định dạng, làm sạch text, fallback | Quy tắc temp path, text trả về, lỗi |
| Export, preview, font, image, icon helpers | Chuyển đổi input/output quanh adapter sẵn có | Export runtime, storage calls, asset URL |

Không thêm framework dependency injection, “base service” dùng chung cho mọi feature, hay abstraction chỉ phục vụ một caller. Chỉ gom logic khi các caller thực sự cần cùng hành vi và cùng contract lỗi/kết quả.

## Các đợt refactor

Mỗi đợt là một PR review và kiểm thử độc lập. Đợt 0 là điều kiện chung; các đợt sau chỉ phụ thuộc nhau tại những điểm được nêu. Sau khi spec được duyệt, viết implementation plan riêng cho từng đợt, không triển khai cả chương trình trên một branch lớn.

| Đợt | Phạm vi và kết quả | Phụ thuộc | Test bắt buộc và rủi ro chính |
| --- | --- | --- | --- |
| **0. Chốt contract** | Thêm assertion phía backend cho OpenAPI, lỗi HTTP tiêu biểu, SSE frame, task progress và public asset URL. Ghi nhận hành vi hiện tại, kể cả hành vi chưa hợp lý. | Không | So với app hiện tại; tránh snapshot metadata sinh tự động không liên quan. |
| **1. Chuyển đổi presentation** | Gom ánh xạ response, tính toán prepare/layout, TOC/outline, slide UI/content vào các module `presentation_*`, `outline_utils`, `process_slides` hiện có. | 0 | Test list/prepare/slide update/template content; rủi ro thứ tự slide, layout ID, default. |
| **2. Generation và stream presentation** | Chia hàm dài thành các pha có tên: outline, nội dung slide, asset scheduling, định dạng event. Giữ nguyên điểm dùng session/commit và background task. | 0, 1 | Generation flow, thứ tự SSE, resume, disconnect/cancel, progress; rủi ro event ảnh chen giữa và ghi dở. |
| **3. Generation và chỉnh template** | Tách ép kiểu request, tạo/gộp layout, chuyển đổi metadata và layout payload khỏi `template.py`; dùng lại `templates/v2`. Giữ nguyên hydrate/save bundle và DB commit. | 0, 1 | Template API/V2 schema/layout, async progress, legacy/bundled response; rủi ro layout ID/thứ tự/default template. |
| **4. Chat tool và chỉnh slide/UI** | Chia tool definition, chuẩn hóa argument và phép biến đổi slide/UI thành module chat tập trung hơn. Không thay SQL history hoặc Mem0 persistence. | 0, 1, 3 | Chat endpoint, tool call, slide UI, trace/SSE, legacy history; rủi ro tên/schema tool và thứ tự mutation. |
| **5. LLM và logic gọi provider** | Tách message conversion, model dispatch, JSON/schema repair và ánh xạ lỗi trong `utils/llm_*` và `utils/llm_calls`. Không đổi cách đọc settings/overlay. | 0; phối hợp với 2–4 tại caller dùng chung | Mock OpenAI/Google/custom, prompt/repair, timeout/lỗi; rủi ro prompt hoặc retry đổi. |
| **6. Nạp tài liệu** | Tách chọn parser, làm sạch text, chọn OCR/conversion và LiteParse fallback trong document services. Giữ temp-file policy và upload contract. | 0 | Fixture TXT/PDF/Office/image, dữ liệu parse lỗi, giới hạn/path/timeout; rủi ro đổi text hoặc cleanup. |
| **7. Helper export và media** | Tách dựng export payload, ánh xạ preview/font, chuẩn hóa image URL và icon result khỏi lời gọi subprocess/storage. Không đổi cách `ExportTaskService` chạy process hay S3. | 0; phối hợp với 1–3 về payload chung | Fixture PPTX/PDF/preview/font/image, output/error path và asset URL; rủi ro khác biệt hình ảnh hoặc đường dẫn. |

Các route nhỏ (`theme`, `icons`, `images`, `fonts`, `prompt`, liệt kê model provider, storage health) chỉ tham gia nếu dùng helper thay đổi ở đợt 5–7; không viết lại router khi chưa có nhu cầu cụ thể. `auth`, `settings`, `webhook`, `mock`, `async-tasks`, health và storage internals giữ nguyên code trong spec này, ngoài test contract ở đợt 0.

## Kiểm thử và tiêu chí merge

- Trước khi tách một hành vi, bổ sung hoặc xác định test mô tả đúng đầu ra và side effect hiện tại. Test hành vi quan sát được, không chỉ xác nhận helper được gọi.
- Sau mỗi thay đổi, chạy test tập trung; trước merge chạy `tests/run_suite.py`, test Node ở root, Ruff/Vulture/Deptry và các gate CI áp dụng. So sánh `app.openapi()` với `openai_spec.json` cùng fixture ở đợt 0.
- Dùng fake tại biên LLM, OCR, export runtime, image provider và object storage. SQLite hiện có đủ cho kiểm thử route/session ở local; không yêu cầu Postgres/S3 local. Kiểm tra staging nếu sau này cần là bước vận hành riêng và không mở rộng phạm vi refactor này.
- Review diff để phát hiện thay đổi SQL, migration, model, middleware, config hoặc storage implementation. Nếu cần đụng các phần bị loại trừ, tách ra spec riêng trước khi merge.
- Chỉ merge khi test của đợt và suite liên quan pass, API/OpenAPI không đổi, reviewer giải thích được call path cũ và mới. Rollback bằng PR/commit của đúng đợt đó, không cần data migration.

## Rủi ro và giới hạn

OpenAPI không mô tả đầy đủ stream frame hoặc nội dung lỗi, nên đợt 0 phải khóa chúng bằng test riêng. Snapshot quá rộng có thể giữ cả text nội bộ không quan trọng; chỉ assert field và thứ tự client quan sát được. Flow lớn dùng SQL model mutable và state cấp module; việc tách code không được dời lịch coroutine, context capture hoặc commit. SQLite/fake không chứng minh đầy đủ hành vi Postgres/S3, nên spec giữ nguyên các adapter và semantics persistence đó. Contract test chỉ phía backend chưa chứng minh mọi call site của `gslide-web`; việc đối chiếu hai repo dành cho giai đoạn chung sau này.

## Điểm cần người review xác nhận

Xác nhận rằng phạm vi hoãn DB và S3 phản ánh đúng ràng buộc staging, bảy đợt ứng dụng trên là trọng tâm backend mong muốn, và việc giữ cả hành vi lỗi/task/SSE hiện tại phù hợp với giai đoạn này. Sau khi spec được duyệt, bước tiếp theo là viết implementation plan cho **đợt 0**; chưa bắt đầu sửa code từ tài liệu này.
