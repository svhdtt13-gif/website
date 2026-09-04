# API Contract — ai tool Flask (`http://127.0.0.1:8080`)

Auth mọi request: `Authorization: Basic base64(admin:DB_WEB_PASS)`.
POST/PUT/PATCH/DELETE cần thêm header `X-DB-Editor: 1`.

Ghi chú cột Webapp: `R` = proxy cho phép (read-only), `W` = phase 2 mới mở.

| Method | Path | Webapp | Mục đích |
|---|---|---|---|
| GET | `/db`, `/db.html` | — | Trang gốc (không dùng ở webapp mới) |
| GET | `/api/master` | R | Đọc master data + schedule |
| POST | `/api/master` | W | Lưu master/schedule |
| GET | `/api/status` | R | Trạng thái tổng |
| GET | `/api/sync_status` | R | Trạng thái auto sync |
| GET | `/api/remote_live?t=&client=` | W | Snapshot remote (gây `row_select` khi kèm client!) |
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

Response lỗi chuẩn: `{"error": "..."}` kèm HTTP 4xx/5xx.
Lưu ý: Flask không bật CORS — trình duyệt gọi trực tiếp sẽ bị chặn,
webapp dùng proxy cùng origin để tránh vấn đề này.
