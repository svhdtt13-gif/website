# Phase 1 — Review trực tiếp

Review theo commit/cấu trúc hiện tại của `webapp/frontend` + `webapp/backend` và biên bản `docs/PHASE1_VERIFY.md`.

## Kết luận

**APPROVE PHASE 1 — có điều kiện xác nhận UI bằng mắt trước khi đóng phase.**

### Đã kiểm tra

- Frontend đã tách đúng trách nhiệm: `api.js` (HTTP), `store.js` (state/load), `views.js` (DOM), `main.js` (bootstrap/polling).
- Backend đã tách đúng trách nhiệm: `proxy.py` là entry tương thích, `app.py` chứa Flask routes, `upstream.py` forward GET, `config.py` cấu hình.
- `main.js` vẫn polling 10 giây; `store.js` dùng đúng 4 endpoint read-only hiện tại.
- `views.js` không thực hiện network call; dữ liệu đi qua store trước khi render.
- Static file nested `js/`/`css/` được giới hạn trong `frontend/`, giới hạn extension và có resolve/relative check.
- `/up/*` vẫn chỉ GET; POST/PUT/PATCH/DELETE bị chặn 403.
- Security regression vẫn **10/10 pass**, phù hợp baseline Phase 0.
- Không thấy code path mới ghi vào `ai tool`.

## Điểm cần chốt trước khi đóng Phase 1

1. **UI smoke bằng mắt:** mở dashboard mới, `Ctrl+Shift+R`, xác nhận 4 card + bảng Clients + banner lỗi/manual override hiển thị đúng như `tools/db.html`/baseline.
2. **Browser console:** không có lỗi module import, 404 static, CORS hoặc runtime exception.
3. **Network:** xác nhận đúng 4 GET endpoint và chu kỳ ~10s; không có POST/PUT/PATCH/DELETE.
4. Nếu muốn chứng minh mạnh hơn, thêm automated browser smoke ở phase sau; không bắt buộc để chặn Phase 1 hiện tại.

## Lưu ý cho Phase 2

Không mở rộng write endpoint trực tiếp trong các module frontend hiện tại. Phase 2 nên bắt đầu bằng abstraction rõ ràng:

`route → service → repository → ai tool`

và viết contract test từng write endpoint một. Tuyệt đối không cho website ghi trực tiếp file JSON/state của `ai tool`.

## Quyết định

Nếu UI smoke + browser console/network đều sạch: **đóng Phase 1 và chuyển Phase 2.**

Giữ nguyên nguyên tắc: `tools/db.html`/`ai tool` là golden reference; nếu phát hiện lệch behavior hoặc dữ liệu thì sửa website, không sửa hệ nguồn.