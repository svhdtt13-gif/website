# Webapp skeleton (read-only)

Dashboard đọc dữ liệu từ ai tool qua proxy cùng origin.
Không có chức năng ghi — mọi POST/DELETE bị chặn 403 tại proxy.

## Chạy

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

Mở `http://127.0.0.1:8090`.

## An toàn

- Proxy bind `127.0.0.1` — chỉ máy local mở được. Muốn public: đi qua
  tunnel + thêm xác thực (Phase 2).
- Credentials ai tool nằm trong biến môi trường phía server, không bao giờ
  ra trình duyệt. Không commit file chứa secrets.
- Muốn trỏ sang ai tool khác: đổi `AI_TOOL_API_BASE`.
