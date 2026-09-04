# Phase 2 Slice 5 — GET API settings redaction

## Scope

Chỉ mở read-only `GET /api/settings` với positive allowlist. Không proxy raw
settings, không mở `POST /api/settings`, `test_telegram` hoặc `open_browser`.
Không direct file access, không đổi write gate, không SQLite/Session/SSE/Worker/
Tunnel và không chạm AutoCycle.

## Approved corrections

- Missing source keys remain absent; website không tạo default/null/placeholder.
- Upstream HTTP 200 nhưng malformed JSON hoặc JSON non-object -> `502` generic JSON.
- Upstream 4xx/5xx status được giữ, nhưng raw error body luôn được thay bằng
  `{"error":"settings upstream response unavailable"}`.
- Public key set luôn là subset của positive allowlist; unknown/nested fields bị
  drop và canary secret leakage được test trực tiếp.
- Golden implementation cho phép `default_browser` là arbitrary string/path;
  field này bị loại khỏi public contract Slice 5.
- `tunnel_port` phải là integer trong miền `1..65535`; invalid value -> generic
  `502`, không echo giá trị.

## Field contract

### Public-safe allowlist

Chỉ các key sau được trả về khi key thực sự tồn tại upstream:

```text
tunnel_port: integer, 1..65535
auto_restart_tunnel: boolean
auto_telegram: boolean
auto_open_browser: boolean
```

Giá trị phải đúng JSON type tương ứng. Nếu allowlisted value sai type hoặc nằm
ngoài miền hợp lệ, response fail-closed `502`.

### Always omitted

- `default_browser` (golden cho phép arbitrary string/path, không expose)
- `telegram_bot_token`
- `telegram_chat_id`
- `effective_telegram_bot_token`
- `effective_telegram_chat_id`
- `cloudflared_path`
- Unknown fields, nested fields, path/credential/environment-derived values

Redaction dùng `omit`, không dùng masked placeholder.

## Route/service/repository

- Website route: `GET /up/api/settings`.
- Service: `services/settings.py::get_settings`.
- Repository: `AiToolRepository.get("api/settings")` với Basic Auth hiện có.
- Service parse/validate source body và serialize lại allowlist; không raw
  passthrough.
- Mọi method write trên `/up/api/settings` -> `403`, zero upstream write.

## Verification

- `tests/security/test_settings_read.py`: **24/24 pass**:
  - exact public allowlist response và Basic Auth/path forwarding;
  - missing key không tự tạo key;
  - `default_browser` canary không lọt vì field bị omit;
  - canary trong allowlisted `tunnel_port` -> generic `502`, không echo;
  - port `0` và `65536` -> generic `502`;
  - malformed JSON/non-object 200 -> generic `502`;
  - upstream 401/503 -> status giữ nguyên nhưng body generic;
  - canary token/password/chat ID ở top-level, unknown/nested fields và error
    body không lọt ra public response;
  - POST/PUT/PATCH/DELETE settings và settings write helpers -> `403`, zero
    upstream write;
  - upstream unreachable -> generic `502`, proxy vẫn sống;
  - service không direct file access và không raw return.
- Regression Slice 1–4:
  - `tests/security/test_proxy.py`: **10/10 pass**.
  - `tests/security/test_write_log.py`: **16/16 pass**.
  - `tests/security/test_backup_read.py`: **17/17 pass**.
  - `tests/security/test_master_read.py`: **16/16 pass**.
- Remote Python syntax compile: app/config/settings service/test pass.

## Live verification (2026-09-04)

- Direct ai tool `GET /api/settings`: `200`; branch proxy
  `GET /up/api/settings`: `200`.
- Raw upstream schema có 8 keys; public response có đúng 4 keys:
  `tunnel_port`, `auto_restart_tunnel`, `auto_telegram`, `auto_open_browser`.
- Không in hoặc ghi giá trị secret; public response không có sensitive key.
- Snapshot trước/sau giữ nguyên: `settings.json`, `client_database.json`,
  `cycle_state.json`, `clients_master.json`, số clients/schedules và client IDs.
- AutoCycle PID vẫn alive; không gọi method write, không sync, không restart/stop.

## Approval gate

Implementation đã chạy test và live evidence trên branch này. PR đang mở để
review; không merge hoặc mở endpoint khác trong PR này.
