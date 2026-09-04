# Phase 2 Slice 9 — canonical remote live read

## Scope

Chỉ mở `GET /api/remote_live` không query qua
`route -> service -> repository -> ai tool`. Selector URL có thể gây `row_select`
trong golden app và không được proxy. Không mở sync, toggle, cycle, alwaysrun,
Telegram, browser, tunnel, AI-fix hoặc remote action.

## Route boundary

`app.py` chặn mọi query string/query parameter cho `api/remote_live` trước khi
lookup hoặc gọi `READ_HANDLERS`. `?client=...`, `?t=...` và query khác trả generic
`400` với zero upstream call. POST/PUT/PATCH/DELETE và subpath đều bị block.

## Upstream and public schema

Service gọi đúng upstream `GET /api/remote_live` với Basic Auth dưới
`_REMOTE_LIVE_LOCK`. Upstream được phép có metadata `room`/`roster`, nhưng public
response chỉ project top-level:

`ok`, `fetchedAt`, `clientCount`, `selectedClientIdx`, `clients`, `tasks`, `logs`.

Positive entry allowlists:

- `clients[]`: `idx`, `id`, `name`, `state`, `cap`, `capAge`, `uiStatus`, `level`,
  `resources`, `checked`.
- `resources`: `nPhieu`, `bac`, `vang`, `ngoc`.
- `tasks[]`: `idx`, `name`, `status`, `checked`.
- `logs[]`: `text`, `color`.

Extra/missing fields ở bất kỳ tầng nào fail closed. `ok` phải là exact `true`;
`fetchedAt` là non-empty ISO-8601 string; `clientCount` là exact non-negative
integer và bằng `len(clients)`; selected index là null hoặc exact non-negative
client index; all indexes unique; `capAge` là null hoặc exact non-negative integer;
text/boolean fields không coercion.

Mọi upstream `4xx/5xx`, timeout, network error, malformed/non-object, invalid
schema/type/range/consistency đều trả generic `502`:
`{"error":"remote live snapshot unavailable"}`. Không raw echo và không retry.

## Runtime boundary

Website không ghi file, không chạy subprocess, không mở WebSocket và không gọi
remote action. Golden remote mutex vẫn là authority cho remote read. Không có
master/DB/config/schedule/override/log/cycle mutation.

## Automated verification

- `tests/security/test_remote_live_read.py`: **45/45 pass**:
  - route-level query guard với zero upstream call;
  - Basic Auth forwarding và safe top-level projection;
  - positive allowlist cho clients/resources/tasks/logs;
  - exact types, ranges, indexes và client-count consistency;
  - all upstream failures -> generic `502`;
  - concurrent reads serialized;
  - non-GET/subpath blocking và no direct file/subprocess/WebSocket access.
- Regression Slice 1–8 trên cùng head:
  - `test_proxy.py`: **10/10 pass**.
  - `test_write_log.py`: **16/16 pass**.
  - `test_backup_read.py`: **17/17 pass**.
  - `test_backup_write.py`: **28/28 pass**.
  - `test_master_read.py`: **16/16 pass**.
  - `test_settings_read.py`: **24/24 pass**.
  - `test_settings_write.py`: **43/43 pass**.
- Python syntax compile: pass.

## API contract update

`docs/API_CONTRACT.md` tách canonical `GET /api/remote_live` read-only khỏi golden
selector `GET /api/remote_live?t=&client=` write-risk. Selector vẫn không thuộc
webapp Slice 9 allowlist.

## Live verification

Verified on the current branch head with the real ai tool:

- canonical webapp GET without query returned `200` with exact safe public keys;
- canonical golden snapshot was read before and after the webapp call;
- `selectedClientIdx` and selected remote client identity were unchanged;
- `clients_master.json`, `client_database.json`, `config.json`, CSV, cycle state,
  alwaysrun override, activity/change/action logs and secret fingerprints were
  unchanged;
- no selector, `row_select`, sync, toggle or action was called;
- AutoCycle remained alive.
