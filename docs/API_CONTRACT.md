# API Contract — ai tool Flask (`http://127.0.0.1:8080`)

Auth mọi request: `Authorization: Basic base64(admin:DB_WEB_PASS)`.
POST/PUT/PATCH/DELETE cần thêm header `X-DB-Editor: 1`.

Ghi chú cột Webapp: `R` = proxy cho phép (read-only). `W` = phase 2 mới mở.
`D` = scope đã xác định nhưng route vẫn deferred/blocked vì thiếu safety gate.
`R*` = guarded read route có thể yêu cầu website Bearer và có remote selection side
effect giới hạn trong selector contract. `—` = golden/internal path chưa được webapp proxy.

| Method | Path | Webapp | Mục đích |
|---|---|---|---|
| GET | `/db`, `/db.html` | — | Trang gốc (không dùng ở webapp mới) |
| GET | `/api/master` | R | Đọc master data + schedule |
| POST | `/api/master` | W | Lưu master/schedule |
| GET | `/api/status` | R | Trạng thái tổng |
| GET | `/api/sync_status` | R | Trạng thái auto sync |
| GET | `/api/remote_live` | R | Canonical remote snapshot read, không selector, không đổi remote selection |
| GET | `/api/remote_live?t=&client=` | R* | Guarded remote client selector; chỉ route này có thể gửi `row_select` |
| POST | `/api/sync_remote` | D | Deferred: thiếu cross-process `RemoteWs` exclusion |
| POST | `/api/sync_all` | D | Deferred: thiếu cross-process `RemoteWs` exclusion |
| POST | `/api/sync_continuous/<start\|stop>` | W | Bật/tắt sync |
| GET | `/api/cycle_status` | R | Cycle chạy hay không |
| POST | `/api/cycle/<start\|stop>` | W | Điều khiển cycle |
| POST | `/api/alwaysrun/<open\|stop>` | W | Điều khiển nhóm fixed |
| POST | `/api/clear_history` | W | Xóa log |
| GET/POST | `/api/cycle/backup` | W | List/tạo backup |
| POST | `/api/cycle/backup/<name>/restore` | W | Khôi phục backup |
| DELETE | `/api/cycle/backup/<name>` | W | Xóa đúng một backup đã xác nhận |
| POST | `/api/cycle/fix` | W | Refresh + tạo URL mới + notifier |
| GET/POST | `/api/settings` | W | Cấu hình |
| POST | `/api/settings/test_telegram` | W | Test Telegram |
| POST | `/api/settings/open_browser` | W | Mở browser phía server |
| POST | `/api/ai_fix` | W | Tạo lệnh AI fix `{kind,text?}` |
| GET | `/api/ai_fix/status` | R | Hàng chờ + watcher + models |
| DELETE | `/api/ai_fix/answers` | D | Deferred: thiếu watcher ownership/exclusion |
| POST | `/api/ai_fix/watcher` | D | Deferred: thiếu watcher ownership/exclusion |
| GET/POST | `/api/cycle/url` | W | Tạo lại public URL |
| GET | `/api/cycle/status` | R | Chi tiết cycle + `manual_overrides` + qnyh |
| POST | `/api/log` | W | Ghi log |
| GET | `/clients_master.json`, `/client_database.json`, `/cache/*` | R | File tĩnh trong allowlist |

## Remote live read contract

Canonical request là chính xác `GET /api/remote_live` với query string rỗng.
Service gọi upstream `GET /api/remote_live` dưới `_REMOTE_LIVE_LOCK`; response public
chỉ có 7 key an toàn. Query có trên canonical route được xử lý tại route boundary,
không được forward raw và không được dùng để bypass selector contract.

Golden upstream success envelope phải có chính xác 9 key:

`ok`, `fetchedAt`, `clientCount`, `selectedClientIdx`, `clients`, `tasks`, `logs`,
`room`, `roster`.

`room` là exact non-empty string; `roster` là object hoặc null. Webapp project public
response thành chính xác 7 key:

`ok`, `fetchedAt`, `clientCount`, `selectedClientIdx`, `clients`, `tasks`, `logs`.

`room`, `roster`, session/token và transport/internal metadata không được expose.
Nested client/resource/task/log fields dùng positive allowlists và exact type/range/
consistency validation theo `docs/PHASE2_SLICE9_VERIFY.md`.

## Guarded selector contract

Request là `GET /api/remote_live?t=<decimal>&client=<client_id>` và cần website
`Authorization: Bearer <WEBAPP_WRITE_TOKEN>`. Bearer gate được kiểm tra sau strict
query parsing và trước mọi upstream call cho selector. Upstream vẫn dùng Basic Auth.

Raw query phải có chính xác hai key `t` và `client`, mỗi key đúng một lần; key/value
được percent-decode strict UTF-8, không chấp nhận malformed `%`, `+`, encoded duplicate
keys hoặc query thừa. `t` là 1–32 chữ số; `client` là non-empty text tối đa 200 ký tự,
không có whitespace-only/control character. Query lỗi trả `400` generic và zero upstream.

Selector giữ `_REMOTE_SELECTOR_LOCK` xuyên suốt fresh read và selection validation.
Target phải tồn tại duy nhất trong fresh snapshot sau normalize `0:`; unknown/ambiguous
trả `409` và không gửi selector upstream. Nếu target đã selected thì trả fresh public
snapshot và không tạo selector side effect. Nếu khác, upstream query được canonical
re-encode thành `t=<t>&client=<target_id>`; response sau selection phải xác nhận đúng
target trước khi trả public snapshot. Mọi upstream/network/timeout/schema failure trả
`502` generic `{"error":"remote live snapshot unavailable"}`.

