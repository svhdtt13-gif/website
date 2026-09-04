# Phase 2 Slice 4 — GET API master read

## Scope

Chỉ mở read-only `GET /api/master` theo baseline đã APPROVE CÓ ĐIỀU KIỆN trên Issue #1.
Không mở `POST /api/master`, không đổi write gate, không local file access,
không SQLite/Session/SSE/Worker/Tunnel và không chạm AutoCycle ownership.

## Correction đã áp dụng

- Giữ nguyên `services/master.py::get_master()` -> `clients_master.json`.
- Thêm riêng `services/master.py::get_api_master()` -> `api/master`.
- `READ_HANDLERS["clients_master.json"]` vẫn dùng `get_master()`.
- `READ_HANDLERS["api/master"]` dùng `get_api_master()`.
- Hai route không fallback hoặc dùng chung upstream path.

## Golden contract

Theo `WebAppControl/flask/app_public.py`:

- Method/path: `GET /api/master`, không body/query bắt buộc.
- Basic Auth upstream giữ nguyên.
- HTTP 200, `Content-Type: application/json`.
- Body là canonical master object gồm `clients`, `schedule`, `meta` khi có;
  không lọc/đổi tên/chuẩn hóa fields.
- `clients[]` và `schedule[]` passthrough nguyên trạng.
- Upstream HTTP error/status/body passthrough; unreachable -> `502` JSON error.

## Side effect / invariants

- Website chỉ gọi HTTP API ai tool qua `AiToolRepository.get()`.
- Không đọc `clients_master.json` hoặc file nào trực tiếp từ website.
- Không ghi/xóa/sửa dữ liệu, không sync, remote WebSocket, subprocess, log,
  cycle state, backup hay settings.
- `POST /api/master` vẫn 403; Slice 2 `POST /api/log` Bearer gate giữ nguyên.

## Verification

- `tests/security/test_master_read.py`: **17/17 pass**:
  - API master trả exact body/status/content-type.
  - Legacy `clients_master.json` trả exact body riêng.
  - Hai route hit đúng hai upstream path khác nhau và đều forward Basic Auth.
  - POST/PUT/PATCH/DELETE `/up/api/master` -> 403 JSON, zero upstream write.
  - Upstream HTTP 500 passthrough + proxy alive.
  - Connection refused -> 502 JSON error + proxy alive.
  - Service mapping riêng và không direct file access.
- Regression `tests/security/test_proxy.py`: **10/10 pass**.
- Regression `tests/security/test_write_log.py`: **16/16 pass**.
- Remote Python syntax compile: app/config/master service/test pass.

## Live verification (2026-09-04)

- Gọi trực tiếp `GET /api/master` trên ai tool và qua proxy với cùng Basic Auth:
  status `200`, MIME `application/json`, raw body khớp.
- Snapshot trước/sau giữ nguyên SHA-256 `clients_master.json` và
  `client_database.json`; số lượng clients/schedules và danh sách client IDs
  không đổi.
- AutoCycle PID giữ nguyên và alive; `cycle_state.json` hash không đổi.
- Không gọi POST/PUT/PATCH/DELETE, không chạy sync, không tạo write side effect.
