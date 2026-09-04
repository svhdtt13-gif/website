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

## Field contract

### Public-safe allowlist

Chỉ các key sau được trả về khi key thực sự tồn tại upstream:

```text
default_browser: string
tunnel_port: integer
auto_restart_tunnel: boolean
auto_telegram: boolean
auto_open_browser: boolean
```

Giá trị phải đúng JSON type tương ứng. Nếu allowlisted value sai type, response
fail-closed `502` thay vì trả dữ liệu có thể chứa nested secret.

### Always omitted

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

- `tests/security/test_settings_read.py` bao phủ:
  - exact public allowlist response và Basic Auth/path forwarding;
  - missing key không tự tạo key;
  - malformed JSON/non-object 200 -> generic `502`;
  - upstream 401/503 -> status giữ nguyên nhưng body generic;
  - canary token/password/chat ID ở top-level, unknown/nested fields và error
    body không lọt ra public response;
  - POST/PUT/PATCH/DELETE settings và settings write helpers -> `403`, zero
    upstream write;
  - upstream unreachable -> generic `502`, proxy vẫn sống;
  - service không direct file access và không raw return.
- Regression Slice 1–4 phải pass.
- Remote Python syntax compile phải pass cho app/config/settings service/tests.
- Live verification phải chụp schema/key set nhưng không ghi giá trị secret; public
  body chỉ chứa allowlist. `settings.json`, `client_database.json`,
  `cycle_state.json`, client IDs/schedules và AutoCycle PID phải giữ nguyên.

## Approval gate

Chỉ mở PR sau khi test và live evidence đạt, rồi chờ review trước merge.
