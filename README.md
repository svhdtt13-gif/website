# Website App (tách từ dự án `ai tool`)

Web app quản lý Auto Ghost Story, triển khai **riêng hoàn toàn** tại repo này.
Dự án gốc `ai tool` (máy local, `tools/db.html` + Flask port 8080)
**không bị sửa đổi**.

## Nguyên tắc vàng

1. **READ-ONLY với ai tool trừ các slice write đã được duyệt**: webapp chỉ đọc
   dữ liệu qua HTTP API; write endpoint phải đi qua repository, không ghi file
   trực tiếp bên ai tool.
2. **Không commit secrets**: token/bot key/pass chỉ nằm trong biến môi trường
   (xem `webapp/README.md`). File mẫu trong repo chỉ chứa placeholder.
3. Mọi thay đổi hành vi (ghi DB, điều khiển cycle) là **Phase 2**, từng endpoint
   một, có contract test và rollback riêng.
4. Action chưa đạt safety gate được ghi là **deferred/NO-GO**; không mở route chỉ
   để hoàn thành đủ danh sách endpoint.

## Cấu trúc (Phase 2: route → service → repository)

```
website/
├── README.md                 # file này
├── docs/                     # architecture / contract / close-out / plan
├── tests/
│   ├── contract/             # historical golden GET smoke + API inventory
│   └── security/             # proxy, write gate và slice security tests
└── webapp/
    ├── README.md             # cách chạy + current write/deferred boundary
    ├── backend/              # proxy + serve frontend
    │   ├── proxy.py          # entry mỏng (giữ lệnh chạy cũ)
    │   ├── app.py            # Flask app + routes (create_app)
    │   ├── config.py
    │   ├── repositories/     # tầng data-access (ai tool qua HTTP)
    │   └── services/         # domain service layers
    └── frontend/             # dashboard tĩnh, ES modules (không build)
```

Luồng đọc/ghi đã duyệt: `route (app.py) → service/* → repositories/aitool.py → ai tool`.
Chi tiết current route inventory và deferred registry nằm ở
`docs/PHASE2_CLOSEOUT.md`.

## Current Phase 2 State

Eight write/action boundaries are implemented and guarded:

- `POST /up/api/log`
- `POST /up/api/cycle/backup`
- `POST /up/api/settings`
- guarded display-name-only CAS `POST /up/api/master`
- guarded `DELETE /up/api/cycle/backup/<name>`
- guarded `POST /up/api/settings/test_telegram`
- guarded `POST /up/api/settings/open_browser`
- guarded `POST /up/api/ai_fix`

The four Bundle 2 sync/answers/watcher actions and all Bundle 3/4 high-risk
actions remain intentionally blocked. No route or generic dispatch exists for
them. `tests/contract/API_INVENTORY.md` remains the historical Phase 0 golden
inventory; it is not the current webapp capability registry.

## Chạy thử skeleton

```bat
cd webapp\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set AI_TOOL_API_BASE=http://127.0.0.1:8080
set AI_TOOL_USER=admin
set AI_TOOL_PASS=<mat-khau-db-web>
set WEBAPP_WRITE_TOKEN=<random-secret-outside-repo>
python proxy.py
```

Mở `http://127.0.0.1:8090` — dashboard đọc trạng thái cycle/sync + bảng clients
 từ ai tool, proxy giữ credentials phía server.

## Roadmap

- Phase 0: docs + read-only skeleton và contract/security baseline xanh.
- Phase 1: tách module, giữ nguyên tính năng.
- Phase 2: **close-out**. Safe writes được triển khai; unsafe writes/runtime
  controls được audit và deferred có chủ đích. Xem `docs/PHASE2_CLOSEOUT.md`.
- Phase 3: SQLite (WAL), một chiều sync, session, SSE và named tunnel; **chưa
  được bắt đầu trước khi close-out docs PR được approve và merge**.
- Chi tiết: `docs/DEPLOY_PLAN.md`.
