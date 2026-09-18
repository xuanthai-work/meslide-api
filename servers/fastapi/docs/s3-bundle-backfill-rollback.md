# S3/MinIO template bundle — backfill & rollback

Áp dụng cho branch `feat/s3-object-storage` (PR1 + PR2 + PR3), theo plan
`artifacts/s3-minio-object-storage-plan-2026-09-16.md`.

## Trạng thái sau PR3

- **Row mới / row được ghi**: bundle JSON nằm trong object storage, DB row
  chỉ giữ metadata (`bundle_object_key`, `bundle_sha256`, `bundle_size_bytes`,
  `bundle_schema_version`, `layout_count`). Cột JSON legacy
  (`raw_layouts`, `layouts`, `merged_components`) được ghi `NULL` sau mỗi
  lần save.
- **Row legacy (trước backfill)**: vẫn còn payload trong cột JSON; read flow
  hydrate ưu tiên bundle, fallback về cột legacy khi `bundle_object_key`
  is NULL. Backfill thêm reference nhưng cố ý giữ payload này trong rollback
  window; việc null/drop hàng loạt là một release riêng sau khi verify.
- **Cột JSON legacy chưa bị drop** — drop là release riêng, sau khi hết
  rollback window.

## Backfill

Script idempotent, chạy riêng lẻ với Alembic schema migration:

```bash
# Xem trước, không ghi gì
python scripts/backfill_template_bundles.py --dry-run

# Chạy thật (theo batch, commit từng row, resume an toàn khi crash)
python scripts/backfill_template_bundles.py --batch-size 50 --report report.json

# Một template cụ thể (ví dụ row bị fail trong report)
python scripts/backfill_template_bundles.py --template-id <id>

# Verify toàn bộ reference + checksum sau backfill (cutover bước 4)
python scripts/backfill_template_bundles.py --verify
```

Exit code ≠ 0 nghĩa là report có `failures` — xem JSON report để xử lý từng
row. Script không bao giờ xoá/clear cột JSON legacy của row đang backfill.

## Thứ tự bật S3/MinIO

`bundle_object_key` là key logic, không chứa tên backend. Vì vậy không được
ghi bundle bằng backend `local` rồi chỉ đổi `OBJECT_STORAGE_BACKEND=s3`: DB sẽ
giữ nguyên key nhưng object vẫn nằm trên filesystem cũ.

Rollout an toàn:

1. Khởi động MinIO/S3, tạo bucket và kiểm tra health.
2. Cấu hình toàn bộ API instance dùng `OBJECT_STORAGE_BACKEND=s3`.
3. Chạy backfill (dry-run trước, chạy thật sau), rồi chạy `--verify` trên chính
   backend S3 đó.
4. Chỉ sau khi verify sạch mới coi S3 là source of truth và bắt đầu rollback
   window.

Nếu môi trường đã có `bundle_object_key` được ghi bằng backend `local`, phải
copy toàn bộ `APP_DATA_DIRECTORY/object-storage/` sang bucket với cùng object
key trước khi đổi backend, rồi chạy `--verify`. Default template có thể được
re-seed từ disk, nhưng custom template thì không được dựa vào cơ chế đó.

## Verify

`--verify` đọc lại từng object theo `bundle_object_key`, so `sha256` với
`bundle_sha256` và deserialize payload. Row legacy chưa backfill không tính
là failure.

## Rollback

**Rollback trong PR1–PR3 code (an toàn):**
- Giữ nguyên backend S3/MinIO và rollback code về commit trước PR3. Object
  revision là content-addressed nên old key vẫn còn nguyên; rollback không
  ghi đè revision nào.
- Row đã backfill vẫn giữ JSON legacy trong rollback window. Mọi version PR2+
  cũng đọc được `bundle_object_key` từ cùng backend object storage.
- Chỉ đổi `OBJECT_STORAGE_BACKEND=local` nếu object đã được copy về
  `APP_DATA_DIRECTORY/object-storage/` với nguyên key. Đổi tên backend đơn
  thuần không chuyển dữ liệu vật lý.

**Rollback về code trước PR2 (giới hạn đã biết):**
- Row tạo/ghi **sau** cutover không còn payload trong cột JSON legacy →
  code trước PR2 không đọc được các row này. Muốn rollback sâu hơn, chạy
  trước một script "re-materialize" (chưa có): đọc bundle từ object
  storage, ghi lại cột JSON legacy.

**Default templates:** luôn khôi phục được từ `templates/*/template.json`
trên disk — seeding chạy lại mỗi startup, nên rollback với default
template chỉ là việc xoá DB row và để seeding import lại.

## Còn nợ (sau rollback window)

- Null toàn bộ cột JSON legacy cho row đã backfill (script riêng, PR kế).
- Drop cột JSON legacy bằng Alembic migration riêng.
- Orphan scanner / GC cho object không còn được row nào reference
  (cutover ghi: DB commit fail sau upload để lại object mồ côi — key là
  content-addressed nên không ai trỏ tới, vô hại cho đến khi GC).
