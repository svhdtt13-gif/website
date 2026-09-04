# Phase 2 Slice 8 — guarded master display-name CAS write

## Scope

Chỉ mở `POST /api/master` cho per-field rename của client hiện có qua
`route -> service -> repository -> ai tool`. Không mở full master/schedule write,
client add/remove, group/selected write, restore/delete, sync/action controls,
remote WebSocket, cycle, alwaysrun, Telegram, browser, tunnel hoặc AI-fix.

## Write intent and CAS contract

Request body chỉ có dạng:

```json
{
  "changes": [
    {"client": "client_1", "expected_name": "Tên cũ", "name": "Tên mới"}
  ]
}
```

- `changes` là non-empty array; mỗi item có đúng `client`, `expected_name`, `name`.
- Không duplicate client; mọi field là exact JSON string.
- `name` và `expected_name` dài `1..200` Unicode characters, không whitespace-only,
  control character hoặc CR/LF; không coercion, trim hay fallback.
- `client` phải là client hiện có; unknown client là generic `400`.
- Full `collectMaster()` không phải write authority và bị reject.

## Transaction and canonicalization

- `_MASTER_WRITE_LOCK` bao trọn transaction duy nhất:
  `GET fresh /api/master -> validate/CAS compare -> canonicalize -> POST upstream`.
- Current `name` phải bằng `expected_name`; mismatch trả `409`, zero upstream POST.
- Canonical full master được dựng từ fresh snapshot. Chỉ các name CAS thành công
  được thay đổi; client khác và toàn bộ `group`, `selected`, `schedule` lấy từ fresh
  snapshot. Không forward `slot`, `expected_name` hoặc raw client body.
- Concurrent rename khác client giữ được cả hai thay đổi; cùng client từ cùng
  expected value chỉ một request thành công.

## Auth and response

- Website Bearer gate chạy trước handler và trả `401` + `WWW-Authenticate: Bearer`.
- Repository dùng Basic Auth và tự ghi đè `X-DB-Editor: 1`.
- Success upstream phải có `status: "OK"`, `clients` và `schedule` là exact JSON
  integer, không phải bool, và `>= 0`.
- Public response chỉ `{status, clients, schedule}`.
- Malformed/non-object/missing/invalid success, upstream error hoặc unreachable
  đều trả generic sanitized `502`, không raw echo và không retry.

## Side effects and rollback

Golden `POST /api/master` được phép thực hiện các side effect vốn có: master
mirror/backup, `sync_master.ps1`, derived database/config/CSV và append-only logs.
Website không ghi file trực tiếp và không gọi riêng các runtime/action endpoint.

Live rollback chỉ rename một client non-fixed bằng CAS từ tên gốc sang tên tạm,
đọc lại xác nhận, rồi CAS từ tên tạm về tên gốc. Verification phải chứng minh:

- master semantic state, derived DB/config/CSV và name client được restore;
- secret fingerprints không đổi;
- cycle state và alwaysrun override không đổi;
- activity/change/action logs chỉ append đúng delta, không xóa;
- AutoCycle vẫn alive.

## Automated verification

- `tests/security/test_master_write.py`: **51/51 pass**:
  - Bearer gate, owned marker và zero upstream khi unauthenticated;
  - exact MIME/schema/name/unknown/duplicate/full-payload rejection;
  - fresh snapshot canonical payload và no raw `slot`/CAS fields forward;
  - stale CAS `409`, zero upstream POST;
  - malformed/error/unreachable response sanitization;
  - exact integer counter validation;
  - concurrent different-client and same-client CAS behavior;
  - method/subpath blocking và no direct file access.
- Regression trên cùng head:
  - `test_proxy.py`: **10/10 pass**.
  - `test_write_log.py`: **16/16 pass**.
  - `test_backup_read.py`: **17/17 pass**.
  - `test_backup_write.py`: **28/28 pass**.
  - `test_master_read.py`: **16/16 pass**.
  - `test_settings_read.py`: **24/24 pass**.
  - `test_settings_write.py`: **43/43 pass**.
- Python syntax compile: pass.

## Live verification

Trên ai tool thật, transaction hiện tại đã pass:

- name-only CAS write: `200`;
- CAS rollback: `200`;
- public response chỉ contract an toàn;
- master semantic state, derived DB/config/CSV được restore;
- secret fingerprints không đổi;
- cycle state/alwaysrun override không đổi;
- activity `+2`, change `+4`, action `+2` cho write + rollback;
- AutoCycle vẫn alive.
