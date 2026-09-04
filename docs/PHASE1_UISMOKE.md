# Phase 1 — UI smoke bằng trình duyệt thật (2026-09-04)

Chạy proxy Phase-1 (code trên `main`) trỏ về ai-tool live, mở dashboard
bằng Chrome headless, hard refresh.

## Kết quả

- [x] 4 card hiển thị: Cycle / Auto Sync / 360Auto-qnyh / AI fix
      (vd: `Auto Sync: Running`, `360Auto 1 • qnyh 5`, `AI fix: Tự động BẬT • chờ 0`).
- [x] Bảng Clients: đủ 26 dòng, đúng id/tên/nhóm/trạng thái như master
      (fixed, HAMI, MNHI, MIE, NA, none).
- [x] Banner: ẩn khi không có manual override (đúng logic).
- [x] Console: sạch lỗi module import / CORS / runtime exception.
      (Còn 1 lỗi duy nhất `favicon.ico 404` → đã thêm favicon inline
      `data:,` để triệt, không đổi hành vi.)
- [x] Network: đúng 4 GET (`/up/api/cycle/status`, `/up/api/sync_status`,
      `/up/clients_master.json`, `/up/api/ai_fix/status`), lặp ~10s,
      toàn 200, **không có POST/PUT/PATCH/DELETE**.
- [x] Static mới (`/`, `/js/*.js`, `/css/*.css`) → 200;
      `/../secret`, `/nope.exe` → 404.

## Kết luận

Dashboard Phase-1 hiển thị đúng dữ liệu ai tool, console sạch,
network đúng contract. Đủ điều kiện đóng Phase 1 theo `docs/PHASE1_REVIEW.md`.
