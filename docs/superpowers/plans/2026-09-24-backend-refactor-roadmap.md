# Backend refactor roadmap sau đợt 0

**Spec:** `docs/superpowers/specs/2026-09-24-backend-only-refactor-design.md`

Đợt 0 đã có trên nhánh `fix/api-ref-0`. Mỗi đợt dưới đây dùng branch và PR riêng; chỉ kế thừa commit của đợt phụ thuộc. Không triển khai cả bảy đợt trên một branch. Ưu tiên phản ánh rủi ro contract và lợi ích giảm coupling, không mở rộng sang DB/S3/Web.

| Đợt | Plan | Ưu tiên | Phụ thuộc cứng | Rủi ro cần review |
| --- | --- | --- | --- | --- |
| 1. Presentation transformations | [Plan 1](2026-09-24-backend-presentation-transformations.md) | P0 | 0 | Layout index, TOC và UI text runs |
| 2. Presentation generation/stream | [Plan 2](2026-09-24-backend-presentation-generation-stream.md) | P0 | 0, 1 | SSE ordering, disconnect, slide commit |
| 3. Template generation/editing | [Plan 3](2026-09-24-backend-template-generation-editing.md) | P0 | 0, 1 | Layout ID/index, bundle và task progress |
| 4. Chat tools/slide UI | [Plan 4](2026-09-24-backend-chat-tools-slide-ui.md) | P1 | 0, 1, 3 | Tool schema, mutation order và trace |
| 5. LLM provider boundaries | [Plan 5](2026-09-24-backend-llm-provider-boundaries.md) | P1 | 0 | Provider kwargs, retry, prompt; phối hợp merge với 2–4 |
| 6. Document loading | [Plan 6](2026-09-24-backend-document-loading.md) | P1 | 0 | Text/OCR fallback, temp cleanup |
| 7. Export/media helpers | [Plan 7](2026-09-24-backend-export-media-helpers.md) | P1 | 0 | Export payload, owner path, public URL; phối hợp merge với 1–3 |

**Thứ tự đề xuất:** 1 → (2 và 3 ở PR riêng) → 4. Đợt 5, 6 và 7 có thể chạy song song sau đợt 0; trước merge đợt 5 và 7 phải rebase lên code presentation/template đã merge rồi chạy lại gate vì dùng chung caller. Không có dependency ngầm đòi Postgres/S3 local.

**Gate chung cho từng PR:** test tập trung trước và sau mỗi task; toàn FastAPI suite bằng `tests/run_suite.py`; Ruff, Vulture, Deptry; `npm test` và `node --test tests/*.test.mjs`; OpenAPI parity 78 operations/119 schemas; review diff xác nhận không đổi SQL/model/session/commit ordering, object storage implementation, auth/middleware hay Web. Pyright kiểm tra ở CI Linux `.venv`. Nếu một task buộc phải đổi boundary bị hoãn, dừng riêng đợt đó và viết spec bổ sung; các đợt khác vẫn độc lập.
