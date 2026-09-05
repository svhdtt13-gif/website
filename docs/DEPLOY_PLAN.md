# Lộ trình triển khai (làm tại repo này, ai tool giữ nguyên)

## Phase 0 — Docs + skeleton đọc ✅

- Tài liệu kiến trúc/API/schema/kế hoạch.
- Skeleton proxy read-only + dashboard tĩnh.
- Contract/security baseline đã ghi nhận.
- `db.html` gốc và ai tool không bị sửa trong phase này.

## Phase 1 — Tách module, giữ nguyên tính năng ✅

- Frontend/backend đã tách module.
- Static traversal containment và read-only proxy regression đã xác nhận.
- Dashboard smoke đã xác nhận bằng browser.
- Chi tiết: `docs/PHASE1_VERIFY.md`, `docs/PHASE1_UISMOKE.md`.

## Phase 2 — Ghi an toàn và audit boundaries ✅ close-out pending merge

Phase 2 hoàn tất theo safety-gated scope:

- Slice 1–11: repository/service abstraction, safe log/backup/settings/master
  operations, guarded remote reads và guarded backup deletion.
- Bundle 1: guarded Telegram test và server-side browser-open action.
- Bundle 2: guarded AI queue creation.
- Bundle 2 sync/answers/watcher: deferred.
- Bundle 3 clear-history/restore/cycle-fix/cycle-url: audited and NO-GO.
- Bundle 4 continuous-sync/cycle/alwaysrun: audited and NO-GO.

Completion does not mean every golden endpoint is proxied. Actions without
ownership, rollback, stale-state, sanitized-error, and disposable-verification
evidence remain deliberately blocked.

Close-out documents and the current deferred registry are in
`docs/PHASE2_CLOSEOUT.md`. The historical golden inventory remains
`tests/contract/API_INVENTORY.md`.

### Phase 2 Evidence

- Recorded contract smoke: `16 passed, 0 failed` (historical evidence; not rerun
  during close-out).
- Documented security aggregate: `384 + 29 = 413 passed, 0 failed`.
- The aggregate is explicitly not represented as one combined test command.
- No GitHub check runs were configured for the Bundle 2 PR.

## Phase 3 — Độc lập ⏸️ NO-GO until close-out merge

Planned scope:

- SQLite with WAL behind the existing repository abstraction.
- One-way sync from ai tool while preserving approved Phase 2 route contracts.
- Session login replacing Basic Auth at the website boundary.
- Realtime SSE replacing dashboard polling.
- Cloudflared named tunnel with fixed URL and service ownership.

Entry gate:

- Phase 2 close-out documentation PR reviewed, approved, and merged.
- Deferred registry remains authoritative and no blocked action is silently
  promoted into Phase 3 implementation.
- SQLite schema and migration/rollback plan reviewed separately.

## Quy tắc an toàn suốt quá trình

1. Mọi test ghi đều làm trên bản sao/tunnel riêng, giờ thấp điểm.
2. Trước mỗi phase: backup ai tool (`/api/cycle/backup`) when the approved
   backup contract covers the intended write set.
3. `db.html` là chuẩn đối chiếu: lệch là dừng, sửa webapp chứ không sửa tool.
4. Không thay scheduler/worker ownership as part of a documentation or schema
   change.
