# Phase 1 — tách module, giữ nguyên hành vi (verify 2026-09-04)

## Đã tách

- Frontend: `app.js` → `js/api.js` (HTTP) + `js/store.js` (state/polling) +
  `js/views.js` (render) + `js/main.js` (bootstrap); `styles.css` →
  `css/base.css` + `css/components.css`. ES modules, không build step.
  Xóa `app.js`, `styles.css` cũ.
- Backend: `proxy.py` (giữ làm entry + lệnh chạy cũ) → `app.py`
  (`create_app`, routes) + `upstream.py` (forward GET) + `config.py`.
- Cho phép serve file tĩnh lồng nhau (`js/`, `css/`) nhưng giam trong
  `frontend/` bằng resolve + relative check (chỉ `.html/.css/.js`).

## Verify (không đổi behavior)

- Syntax: 4/4 JS modules ESM-OK + import/export chéo khớp 100%;
  3/3 file Python AST-OK; `index.html` đúng ref mới, không ref cũ.
- Backend tách: chạy lại `tests/security/test_proxy.py` với code mới →
  **10 passed, 0 failed** (giống hệt baseline).
- Static: `/` + `/js/*.js` + `/css/*.css` → 200; `/../secret`, `/nope.exe` → 404.
- ai tool: không sửa file nào, không gọi POST nào trong quá trình verify.

## Acceptance Phase 1

- [x] Dashboard đọc/hiển thị như cũ (cùng endpoint, cùng shape, cùng polling 10s).
- [x] ai tool không bị sửa.
- [ ] Chờ user mở dashboard mới xác nhận bằng mắt (khuyến nghị Ctrl+Shift+R).
