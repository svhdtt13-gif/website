# Phase 2 Slice 7 — POST settings safe partial update

## Scope

Chỉ mở `POST /api/settings` qua route -> service -> repository -> ai tool.
GET `/api/settings` redaction Slice 5 giữ nguyên. Không mở credential workflow,
`test_telegram`, `open_browser` hoặc runtime endpoint khác; không SQLite/Session/
SSE/Worker/Tunnel và không chạm AutoCycle ownership.

## Shared contract and security

- GET và POST dùng chung `_project_public_settings()` trong
  `services/settings.py` cho exact type/range và public projection.
- Website chỉ nhận non-empty JSON object với 4 safe fields:
  `tunnel_port` (`int` `1..65535`), `auto_restart_tunnel`, `auto_telegram`,
  `auto_open_browser` (exact JSON boolean).
- Unknown, nested, Telegram/effective credential, `default_browser`,
  `cloudflared_path` và secret-like fields reject toàn request `400`, không echo,
  zero upstream write.
- Missing fields là partial update semantics; không synthesize default/null.
- Chỉ `application/json` và `application/json; charset=utf-8` hợp lệ; text/plain,
  empty/malformed/non-object/empty object -> generic `400`.
- Upstream request là canonical JSON safe fields, Basic Auth và repository-owned
  `X-DB-Editor: 1`; Bearer gate chạy trước handler.
- Success response phải là valid `status=OK` + settings object; website project
  lại settings thành public 4-key allowlist. Corrupt allowlisted value,
  malformed/non-object/missing success contract -> generic `502`, không raw echo,
  không retry.
- Upstream 4xx/5xx/network body được settings-specific sanitize; không lộ raw
  settings hoặc secret.

## Side effects and runtime boundaries

Ai tool merge safe fields vào `cache/settings.json`, rewrite file và append một
activity line + một change line. Website không local file access, không log body,
không invoke Telegram, browser, tunnel, subprocess, sync, remote WebSocket hoặc
AutoCycle.

Process-local settings write lock serialize website writes; đây không phải
cross-process/cross-machine lock. Nếu phát hiện external concurrent writer khi
rollback, không tự overwrite.

## Rollback

Live rollback gửi lại chỉ safe field ban đầu qua cùng endpoint sau khi re-read và
chứng minh safe state hiện tại đúng test delta, không có unexpected concurrent
change. Không restore nguyên settings/log file và không xóa audit lines.
Code rollback là revert hoặc bỏ riêng `api/settings` khỏi write allowlist/handler.

## Automated verification

- `tests/security/test_settings_write.py`: **33/33 pass**:
  - shared projector/validator cho GET/POST;
  - valid partial update, canonical safe request và owned marker;
  - both accepted JSON content types; text/plain rejected;
  - empty/malformed/non-object/unknown/secret/nested/type/range rejection;
  - corrupt allowlisted success values, malformed/non-object/missing response
    contract -> generic 502, no raw echo, exactly one upstream attempt;
  - upstream error sanitization, no helper writes, no direct file access.
- Regression:
  - `test_proxy.py`: **10/10 pass**.
  - `test_write_log.py`: **16/16 pass**.
  - `test_backup_read.py`: **17/17 pass**.
  - `test_backup_write.py`: **28/28 pass**.
  - `test_master_read.py`: **16/16 pass**.
  - `test_settings_read.py`: **24/24 pass**.
- Python syntax compile for app/config/settings service and tests: pass.

## Live verification (2026-09-04)

Một transaction write + guarded rollback riêng đã chạy trên ai tool thật:

- `POST /up/api/settings` safe partial update: `200`, public response redacted.
- Re-read xác nhận đúng test delta và không có unexpected concurrent change.
- Rollback bằng chỉ safe field ban đầu: `200`; safe settings state restored.
- Secret-field fingerprints giữ nguyên, không in hoặc ghi giá trị secret.
- Activity delta đúng `+2` lines và change-log delta đúng `+2` lines tổng cộng
  cho write + rollback; không xóa log để ép hash.
- `client_database.json`, `cycle_state.json`, `clients_master.json` không đổi;
  AutoCycle PID vẫn alive.
- Không gọi Telegram/browser/tunnel/sync/remote action hoặc endpoint write khác.
