# Security baseline — Phase 0 (trả lời review issue #1)

- Ngày: 2026-09-04. Lệnh: `python tests/security/test_proxy.py`
  (tự dựng upstream stub + proxy thật, không dùng ai tool).
- Kết quả: **10 passed, 0 failed, exit 0**.

```
PASS: proxy boots
PASS: GET /up/api/cycle/status [200 shape+ctype]
PASS: GET /up/clients_master.json [200 shape+ctype]
PASS: auth forwarded to upstream
PASS: write methods blocked on all allowlist paths (28 calls)
PASS: upstream saw zero write requests
PASS: no traversal reached upstream outside allowlist
PASS: traversal variants contained
PASS: upstream down -> 502 json-error
PASS: proxy survives upstream outage
SUMMARY: 10 passed, 0 failed
```

## Phát hiện trong lúc test (đã sửa cùng đợt)

- Lần chạy 1: 8 pass / 29 fail — Flask trả **405 HTML** thay vì 403 JSON
  cho POST/PUT/PATCH/DELETE vì route `/up/` chỉ khai báo GET.
- Sửa `webapp/backend/proxy.py`: khai báo đủ 5 method, chặn ghi bằng
  `403 {"error": ...}` trong handler; tăng timeout case upstream-down.
- Chạy lại: 10/10 xanh. Không có code path ghi vào ai tool.
