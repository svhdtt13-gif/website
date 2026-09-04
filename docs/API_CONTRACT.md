# API Contract — ai tool Flask (`http://127.0.0.1:8080`)

Auth mọi request: `Authorization: Basic base64(admin:DB_WEB_PASS)`.
POST/PUT/PATCH/DELETE cần thêm header `X-DB-Editor: 1`.

Ghi chú cột Webapp: `R` = proxy cho phép (read-only). `W` = phase 2 mới mở.
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
| POST | `/api/sync_remote` | W | Sync 1 lần từ remote |
| POST | `/api/sync_all` | W | Sync toàn bộ |
| POST | `/api/sync_continuous/<start\|stop>` | W | Bật/tắt sync |
| GET | `/api/cycle_status` | R | Cycle chạy hay không |
| POST | `/api/cycle/<start\|stop>` | W | Điều khiển cycle |
| POST | `/api/alwaysrun/<open\|stop>` | W | Điều khiển nhóm fixed |
| POST | `/api/clear_history` | W | Xóa log |
| GET/POST | `/api/cycle/backup` | W | List/tạo backup |
| POST | `/api/cycle/backup/<name>/restore` | W | Khôi phục backup |
| DELETE | `/api/cycle/backup/<name>` | W | Xóa backup |
| POST | `/api/cycle/fix` | W | Refresh + tạo URL mới + notifier |
| GET/POST | `/api/settings` | W | Cấu hình |
| POST | `/api/settings/test_telegram` | W | Test Telegram |
| POST | `/api/settings/open_browser` | W | Mở browser phía server |
| POST | `/api/ai_fix` | W | Tạo lệnh AI fix `{kind,text?}` |
| GET | `/api/ai_fix/status` | R | Hàng chờ + watcher + models |
| DELETE | `/api/ai_fix/answers` | W | Xóa câu trả lời cũ |
| POST | `/api/ai_fix/watcher` | W | Bật/tắt watcher |
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

Response lỗi chuẩn: `{"error": "..."}` kèm HTTP 4xx/5xx.
Lưu ý: Flask không bật CORS — trình duyệt gọi trực tiếp sẽ bị chặn,
webapp dùng proxy cùng origin để tránh vấn đề này.
