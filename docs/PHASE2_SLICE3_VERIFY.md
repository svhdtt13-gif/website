# Phase 2 Slice 3 — GET cycle backup metadata

Phạm vi duy nhất của Slice 3 là read-only `GET /api/cycle/backup`.
Không mở `POST /api/cycle/backup`, restore, delete hoặc bất kỳ endpoint write mới nào.

## Implementation

- `READ_ONLY_ALLOWLIST` thêm đúng `api/cycle/backup`.
- `services/backup.py::get_cycle_backups()` gọi duy nhất
  `repositories/aitool.py::get("api/cycle/backup")`.
- `app.py::READ_HANDLERS` dispatch qua service; website không đọc trực tiếp
  `tools/cache/cycle_backups` và không có fallback file access.
- Slice 2 `POST /up/api/log` + Bearer write gate không thay đổi.
- Không SQLite/Session/SSE/Worker/Tunnel; không restart/stop AutoCycle.

## Golden contract

Theo `WebAppControl/flask/app_public.py` của ai tool:

- `GET /api/cycle/backup`, không body/query bắt buộc.
- Basic Auth upstream giữ nguyên.
- HTTP 200, `application/json`, body `{"backups": [...]}`.
- Item có tối thiểu `name`, `size`, `mtime`; manifest hợp lệ có thể thêm
  `label`, `created_at`, `files`, `script_files`.
- ZIP được liệt kê giảm dần theo filesystem `mtime`; lỗi manifest từng ZIP
  bị bỏ qua như golden implementation.
- Upstream HTTP error/status/body được passthrough; unreachable -> 502 JSON error.

## Verification

- `tests/security/test_backup_read.py`: **15 checks pass** trên stub local:
  boot, GET shape/content-type, Basic Auth, POST/PUT/PATCH/DELETE 403 + zero
  upstream write, traversal variants, Slice 2 write gate, upstream error/body,
  proxy survival.
- Remote Python syntax compile: app/config/service/test pass.
- Live (2026-09-04): gọi qua proxy Slice 3 và đối chiếu direct ai tool với cùng
  Basic Auth; status `200`, MIME `application/json`, body **3684 bytes** khớp
  byte-for-byte. Không có backup tạo đồng thời nên không phát sinh delta cần
  nới assertion.
- Live pre/post: AutoCycle PID **21268** giữ nguyên và alive.
  `cycle_state.json` SHA-256 giữ nguyên:
  `37B3D96B07FD54C6FFF516EF3C601A6F9F4A8357AFAEB5125CC52B9626D613F4`.
  `client_database.json` SHA-256 giữ nguyên:
  `36FC284CD606E56BCC0D42C06B37211CC7B0EF4BAA78F6913C5F01206A8B66FC`.
  Không có request write, không restart/stop AutoCycle, không đổi scheduler
  ownership.
- Website không chạm trực tiếp thư mục backup; chỉ upstream ai tool đọc nó.
