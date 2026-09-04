# Phase 0 — Contract tests (read-only)

Mục tiêu: đóng băng hành vi API hiện tại của ai tool. Mọi thay đổi ở `website`
phải chứng minh không phá contract trong `API_INVENTORY.md`.

## Chạy smoke test

```bat
set AI_TOOL_API_BASE=http://127.0.0.1:8080
set AI_TOOL_USER=admin
set AI_TOOL_PASS=<mat-khau-db-web>
python tests\contract\smoke.py
```

- Exit 0 = toàn bộ check xanh. Exit 1 = có check đỏ (xem dòng FAIL).
- Script **chỉ dùng GET** — không gọi bất kỳ POST/PUT/PATCH/DELETE nào nên
  không thể đổi trạng thái cycle/sync/remote/file. An toàn chạy bất cứ lúc nào.
- Ngoại lệ duy nhất: `GET /api/remote_live` (không kèm `client`) đọc snapshot
  remote, timeout 60s. Tuyệt đối không tự thêm `?client=` khi test tay —
  tham số đó gửi `row_select` làm đổi selection trên remote thật.

## Cái gì KHÔNG test live (và vì sao)

Các endpoint POST/DELETE đều có side effect thật (restart worker, ghi file,
kill process, gửi Telegram, tạo tunnel mới...). Contract của chúng được
freeze bằng **code inspection** trong `API_INVENTORY.md`, không gọi live.
Muốn test chúng: dùng bản sao ai tool + giờ thấp điểm + backup trước.
