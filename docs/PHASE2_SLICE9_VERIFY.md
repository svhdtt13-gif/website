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
`_REMOTE_LIVE_LOCK`. Golden upstream success phải có chính xác 9 key:

`ok`, `fetchedAt`, `clientCount`, `selectedClientIdx`, `clients`, `tasks`, `logs`,
`room`, `roster`.

`room` phải là exact non-empty string; `roster` phải là object hoặc null. Public
response chỉ project top-level 7 key:

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

- `tests/security/test_remote_live_read.py`: **49/49 pass**:
  - route-level query guard với zero upstream call;
  - Basic Auth forwarding và exact 9-key upstream envelope;
  - exact 7-key public projection, room/roster stripping;
  - positive allowlist cho clients/resources/tasks/logs;
  - exact types, ranges, indexes và client-count consistency;
  - malformed/error/unreachable/invalid room-roster responses -> generic `502`;
  - concurrent reads serialized;
  - non-GET/subpath blocking và no direct file/subprocess/WebSocket access.
- Regression Slice 1–8 trên cùng head: tất cả pass.
- Python syntax compile: pass.

## API contract update

`docs/API_CONTRACT.md` tách canonical `GET /api/remote_live` read-only khỏi golden
selector `GET /api/remote_live?t=&client=` write-risk. Upstream envelope 9 key và
public webapp projection 7 key được ghi rõ; selector không thuộc webapp allowlist.

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
