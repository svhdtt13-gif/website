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

- `tests/security/test_master_read.py`: **16/16 pass**:
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
  status `200`, MIME `application/json`, body **3633 bytes** khớp byte-for-byte.
- Snapshot `clients_master.json` trước/sau giống hệt:
  SHA-256 `ED1BC62AB9228220F2A731C5A1131F798F32369516CA1603052C5E7841E9820B`,
  `clients=26`, `schedules=4`, IDs:
  `client_14,client_15,client_3,client_1,client_13,client_8,client_6,client_5,client_2,client_7,client_31,client_32,client_33,client_34,client_35,client_52,client_53,client_54,client_55,client_56,client_46,client_47,client_48,client_49,client_50,client_51`.
- `client_database.json` SHA-256 trước/sau:
  `36FC284CD606E56BCC0D42C06B37211CC7B0EF4BAA78F6913C5F01206A8B66FC`.
- `cycle_state.json` SHA-256 trước/sau:
  `37B3D96B07FD54C6FFF516EF3C601A6F9F4A8357AFAEB5125CC52B9626D613F4`.
- AutoCycle PID `21268` giữ nguyên và alive.
- Không gọi POST/PUT/PATCH/DELETE, không chạy sync, không tạo write side effect.
