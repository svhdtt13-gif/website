# Lộ trình triển khai (làm tại repo này, ai tool giữ nguyên)

## Phase 0 — Docs + skeleton đọc ✅ (hiện tại)

- [x] Tài liệu kiến trúc/API/schema/kế hoạch.
- [x] Skeleton proxy read-only + dashboard tĩnh.
- Tiêu chí: mở dashboard thấy cycle/sync/clients; `db.html` gốc không ảnh hưởng.

## Phase 1 — Tách module, giữ nguyên tính năng (2–3 ngày)

- Tách frontend thành file riêng (đã có mẫu `webapp/frontend/`).
- Tách backend proxy thành module theo nhóm API.
- Thêm kiểm tra tự động: mỗi endpoint allowlist phải 200 + shape đúng.
- Tiêu chí: mọi nút đọc hiện tại xanh; ai tool không đổi 1 byte.

## Phase 2 — Ghi an toàn (sau Phase 1 ổn định)

- Mở từng endpoint ghi qua proxy: xác thực riêng (Bearer), log đầy đủ,
  giới hạn theo vai trò (xem/sửa).
- Không bao giờ ghi file ai tool trực tiếp — luôn qua API của nó.
- Tiêu chí: thao tác ghi từ webapp mới = kết quả như bấm trên `db.html`.

## Phase 3 — Độc lập (3–5 ngày)

- SQLite (WAL) thay JSON; sync một chiều từ ai tool.
- Session login thay Basic Auth; realtime SSE thay polling.
- Cloudflared named tunnel (URL cố định) + chạy service.
- Tiêu chí: tắt ai tool vẫn xem được dữ liệu đã sync; bật lại tự bắt kịp.

## Quy tắc an toàn suốt quá trình

1. Mọi test ghi đều làm trên bản sao/tunnel riêng, giờ thấp điểm.
2. Trước mỗi phase: backup ai tool (`/api/cycle/backup`).
3. `db.html` là chuẩn đối chiếu: lệch là dừng, sửa webapp chứ không sửa tool.
