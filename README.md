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

## Cấu trúc

```
website/
├── README.md                 # file này
├── docs/
│   ├── ARCHITECTURE.md       # kiến trúc nguồn + kiến trúc đích
│   ├── API_CONTRACT.md       # toàn bộ API của ai tool (method/auth/mục đích)
│   ├── DATA_SCHEMAS.md       # hình dạng JSON dữ liệu
│   └── DEPLOY_PLAN.md        # lộ trình 4 phase
└── webapp/
    ├── README.md             # cách chạy skeleton
    ├── backend/              # proxy read-only + serve frontend
    │   ├── proxy.py
    │   ├── config.py
    │   └── requirements.txt
    └── frontend/             # dashboard tĩnh (HTML/CSS/JS thuần)
        ├── index.html
        ├── styles.css
        └── app.js
```

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

- Phase 0 (hiện tại): docs + skeleton đọc. `db.html` gốc phải luôn ổn định.
- Phase 1: tách module, giữ nguyên tính năng.
- Phase 2: ghi an toàn (qua API ai tool, có xác thực riêng).
- Phase 3: DB SQLite + realtime + auth + tunnel cố định.
- Chi tiết: `docs/DEPLOY_PLAN.md`.
