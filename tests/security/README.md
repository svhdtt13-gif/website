# Phase 0 — Security & contract tests cho proxy read-only

Trả lời comment review issue #1: chứng minh proxy **không có đường ghi** vào
ai tool, giữ contract, chặn traversal, và lỗi ổn định khi upstream chết.

## Chạy

```bat
python tests\security\test_proxy.py
```

- Không cần ai tool đang chạy: test tự dựng upstream stub local + chạy bản
  `webapp/backend/proxy.py` thật trên port tạm, xong tự dọn.
- Chỉ dùng stdlib Python. Exit 0 = xanh hết, 1 = có đỏ.
- Không đụng ai tool, không cần credentials thật.

## Phạm vi (khớp acceptance trong issue #1)

1. **Read-only allowlist**: GET các path allowlist → 200 + JSON đúng shape +
   proxy chuyển tiếp đúng `Authorization: Basic` sang upstream.
2. **Method-block**: POST/PUT/PATCH/DELETE vào mọi path allowlist → 403,
   upstream không nhận bất kỳ request ghi nào.
3. **API contract**: giữ HTTP status, JSON body/error shape (`{"error":...}`),
   `Content-Type: application/json`; upstream chết → 502 JSON ổn định,
   proxy không crash.
4. **Path traversal**: `../`, `..\`, encoded (`%2e`, `%5c`, `%00`), double
   slash → chỉ 403/404; upstream không bao giờ thấy path ngoài allowlist.
5. **Invariant**: toàn bộ test không ghi vào ai tool (không dùng ai tool thật).
