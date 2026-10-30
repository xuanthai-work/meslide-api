# meslide-api

Runtime FastAPI của MeSlide: sinh và quản lý bài thuyết trình, xử lý tài liệu, gọi LLM, và xuất PPTX/PDF qua Chromium.

Repo này không chứa Next.js hay reverse proxy. Giao diện chạy ở [`meslide-web`](../meslide-web).

## Chạy local

Cần Docker. Từ thư mục repo:

```powershell
copy .env.example .env
docker compose up --build api
```

Hot-reload khi phát triển:

```powershell
docker compose up --build api-dev
```

API lắng nghe `http://localhost:8000`. UI chạy từ `meslide-web` trên cùng Docker network `meslide` tại [http://localhost:3000](http://localhost:3000). Trình duyệt không gọi thẳng cổng 8000; web chuyển tiếp `/api/v1` tới service `api`.

## Health

Probe nằm ở **root process**, không dưới `/api/v1` (đó là API sản phẩm, có version và auth).

| Path | Ý nghĩa | Khi fail |
| --- | --- | --- |
| `GET /health/live` | Process còn nhận request. Không đụng DB/storage. | Docker/K8s **liveness** → restart container |
| `GET /health/ready` | DB (`SELECT 1`), Alembic head, object storage. `200` khi cả ba `ok`, `503` kèm `checks` nếu chưa. | Compose/K8s **readiness** → cắt traffic, không restart |

Compose `api` / `api-dev` ping `http://127.0.0.1:8000/health/ready`. Kubelet gọi thẳng pod `:8000`, không qua Ingress.

Chromium trong container này render trang xuất của web qua `EXPORT_PAGE_BASE_URL` (mặc định `http://web:3000`).

Biến môi trường nằm trong [`.env.example`](./.env.example). Điền LLM provider trước khi sinh slide. Sau khi sửa `.env`, recreate container:

```powershell
docker compose up --build --force-recreate api
```

Tìm kiếm ảnh web (SearXNG) không đi kèm bản deploy này. Để tắt, giữ `WEB_IMAGE_SEARCH_ENABLED=false`.

## Kubernetes

Xem [`deploy/kubernetes/README.md`](./deploy/kubernetes/README.md). Apply API trước (namespace, PVC, secrets, Service `api`), sau đó apply web.

## License

Apache 2.0 — xem [LICENSE](./LICENSE) và [NOTICE](./NOTICE).
