# Baseline Phase 0 — freeze contract

- Ngày: 2026-09-04 (giờ máy ai tool, UTC+7).
- Mục tiêu: `http://127.0.0.1:8080` (ai tool Flask, không sửa gì).
- Lệnh: `python tests/contract/smoke.py` (chỉ GET, xem `README.md`).
- Kết quả: **16 passed, 0 failed, exit 0**.

```
PASS: /db html
PASS: /api/master [200 keys=clients,schedule]
PASS: /api/status [200 keys=clients,lastUpdated,time]
PASS: /api/sync_status [200 keys=continuous_running,total_clients]
PASS: /api/cycle_status [200 keys=running,status]
PASS: /api/cycle/status [200 keys=cycle_running,manual_overrides,qnyh]
PASS: manual_overrides is list
PASS: /api/cycle/backup [200 keys=backups]
PASS: /api/settings [200 keys=tunnel_port]
PASS: settings has no secrets leaked
PASS: /api/ai_fix/status [200 keys=watcher,pending,models]
PASS: ai_fix models non-empty
PASS: /clients_master.json [200 keys=clients]
PASS: /client_database.json [200 keys=clients,schedule]
PASS: /api/remote_live [200 ok]
PASS: no-auth -> 401
SUMMARY: 16 passed, 0 failed
```

Mọi thay đổi sau này ở `website` phải chạy lại smoke này và vẫn xanh;
nếu đỏ, sửa webapp — cấm sửa ai tool để test pass.
