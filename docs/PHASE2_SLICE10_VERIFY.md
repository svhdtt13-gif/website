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

Executed against the configured real ai tool on `127.0.0.1:8080`, through the branch
proxy on `127.0.0.1:8091`, using a temporary Bearer token and no source changes:

- initial selection: `idx 0`, `client_14`;
- alternate: `idx 20`, `client_46` (scheduled group, not fixed);
- guarded selector returned `200` and selected `idx 20`;
- canonical read confirmed selected `idx 20`;
- guarded rollback returned `200` and restored `idx 0` / `client_14`;
- final canonical upstream read confirmed selected `idx 0`;
- AutoCycle process count remained `1` before and after;
- no selector test call used POST/PUT/PATCH/DELETE or any sync/toggle/action endpoint.

The first live run took long enough to overlap the already-running continuous sync. The
state-file fingerprint was therefore not byte-identical: `activity_history.jsonl` and
`change_log.jsonl` gained periodic `remote_sync` entries, and the associated sync-owned
state files changed during that background activity. The observed entries were
`event=sync_remote`, not selector activity. A second run holding
`Local\\AutoGhostStory_RemoteSync` was not possible because the continuous sync process
owns that mutex; the process was not stopped or modified. Thus selector rollback passed,
but byte-level “all state files unchanged” is not claimed as isolated evidence in this
environment. Do not merge until a reviewer accepts this limitation or repeats the live
check during a controlled maintenance window.

Do not merge until live evidence and review are complete.
