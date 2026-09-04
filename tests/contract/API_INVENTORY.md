# API Inventory Freeze — Phase 0

Nguồn: `WebAppControl/flask/app_public.py` (ai tool, đọc ngày 2026-09-04).
Auth chung: `Authorization: Basic base64(admin:DB_WEB_PASS)`; mọi
POST/PUT/PATCH/DELETE phải thêm `X-DB-Editor: 1` (thiếu -> 403).
Lỗi JSON chuẩn: `{"error": "..."}`; riêng 401 là text + header Basic realm.

Cột Live: `S` = smoke.py gọi live (GET an toàn), `I` = freeze bằng code
inspection (POST/DELETE có side effect, không gọi live).

## Trang + file tĩnh (GET)

| Path | Live | Response / ghi chú |
|---|---|---|
| `/`, `/db`, `/db.html` | S | HTML `db.html` (strip BOM), `text/html; charset=utf-8` |
| `/clients_master.json`, `/client_database.json`, `/cache/cycle.log`, `/cache/change_log.jsonl`, `/cache/action.log`, `/cache/activity_history.jsonl`, `/cache/public_url.txt`, `bg.jpg` | S (2 JSON) | File trong allowlist `PUBLIC_FILES`, else 404 |

## Master / sync (GET = S, POST = I)

| Method Path | Live | Request | Response 200 | Side effect |
|---|---|---|---|---|
| GET `/api/master` | S | — | `{clients[], schedule[], meta}` | không |
| POST `/api/master` | I | `{clients[{client,name,group,selected}], schedule[{group,time,close}]}` | `{status:OK, clients:N, schedule:M}`; 400 khi lịch trùng/thiếu | backup master cũ; ghi master + AUTORELOG; chạy `sync_master.ps1`; 500 nếu sync fail |
| GET `/api/status` | S | — | `{clients, lastUpdated, time(HH:MM:SS)}` | không |
| GET `/api/sync_status` | S | — | `{continuous_running, continuous_pid, interval_sec:10800, status_interval_sec:20, last_sync, extraction_method, total_clients, source}` | không |
| GET `/api/remote_live?t=&client=` | S (không `client`) | query `t`, `client` optional | `{ok, clients[], tasks[], logs[], selectedClientIdx, ...}`; 500 thiếu ps1; 502 payload lỗi; 504 timeout 20s; cache 8s | ⚠️ kèm `client=` sẽ gửi `row_select` lên remote — smoke KHÔNG dùng |
| POST `/api/sync_remote` | I | — | `{status:OK\|ERROR, message, clients, last_sync}` + `returncode` khi lỗi; timeout 45s | chạy `sync_remote_once.ps1` + `sync_master.ps1`, ghi change_log |
| POST `/api/sync_all` | I | — | `{status:OK, message, master, db}` | 2 bước remote→master→db + ghi action/change log |
| POST `/api/sync_continuous/<start\|stop>` | I | — | `{status: started\|already_running\|stopped}`; action lạ → 400 | start `start_sync.ps1` / kill tiến trình sync |

## Cycle / AlwaysRun (GET = S, POST = I)

| Method Path | Live | Request | Response | Side effect |
|---|---|---|---|---|
| GET `/api/cycle_status` | S | — | `{running, status: running\|offline, stopped}` | không |
| POST `/api/cycle/<start\|stop>` | I | — | start `{status, running}` 200 (503 nếu chưa chạy); stop ngược lại; action lạ 400 | start xóa stop-flag + chạy AutoCycle `-Background`; stop ghi flag + kill worker |
| POST `/api/alwaysrun/<open\|stop>` | I | `{clients[]}` hoặc `{all:true}` (lọc fixed+selected) | `{status:OK, action, clients, disabled}`; 400 khi rỗng/sai | open chạy `remote_toggle_rows.ps1` (120s); stop chạy `FullAutoActuator.ps1` từng client (75s); quản lý `alwaysrun_override.json` |
| POST `/api/clear_history` | I | — | `{status:OK, cleared[], message}` | truncate 4 log + reset cycle_state + xóa results/*.json |
| GET `/api/cycle/backup` | S | — | `{backups[]}` | không |
| POST `/api/cycle/backup` | I | `{label?}` (json/form) | `{status:OK, backup, manifest}` | tạo zip trong `cache/cycle_backups` |
| POST `/api/cycle/backup/<name>/restore` | I | — (tên phải `^[A-Za-z0-9_\-\.]+$`) | `{status:OK, restored}`; 400/404 | ghi đè file runtime + scripts |
| DELETE `/api/cycle/backup/<name>` | I | — | `{status:OK, deleted}`; 400/404 | xóa zip |
| POST `/api/cycle/fix` | I | — | `{status:OK, before{...}, actions[], public_url, browser_opened, telegram_sent}` | validate master/db, clear stop flag, chạy sync scripts, set restart flag, restart tunnel, mở browser, gửi Telegram |
| GET/POST `/api/cycle/url?open=&notify=` | I | — | `{status:OK, public_url, browser_opened, telegram_sent}`; 500 nếu tunnel lỗi | restart cloudflared, mở browser, Telegram |
| GET `/api/cycle/status` | S | — | `{checked_at, cycle_running, cycle_pid, sync_running, sync_pid, 360auto, qnyh, stop_flag, last_log[3], state, manual_overrides[]}` | không |

## Settings / AI fix / log

| Method Path | Live | Request | Response | Side effect |
|---|---|---|---|---|
| GET `/api/settings` | S | — | settings JSON (xem DATA_SCHEMAS) | không |
| POST `/api/settings` | I | partial settings JSON | `{status:OK, settings}` | ghi `cache/settings.json` |
| POST `/api/settings/test_telegram` | I | — | `{status:OK, sent:true}` hoặc 400 | gửi Telegram test |
| POST `/api/settings/open_browser` | I | `{url?}` | `{status, opened, url}` | mở browser phía server |
| POST `/api/ai_fix` | I | `{kind: cycle\|web\|userimport, text?}` (text ≤2000, bắt buộc nếu userimport) | `{status:OK, kind, project:"ai tool", file, command}`; 400 | ghi `cache/ai_fix_requests/*.json` + activity |
| GET `/api/ai_fix/status` | S | — | `{watcher{auto,pid,updated_at,cli_ok,dry_run,pending,last_action,last_model}, models[], pending[], recent_done[5], recent_failed[5]}` | không |
| DELETE `/api/ai_fix/answers` | I | — | `{status:OK, deleted[], count}` | chỉ xóa `.done/.failed/.out.log/.prompt.txt`, giữ pending/processing |
| POST `/api/ai_fix/watcher` | I | `{action: start\|stop}` | `{status:OK, action}`; 400 | start/stop tiến trình watcher |
| POST `/api/log` | I | `{action, clients, schedule}` | `{status:logged}` | ghi action.log + activity |

## Quy tắc freeze

- Mọi thay đổi response shape/method/status/side-effect so với bảng trên
  phải được ghi nhận + phê duyệt trước khi merge vào `website`.
- Smoke test chỉ bao phủ cột `S`. Cột `I` giữ nguyên bằng inspection lại
  khi ai tool đổi code (so diff `app_public.py`).
