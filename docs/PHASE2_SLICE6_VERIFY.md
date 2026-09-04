# Phase 2 Slice 6 — POST cycle backup write

## Scope

Chỉ mở `POST /up/api/cycle/backup` qua route -> service -> repository -> ai tool.
GET backup giữ contract Slice 3. Không mở restore/delete proxy, không mở write
endpoint khác, không SQLite/Session/SSE/Worker/Tunnel và không đổi AutoCycle
scheduler ownership.

## Contract and security boundary

- Website: `POST /up/api/cycle/backup`.
- Upstream: `POST /api/cycle/backup` với Basic Auth.
- Website Bearer token phải pass trước handler/repository.
- Repository tự thêm/ghi đè `X-DB-Editor: 1`; client marker không phải auth.
- JSON/form body và Content-Type forward raw; empty request body phải tới upstream
  đúng `b""`, không synthesize `{}`.
- Success body `200 application/json` phải parse thành object có contract đầy đủ:
  `status=OK`, string `backup`, manifest object với string `created_at`/`label`
  và list `files`/`script_files`.
- Malformed JSON, non-object hoặc thiếu/sai success contract tại HTTP 200 ->
  generic `502 {"error":"invalid backup response"}`, không echo raw body và
  không retry.
- Upstream error/unreachable giữ status/error behavior hiện tại của repository.
- Missing/wrong Bearer -> `401` JSON, zero upstream write.
- PUT/PATCH/DELETE cùng path và restore/delete subpaths -> `403` JSON, zero
  upstream write.

## Artifact and concurrency policy

Ai tool tạo một ZIP mới dưới `tools/cache/cycle_backups` chứa runtime/script
entries nếu tồn tại và `manifest.json`. Website không đọc/ghi artifact local.

Golden filename chỉ có timestamp tới giây và dùng mode `w`; vì vậy service dùng
process-local lock riêng cho backup và monotonic spacing `1.1s` giữa các upstream
backup POST. Policy này tránh collision trong một website process; đây **không
phải cross-process/cross-machine lock**. Website không stop/pause AutoCycle, nên
snapshot artifact vẫn best-effort theo golden khi runtime file đang thay đổi.

## Rollback and cleanup

- Code rollback: revert commit hoặc bỏ `api/cycle/backup` khỏi write allowlist/
  handler; GET backup và các write gate cũ giữ nguyên.
- POST chỉ tạo artifact, không restore source state để rollback.
- Live cleanup chỉ DELETE đúng filename lấy từ POST response sau khi xác minh:
  artifact không có trước test, name marker đúng, manifest label đúng, hash/size
  local khớp API metadata, không overwrite pre-existing artifact.
- Cleanup dùng existing ai-tool DELETE trực tiếp với Basic Auth + `X-DB-Editor: 1`;
  website không proxy DELETE. Không bulk delete. Nếu điều kiện fail thì giữ
  artifact và báo blocker.

## Automated verification

- `tests/security/test_backup_write.py`: **28/28 pass**:
  - Bearer missing/wrong/forged marker gate;
  - exact JSON/form raw body and Content-Type forwarding;
  - empty request body forwarded as exact `b""`;
  - repository Basic Auth and owned `X-DB-Editor: 1`;
  - malformed/non-object/missing-contract HTTP 200 -> generic 502, raw body not
    echoed and exactly one upstream attempt;
  - upstream error and unreachable handling;
  - concurrent requests serialized with upstream spacing >= 1.0s;
  - GET remains available; other methods/subpaths blocked;
  - no direct file access and repository post mapping.
- Regression:
  - `test_proxy.py`: **10/10 pass**.
  - `test_write_log.py`: **16/16 pass**.
  - `test_backup_read.py`: **17/17 pass** (POST without Bearer explicitly
    expects `401`; PUT/PATCH/DELETE remain `403`).
  - `test_master_read.py`: **16/16 pass**.
  - `test_settings_read.py`: **24/24 pass**.
- Python syntax compile for app/config/backup and backup tests: pass.

## Live verification (2026-09-04)

Three independent transactions were executed, each with its own pre/post
snapshot and cleanup; no shared `+1` artifact claim:

1. Direct golden POST: `200`; exact response filename used; new ZIP marker,
   manifest label, local SHA-256 and size matched API metadata; exact artifact
   deleted; state snapshot unchanged; AutoCycle alive.
2. Proxy POST on current validation code: `200`; Bearer gate and forged client
   marker path exercised; validated success response, new ZIP marker, manifest
   label, local SHA-256 and size matched API metadata; exact artifact deleted;
   state snapshot unchanged; AutoCycle alive.
3. Concurrent proxy POST: two `200` responses; two distinct marker artifacts,
   no overwrite; each manifest/hash/size verified independently; exact artifacts
   deleted; state snapshot unchanged; AutoCycle alive.

Snapshot included settings, client database, cycle state, master hash, client
IDs, and AutoCycle PID. No sync, remote WebSocket, restart/stop, or other write
endpoint was called.
