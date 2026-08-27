# gslide-api

GSlide FastAPI runtime + Chromium + LiteParse + đồng bộ `presentation-export` + slide `templates`. Không chứa Next hay nginx.

## Chạy

Chạy qua Compose từ repo [`gslide-ops`](../gslide-ops). Trong container, entrypoint là `node scripts/start-api.js` (khởi động FastAPI bằng `python server.py` tại `servers/fastapi`). Đừng clone Next vào repo này.

## License

Apache 2.0 — xem [LICENSE](./LICENSE) và [NOTICE](./NOTICE).