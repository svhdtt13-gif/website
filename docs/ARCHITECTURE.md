# Kiến trúc

## Hệ nguồn: dự án `ai tool` (authoritative API + JSON source)

```
┌──────────────┐   WebSocket   ┌──────────────────┐
│ 360Auto.exe  │◄──────────────│ remote.360auto.net │
│ (qnyh.exe)   │   product 73  │ room e1c51deba...  │
└──────┬───────┘               └──────────────────┘
       │ local process
       ▼
┌──────────────────────────────────────────────┐
│ Flask app_public.py :8080 (Waitress)         │
│ 29 golden routes: /db, /api/cycle/*,         │
│ /api/remote_live, /api/ai_fix/* ...          │
│ Auth: Basic (admin) + header X-DB-Editor     │
│ DB/source: JSON files and owned workers      │
└──────┬───────────────────────────┬───────────┘
       │ serve                    │ read/write API
       ▼                          ▼
 tools/db.html (1 file,      AutoCycle.ps1 (lịch chạy
 polling 5-15s)              client theo giờ)
```

`ai tool` remains the golden behavior and source of truth. The website may call
only the explicitly approved upstream write APIs; it never reads or writes the
ai tool filesystem directly. The historical Phase 0 route list is preserved in
`tests/contract/API_INVENTORY.md`.

- Worker nền: `AutoCycle.ps1` (mutex `Local\AutoGhostStory_AutoCycle`),
  `continuous_sync_remote.ps1` (mutex `..._RemoteSync`), watchdog
  `start_onboot.ps1` (ownership-sensitive; not controlled by the website).
- Public tạm: cloudflared `trycloudflare.com` → 127.0.0.1:8080.

## Hệ đích: repo `website` này

```
┌──────────┐  HTTP  ┌─────────────────────────────┐  HTTP (server-side) ┌──────────┐
│ Browser  │◄──────►│ webapp/backend/proxy         │◄───────────────────►│ ai tool  │
│ (static) │ :8090  │ frontend + guarded dispatch  │  Basic auth in env  │  :8080   │
└──────────┘        └─────────────────────────────┘                     └──────────┘
```

- Read paths use the route → service → repository → ai tool boundary.
- The eight approved write/action paths use the same boundary with a website
  Bearer gate and typed repository methods.
- Bundle 2 sync/answers/watcher and all Bundle 3/4 actions remain absent from
  write dispatch and are intentionally deferred/NO-GO.
- Phase 3 may add SQLite behind the repository abstraction; it must not rewrite
  the approved Phase 2 route contracts or take scheduler ownership.
