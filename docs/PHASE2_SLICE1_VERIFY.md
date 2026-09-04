# Phase 2 lát cắt 1 — abstraction READ `route → service → repository` (verify)

## Đã làm (chưa đụng write endpoint nào)

- Mới: `webapp/backend/repositories/aitool.py` (`AiToolRepository.get()` +
  `UpstreamError`) — logic forward nguyên văn từ `upstream.py` cũ.
- Mới: `webapp/backend/services/{cycle,sync,master,aifix}.py` — hàm đọc
  1:1 với 7 path allowlist, passthrough giữ nguyên contract.
- `webapp/backend/app.py` — route gọi service qua bảng `READ_HANDLERS`;
  path allowlisted nhưng chưa có service vẫn 403 (phòng thủ sâu).
- Xóa `upstream.py` cũ. `proxy.py` còn là entry mỏng, lệnh chạy không đổi.
- README cập nhật cấu trúc.

## Verify (2026-09-04)

- AST: 6/6 file backend đạt.
- `tests/security/test_proxy.py` với code mới: **10 passed, 0 failed**
  (allowlist, method-block 28 calls, traversal, 502 ổn định — y baseline).
- Contract smoke với ai-tool live (chỉ GET): **16 passed, 0 failed**.
- ai tool: không sửa file, không gọi POST trong toàn bộ verify.

## Chưa làm (đúng luật issue)

- Không service ghi nào; không SQLite; không session/SSE/worker/tunnel.
- Lát cắt tiếp theo đề xuất: write endpoint đầu tiên (`POST /api/log` —
  ít rủi ro nhất) với contract test trước/sau + rollback.
