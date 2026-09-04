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
- `python tests/security/test_remote_live_selector_preconditions.py`: **6/6 pass**.
  - `selectedClientIdx=null` returns `SKIPPED/BLOCKED BY PRECONDITION` and emits zero selector action.
  - no alternate client returns `SKIPPED/BLOCKED BY PRECONDITION` and emits zero selector action.
  - a reversible snapshot is the only branch marked `READY` to emit a selector action.
- `python tests/security/test_remote_live_read.py`: **49/49 pass**.
- `python tests/security/test_proxy.py`: **10/10 pass**.
- `python tests/security/test_write_log.py`: **16/16 pass**.
- `python tests/security/test_backup_read.py`: **17/17 pass**.
- `python tests/security/test_backup_write.py`: **28/28 pass**.
- `python tests/security/test_master_read.py`: **16/16 pass**.
- `python tests/security/test_master_write.py`: **54/54 pass**.
- `python tests/security/test_settings_read.py`: **24/24 pass**.
- `python tests/security/test_settings_write.py`: **43/43 pass**.
- Combined security assertions: **295 passed, 0 failed**.
- `python -m py_compile webapp/backend/app.py webapp/backend/services/remote_live.py tests/security/test_remote_live_selector.py tests/security/test_remote_live_read.py tests/security/test_remote_live_selector_preconditions.py`: pass.

## Live verification

Executed against the configured real ai tool on `127.0.0.1:8080`, through the branch
proxy on `127.0.0.1:8091`, using a temporary Bearer token and no source changes.
The run completed without a concurrent `remote_sync` entry:

- initial selection: `idx 19`, `client_56`;
- alternate: `idx 20`, `client_46` (scheduled group, not fixed);
- guarded selector returned `200` and selected `idx 20`;
- canonical read confirmed selected `idx 20`;
- guarded rollback returned `200` and restored `idx 19` / `client_56`;
- final canonical upstream read confirmed selected `idx 19`;
- AutoCycle process count remained `1` before and after;
- no selector test call used POST/PUT/PATCH/DELETE or any sync/toggle/action endpoint.

### Semantic diff matrix

The following matrix was captured immediately before and after the selector + rollback
transaction. JSON files were compared both by SHA-256 and recursively by semantic JSON
paths. JSONL/text files were compared by SHA-256, line count, and newly appended entries.
No secret values were printed.

| Protected state | Raw change | Semantic diff / new entries | Attribution |
|---|---:|---|---|
| `tools/client_database.json` | no | no paths | unchanged |
| `tools/clients_master.json` | no | no paths | unchanged |
| `tools/config.json` | no | no paths | unchanged |
| `tools/cache/cycle_state.json` | no | no paths | unchanged |
| `tools/cache/manual_override.json` | no | no paths | unchanged |
| `tools/remote_session.json` | no | no paths | unchanged |
| `tools/remote_rooms.json` | no | no paths | unchanged |
| `tools/cache/activity_history.jsonl` | no | line count unchanged; new entries `[]` | no `remote_sync` or selector event |
| `tools/cache/change_log.jsonl` | no | line count unchanged; new entries `[]` | no `remote_sync` or selector event |
| `tools/cache/cycle.log` | no | line count unchanged; new lines `[]` | unchanged |
| AutoCycle process | no | count `1 -> 1` | scheduler remained alive |

This controlled semantic run isolates the selector transaction and closes the earlier
continuous-sync attribution gap. The earlier exploratory run did overlap background
`remote_sync`; its non-selector changes are not used as acceptance evidence.

Rollback was executed only because the initial selected identity was non-null and an
alternate scheduled client existed. The precondition test separately proves that
`selectedClientIdx=null` and “no alternate client” are explicit
`SKIPPED/BLOCKED BY PRECONDITION` branches with zero selector action.

Do not merge until this evidence and the requested review are complete.
