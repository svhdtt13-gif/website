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

## Phase 2 — Ghi an toàn và audit boundaries ✅

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

## Phase 3 — SQLite

Planned scope:

- SQLite with WAL behind the existing repository abstraction.
- Preserve the approved Phase 2 route contracts while introducing local durable
  data ownership.
- Define one-way synchronization boundaries without silently enabling deferred
  runtime-control actions.

Entry gate:

- SQLite schema, migration, rollback, and source-of-truth transition are reviewed
  separately before implementation.
- No worker/scheduler, session, SSE, named tunnel, or Windows service migration
  is bundled into the SQLite change.

## Phase 4 — Worker/scheduler runtime control

- Define process ownership, PID/mutex identity, stale-state handling, and durable
  lifecycle intent.
- Audit and implement only after the deferred Bundle 2/3/4 boundaries receive
  separate safety approval.

## Phase 5 — Session authentication

- Replace the website boundary's Basic-auth dependency with reviewed session
  authentication and explicit CSRF/write policy.
- Preserve the repository boundary and approved API contracts.

## Phase 6 — Realtime SSE

- Replace polling only after connection lifecycle, authorization, backpressure,
  and rollback behavior are reviewed.

## Phase 7 — Named tunnel

- Introduce a fixed named tunnel only after tunnel process ownership, secret
  handling, origin binding, and recovery behavior are reviewed.

## Phase 8 — Windows services

- Move approved long-running components to Windows services only after service
  ownership, startup/shutdown ordering, recovery, and rollback are reviewed.

## Quy tắc an toàn suốt quá trình

1. Mọi test ghi đều làm trên bản sao/tunnel riêng, giờ thấp điểm.
2. Trước mỗi phase: backup ai tool (`/api/cycle/backup`) when the approved
   backup contract covers the intended write set.
3. `db.html` là chuẩn đối chiếu: lệch là dừng, sửa webapp chứ không sửa tool.
4. Không thay scheduler/worker ownership as part of a documentation or schema
   change.
