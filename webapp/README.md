# Webapp skeleton + Slice 2 write endpoint

Dashboard doc du lieu tu ai tool qua proxy cung origin.
Write chi mo duy nhat `POST /up/api/log` trong Slice 2 va phai qua Bearer
write token rieng cua website; cac write khac van bi chan 403.

## Chay

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

Mo `http://127.0.0.1:8090`.

## Write boundary

- `POST /up/api/log` yeu cau `Authorization: Bearer <WEBAPP_WRITE_TOKEN>`.
- Thieu/sai token -> 401 JSON va khong co request nao toi ai tool.
- `WEBAPP_WRITE_TOKEN` chi lay tu environment; khong hard-code, khong commit.
- Bearer gate nay la bao ve toi thieu cho Phase 2, chua phai Session/role system.
- Repository tu tao Basic Auth + `X-DB-Editor: 1` khi goi ai tool; client khong
  the override header nay.

## An toan

- Proxy bind `127.0.0.1` — chi may local mo duoc.
- Credentials ai tool va write token nam trong bien moi truong phia server,
  khong bao gio ra trinh duyet. Khong commit file chua secrets.
- Muon tro sang ai tool khac: doi `AI_TOOL_API_BASE`.
