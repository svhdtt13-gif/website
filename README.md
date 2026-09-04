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

## Cấu trúc (Phase 2: route → service → repository)

```
website/
├── README.md                 # file này
├── docs/                     # ARCHITECTURE / API_CONTRACT / DATA_SCHEMAS / DEPLOY_PLAN
├── tests/
│   ├── contract/             # smoke GET + API_INVENTORY + BASELINE
│   └── security/             # proxy, write gate và slice security tests
└── webapp/
    ├── README.md             # cách chạy skeleton + write token
    ├── backend/              # proxy + serve frontend
    │   ├── proxy.py          # entry mỏng (giữ lệnh chạy cũ)
    │   ├── app.py            # Flask app + routes (create_app)
    │   ├── config.py
    │   ├── repositories/     # tầng data-access (ai tool qua HTTP)
    │   │   └── aitool.py     # AiToolRepository.get()/post() + UpstreamError
    │   └── services/          # cycle/sync/master/aifix/backup/log
    └── frontend/             # dashboard tĩnh, ES modules (không build)
        ├── index.html
        ├── css/base.css + components.css
        └── js/api.js, store.js, views.js, main.js
```

Luồng đọc/ghi đã duyệt: `route (app.py) → service/* → repositories/aitool.py → ai tool`.
Hiện chỉ có write `POST /api/log`, được bảo vệ bởi `WEBAPP_WRITE_TOKEN`; các
write khác chưa migrate.

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

- Phase 0 (xong): docs + skeleton đọc + contract/security baseline xanh.
  `db.html` gốc phải luôn ổn định.
- Phase 1 (xong): tách module, giữ nguyên tính năng (`docs/PHASE1_VERIFY.md`,
  `docs/PHASE1_UISMOKE.md`).
- Phase 2 (đang làm): repository/service abstraction; Slice 2 đã mở có kiểm soát
  `POST /api/log`; Slice 3 thêm read `GET /api/cycle/backup`.
- Phase 3: DB SQLite + realtime + auth + tunnel cố định.
- Chi tiết: `docs/DEPLOY_PLAN.md`.