Selector không gọi sync, toggle, cycle, alwaysrun, Telegram, browser, tunnel, AI-fix,
không ghi file và không mở WebSocket. POST/PUT/PATCH/DELETE và mọi subpath vẫn bị block.

## Guarded backup deletion contract

Request là chính xác `DELETE /api/cycle/backup/<name>` và cần website
`Authorization: Bearer <WEBAPP_WRITE_TOKEN>`. Đây là dedicated route riêng; không mở
generic prefix DELETE hoặc generic dynamic write dispatch. Upstream dùng Basic Auth,
`X-DB-Editor: 1`, canonical encoded name và body rỗng.

Golden DELETE không đọc query/body. Webapp giữ compatibility nhưng không dùng hoặc
forward query/body; upstream URL chỉ có canonical backup name. Raw path được decode đúng
một lần strict UTF-8, phải là đúng một segment, không slash/backslash/NUL/control,
malformed percent, residual percent, `.`/`..`, hoặc ký tự ngoài `^[A-Za-z0-9_.-]+$`.

Service fresh-reads `/api/cycle/backup`, xác nhận name tồn tại đúng một lần dưới
`_BACKUP_LOCK`, rồi mới gửi đúng một DELETE. Unknown/stale/ambiguous name trả
`409 {"error":"backup target unavailable"}` với zero upstream DELETE. GET listing,
POST create, restore và mọi path/method khác không đổi.

Success public contract là `200 application/json` với chính xác
`{"status":"OK","deleted":"<canonical_name>"}`. Invalid raw path/name là `400`
generic với zero upstream DELETE. Mọi upstream HTTP `500`, repository/runtime exception,
timeout, network error, malformed/non-JSON/invalid success response hoặc unexpected
status map deterministically thành `502 application/json` với chính xác
`{"error":"backup deletion unavailable"}`; không raw echo và không retry.

Không mở cycle/scheduler/fixed-client/remote selection action, không restore, không ghi
file/database trực tiếp và không đổi scheduler ownership.

## Bundle 1 settings actions contract

Bundle 1 mở đúng hai dedicated POST routes:

- `POST /api/settings/test_telegram`
- `POST /api/settings/open_browser`

Website route là `/up/...`; Bearer authentication được kiểm tra trước mọi upstream
call. Query string trên cả hai action bị reject tại route boundary bằng `400`
`{"error":"query parameters not allowed"}` và zero upstream. GET/PUT/PATCH/DELETE
và mọi subpath khác bị block `403`.

`test_telegram` không nhận credential/message từ client và không forward body/query.
Upstream repository gửi một POST body rỗng với Basic Auth + `X-DB-Editor: 1`. Success
là `200 {"status":"OK","sent":true}`. Golden known configuration/send failure
là `400 {"error":"Telegram token/chat id chưa cấu hình hoặc gửi thất bại"}`.
Mọi runtime/network/timeout/malformed upstream response map thành
`502 {"error":"telegram test unavailable"}` và không retry, echo hoặc expose secrets.

`open_browser` nhận JSON object optional. Empty body hoặc missing/falsey `url` dùng
`http://127.0.0.1:8080/db`; malformed/non-object JSON trả deterministic `400`
`{"error":"invalid browser request"}` và zero upstream. URL truthy phải là absolute
`http`/`https`, có hostname, không userinfo/control/CRLF/NUL và tối đa 2048 UTF-8 bytes.
Repository chỉ gửi typed `{"url":"<validated_url>"}` tới fixed upstream action.
Success/failure giữ `200` shape lần lượt
`{"status":"OK","opened":true,"url":"<url>"}` hoặc
`{"status":"error","opened":false,"url":"<url>"}`. Runtime/network/timeout/
malformed upstream response map thành `502 {"error":"browser open unavailable"}`
không echo hoặc retry.

Hai action không ghi settings/state/activity/change log và không gọi sync, cycle,
alwaysrun, tunnel hoặc action còn lại. Không direct file access; typed repository
methods là `test_telegram()` và `open_browser(url)`.

## Bundle 2 AI create and deferred writes

Bundle 2 chỉ enable `POST /up/api/ai_fix`; website Bearer auth is checked before
contacting ai tool, and query strings/methods/schema failures make zero upstream calls.
The request accepts exactly `cycle`, `web`, or `userimport`; userimport text is trimmed,
non-empty, control-character-free, and at most 2,000 characters. Fixed commands are
owned by the service and the queue file is created through a typed repository method.

The golden queue filename has only second precision. The repository therefore holds
`Local\\AutoGhostStory_AiFixCreate` during each create and spaces upstream create calls
by at least 1.1 seconds. The returned success envelope must have exactly
`status`, `kind`, `project`, `file`, and `command`; `file` must be a safe basename of
`ai_fix_<requested-kind>_YYYYMMDD_HHMMSS.json`, and the command must match the frozen
command. Runtime, network, timeout, HTTP, and malformed responses map to
`502 {"error":"ai fix unavailable"}` with no raw detail.

`DELETE /up/api/ai_fix/answers`, `POST /up/api/ai_fix/watcher`,
`POST /up/api/sync_remote`, and `POST /up/api/sync_all` remain deferred and are not
write-dispatched. They return the existing JSON `403` without contacting ai tool.
Answers deletion requires atomic watcher ownership and an archive/preimage; watcher
control requires heartbeat/PID identity and atomic queue exclusion; sync requires
cross-process exclusion with `Local\\AutoGhostStory_RemoteWs` and the continuous-sync
worker. Website-local status preflight or lock alone is insufficient.

## Response errors

Response lỗi chuẩn: `{"error": "..."}` kèm HTTP 4xx/5xx.
Lưu ý: Flask không bật CORS — trình duyệt gọi trực tiếp sẽ bị chặn,
webapp dùng proxy cùng origin để tránh vấn đề này.
