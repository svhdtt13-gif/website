# Kiến trúc

## Hệ nguồn: dự án `ai tool` (máy local, CHỈ ĐỌC — không sửa)

```
┌──────────────┐   WebSocket   ┌──────────────────┐
│ 360Auto.exe  │◄──────────────│ remote.360auto.net │
│ (qnyh.exe)   │   product 73  │ room e1c51deba...  │
└──────┬───────┘               └──────────────────┘
       │ local process
       ▼
┌──────────────────────────────────────────────┐
│ Flask app_public.py :8080 (Waitress)         │
│ 29 routes: /db, /api/master, /api/cycle/*,   │
│ /api/remote_live, /api/ai_fix/* ...          │
│ Auth: Basic (admin) + header X-DB-Editor     │
│ DB: JSON files (clients_master,              │
│     client_database, settings, cycle_state)  │
└──────┬───────────────────────────┬───────────┘
       │ serve                    │ read/write
       ▼                          ▼
 tools/db.html (1 file,      AutoCycle.ps1 (lịch chạy
 polling 5-15s)              client theo giờ)
```

- Worker nền: `AutoCycle.ps1` (mutex `Local\AutoGhostStory_AutoCycle`),
  `continuous_sync_remote.ps1` (mutex `..._RemoteSync`), watchdog
  `start_onboot.ps1` (Scheduled Task logon).
- Public tạm: cloudflared `trycloudflare.com` → 127.0.0.1:8080.

## Hệ đích: repo `website` này

```
┌──────────┐  HTTP  ┌───────────────────────┐  HTTP (server-side) ┌──────────┐
│ Browser  │◄──────►│ webapp/backend/proxy  │◄───────────────────►│ ai tool  │
│ (static) │ :8090  │ + serve frontend      │  Basic auth trong   │  :8080   │
└──────────┘        └───────────────────────┘  env, allowlist GET │ (nguồn)  │
```

- Phase 0: proxy **chỉ cho GET** trong allowlist (status + master JSON).
  Mọi POST/DELETE bị chặn 403 tại proxy — ai tool không thể bị ghi từ đây.
- Phase 2+: thêm endpoint ghi có xác thực riêng, vẫn gọi sang API ai tool
  (không đụng file trực tiếp).
- Phase 3+: backend riêng + SQLite, ai tool thành nguồn sync một chiều.
