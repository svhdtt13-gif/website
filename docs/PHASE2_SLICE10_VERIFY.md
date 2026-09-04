# Phase 2 Slice 10 — guarded remote client selector

## Scope

Mở đúng một webapp selector route:
`GET /api/remote_live?t=<t>&client=<client_id>`.
Canonical `GET /api/remote_live` vẫn là read-only snapshot read. Không mở sync,
toggle, cycle, alwaysrun, Telegram, browser, tunnel, AI-fix hoặc remote action nào khác.

## Route and query boundary

`app.py` xử lý `request.query_string` trước handler dispatch. Có query trên
`api/remote_live` phải qua strict raw-byte parser; parser không dùng form decoding.
Chỉ chấp nhận chính xác một `t` và một `client`, reject duplicate/encoded duplicate
keys, query thừa, malformed percent encoding, invalid UTF-8, `+`, empty values và
control characters. `t` là 1–32 chữ số; `client` là non-empty text tối đa 200 ký tự.

Query lỗi trả `400` generic với zero upstream call. Query hợp lệ nhưng thiếu hoặc sai
website Bearer trả `401` với `WWW-Authenticate: Bearer` và zero upstream call.

## Selector transaction

`get_remote_live_selector()` giữ `_REMOTE_SELECTOR_LOCK` xuyên suốt:

1. Fresh-read và validate snapshot từ upstream dưới Basic Auth.
2. Xác nhận target tồn tại duy nhất sau normalize `0:`.
3. Unknown/ambiguous target trả `409`, không gửi selector upstream.
4. Nếu target đã selected, trả fresh public projection, không tạo `row_select`.
5. Nếu target khác, gửi canonical query `t=<t>&client=<target_id>` và validate fresh
   response phải selected đúng target trước khi trả.

Mọi upstream error, network/timeout, malformed JSON hoặc schema/type/range/consistency
failure trả `502` generic `{"error":"remote live snapshot unavailable"}`. Không raw echo.

## Safety invariants

- Public response chỉ expose exact 7-key projection của Slice 9.
- Selector upstream call chỉ là GET; không có POST/PUT/PATCH/DELETE.
- Selector không truy cập file, subprocess, WebSocket, sync, cycle, toggle, notifier,
  browser, tunnel, AI-fix hoặc mutation ngoài selection.
- POST/PUT/PATCH/DELETE và subpath của remote live vẫn trả `403`.
- Selector requests serialize dưới process-local lock.
- Canonical upstream query được re-encode từ parsed values, không passthrough raw query.

## Automated verification

Executed on branch `phase2-slice10-remote-selector`:

- `python tests/security/test_remote_live_selector.py`: **32/32 pass**.
- `python tests/security/test_remote_live_read.py`: **49/49 pass**.
- `python tests/security/test_proxy.py`: **10/10 pass**.
- `python tests/security/test_write_log.py`: **16/16 pass**.
- `python tests/security/test_backup_read.py`: **17/17 pass**.
- `python tests/security/test_backup_write.py`: **28/28 pass**.
- `python tests/security/test_master_read.py`: **16/16 pass**.
- `python tests/security/test_master_write.py`: **54/54 pass**.
- `python tests/security/test_settings_read.py`: **24/24 pass**.
- `python tests/security/test_settings_write.py`: **43/43 pass**.
- Combined security assertions: **289 passed, 0 failed**.
- `python -m py_compile webapp/backend/app.py webapp/backend/services/remote_live.py tests/security/test_remote_live_selector.py tests/security/test_remote_live_read.py`: pass.

## Live verification

Not run yet. Before opening the PR, run the guarded selector against the configured real
ai tool with a reversible alternate client and record:

- canonical snapshot before and after;
- selected target before, requested target, and selected target after;
- exact selector call and absence of sync/toggle/action calls;
- unchanged master/database/config/CSV/cycle/alwaysrun/activity/change/action state;
- AutoCycle remains alive;
- rollback restores the original selected client only when the initial selected target
  was non-null and an alternate client exists; otherwise record an explicit skip.

Do not merge until this live evidence and review are complete.
