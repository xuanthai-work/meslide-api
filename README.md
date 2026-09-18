# gslide-api

GSlide FastAPI runtime + Chromium + LiteParse + đồng bộ `presentation-export` + slide `templates`. Không chứa Next hay nginx.

## Chạy

Repo này tự chạy FastAPI. Next.js phải chạy từ [`gslide-web`](../gslide-web) trên cùng Docker network `gslide`.

```powershell
copy .env.example .env
docker compose up --build api
```

Dev (hot-reload):

```powershell
docker compose up --build api-dev
```

Trong container, entrypoint là `node scripts/start-api.js` (khởi động FastAPI bằng `python server.py` tại `servers/fastapi`). Đừng clone Next vào repo này.

Chromium mở `http://web:3000/pdf-maker` qua `EXPORT_PAGE_BASE_URL`. Trình duyệt không cần gọi thẳng `:8000` — web rewrite `/api/v1` sang API.

Kubernetes: [`deploy/kubernetes/README.md`](./deploy/kubernetes/README.md). Apply API trước (namespace, PVC, secrets, Service `api`), rồi web. Lần deploy này không kèm SearXNG; `WEB_IMAGE_SEARCH_ENABLED=false`.

## Prompt rewrite API

`POST /api/v1/ppt/prompt/rewrite` biến ý tưởng ngắn thành brief có cấu trúc cho
outline generator. Request prompt-only giữ tương thích:

```json
{
  "prompt": "Tạo bài thuyết trình về kết quả kinh doanh Q3",
  "language": "Vietnamese",
  "n_slides": 10
}
```

Để rewrite dựa trên tài liệu, truyền thêm `file_paths` chứa tối đa 8 đường dẫn
file text đã được `/api/v1/ppt/files/decompose` tạo ra:

```json
{
  "prompt": "Tóm tắt báo cáo cho ban điều hành",
  "language": "Vietnamese",
  "n_slides": 10,
  "file_paths": ["owner-id/report.txt"]
}
```

API chỉ chấp nhận file tạm thuộc đúng owner và nằm trong temp root. Nội dung
tài liệu được xem là dữ liệu không tin cậy, được chuyển thành
`DocumentProfile` có giới hạn và cache theo nội dung, ngôn ngữ, phiên bản
profile. Response giữ nguyên contract:

```json
{ "rewritten_prompt": "..." }
```

Profile rút gọn chỉ phục vụ rewrite; outline generation tiếp tục đọc toàn bộ
nội dung đã phân rã. Prompt, nội dung tài liệu, profile và server path không
được ghi vào log ứng dụng.

## License

Apache 2.0 — xem [LICENSE](./LICENSE) và [NOTICE](./NOTICE).
