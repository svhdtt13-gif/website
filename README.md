# Website App (tách từ dự án `ai tool`)

Web app quản lý Auto Ghost Story, triển khai **riêng hoàn toàn** tại repo này.
Dự án gốc `ai tool` (máy local, `tools/db.html` + Flask port 8080) **không bị sửa đổi**.

## Nguyên tắc vàng

1. **READ-ONLY với ai tool**: webapp chỉ đọc dữ liệu qua HTTP API của ai tool.
   Không ghi, không xóa, không sửa bất kỳ file nào bên ai tool.
2. **Không commit secrets**: token/bot key/pass chỉ nằm trong biến môi trường
   (xem `webapp/README.md`). File mẫu trong repo chỉ chứa placeholder.
3. Mọi thay đổi hành vi (ghi DB, điều khiển cycle) là **Phase 2**, sau khi app
   đọc ổn định và `db.html` gốc vẫn chạy bình thường.

## Cấu trúc (Phase 2: route → service → repository, mới chỉ READ)

```
website/
├── README.md                 # file này
├── docs/                     # ARCHITECTURE / API_CONTRACT / DATA_SCHEMAS / DEPLOY_PLAN
├── tests/
│   ├── contract/             # smoke GET + API_INVENTORY + BASELINE
│   └── security/             # test proxy read-only + SECURITY_BASELINE
└── webapp/
    ├── README.md             # cách chạy skeleton
    ├── backend/              # proxy read-only + serve frontend
    │   ├── proxy.py          # entry mỏng (giữ lệnh chạy cũ)
    │   ├── app.py            # Flask app + routes (create_app)
    │   ├── config.py
    │   ├── requirements.txt
    │   ├── repositories/     # tầng data-access (hiện tại: ai tool qua HTTP)
    │   │   └── aitool.py     # AiToolRepository.get() + UpstreamError
    │   └── services/         # tầng nghiệp vụ đọc (cycle/sync/master/aifix)
    └── frontend/             # dashboard tĩnh, ES modules (không build)
        ├── index.html
        ├── css/base.css + components.css
        └── js/api.js, store.js, views.js, main.js
```

Luồng đọc: `route (app.py) → service/* → repositories/aitool.py → ai tool`.
Chưa có service ghi nào — write endpoint là lát cắt sau, từng cái một,
mỗi cái có contract test + rollback riêng.

## Chạy thử skeleton (2 phút)

```bat
cd webapp\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set AI_TOOL_API_BASE=http://127.0.0.1:8080
set AI_TOOL_USER=admin
set AI_TOOL_PASS=<mat-khau-db-web>
python proxy.py
```

Mở `http://127.0.0.1:8090` — dashboard đọc trạng thái cycle/sync + bảng clients
từ ai tool, không cần đăng nhập trình duyệt (proxy giữ credentials phía server).

## Roadmap

- Phase 0 (xong): docs + skeleton đọc + contract/security baseline xanh.
  `db.html` gốc phải luôn ổn định.
- Phase 1 (xong): tách module, giữ nguyên tính năng (`docs/PHASE1_VERIFY.md`,
  `docs/PHASE1_UISMOKE.md`).
- Phase 2 (đang làm): abstraction service/repository cho READ trước;
  write endpoint từng cái một, có test + rollback.
- Phase 3: DB SQLite + realtime + auth + tunnel cố định.
- Chi tiết: `docs/DEPLOY_PLAN.md`.
