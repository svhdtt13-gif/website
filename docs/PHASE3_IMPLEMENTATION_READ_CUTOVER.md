# Phase 3 Implementation: Guarded SQLite Reads

This PR implements the approved Issue #16 baseline. It is not a production enablement change.

## Runtime Boundary

- `SQLITE_READ_ENABLED=false` by default.
- `SQLITE_MASTER_DATABASE_READ_ENABLED=false` and `SQLITE_PUBLIC_SETTINGS_READ_ENABLED=false` by default.
- Wave 1 SQLite routes are `/up/api/master`, `/up/client_database.json`, and `/up/api/settings`.
- `/up/clients_master.json` remains HTTP-only.
- All other routes remain on the typed authenticated HTTP repository.
- App startup schedules a single-flight refresh for every enabled group without an eligible candidate.

## Generation Lifecycle

`SQLiteRuntimeCoordinator` writes an immutable staging database, requires the existing import/shadow verification plus normalized route parity to return `verified`, checkpoints/ closes the writable WAL candidate, then atomically renames the database and replaces `current.json`. Readers resolve and pin the manifest generation once per request. No mtime/newest-file selection exists.

The manifest contains generation identity, candidate path, receipt/snapshot IDs, source hash, schema identity, `captured_at`, `completed_at`, and status. Source snapshots are used only for stored-candidate hash integrity. Route responses are reconstructed from normalized SQLite tables, including `master_meta`, ordered master/database rows, and redacted public settings.

Published readers use SQLite URI `mode=ro` and `PRAGMA query_only=ON`. Failed, partial, corrupt, stale, or mismatched candidates fall back to HTTP and are never served as SQLite.

## Write Fence

Master and settings services accept a narrow pre-upstream callback. The callback persists runtime ineligibility and a fence before `ai_tool.post` is called, and holds `Local\\WebsiteSQLiteGenerationMutex` through write completion bookkeeping. The callback is not injected when the relevant SQLite read group is not configured, preserving the Phase 2 write path while all flags are false. A crash after upstream dispatch leaves the durable fence in place; startup and reads remain HTTP-only until a verified replacement generation is published.

## Refresh and Fallback

The in-process coordinator starts one bounded refresh for an ineligible enabled group. The explicit Windows named mutex is shared across processes for refresh, publication, invalidation, and write dispatch. Each HTTP request receives only the remaining refresh budget, including the redacted settings request. Reads during refresh use HTTP. Refresh failures leave the old generation unpublished and the group ineligible. If HTTP also fails, the existing typed HTTP error is returned; stale SQLite is never served.

## Verification Status

The disposable candidate tests and runtime tests cover normalized route reconstruction, same-generation reads, read-only access, staging isolation, pre-dispatch fencing, crash-state persistence, startup scheduling, runtime/config separation, HTTP error fallback, and Windows two-process mutex contention.

Live acceptance remains a deployment gate outside this PR:

- Three consecutive live parity imports with zero mismatches.
- Route parity against the current production HTTP contracts.
- Crash recovery/fencing evidence.
- Same-generation master/database evidence.
- Two-process contention evidence on the Windows deployment.
- Fallback and rollback evidence, including no-secret review.

No production flags should be enabled until all evidence is attached to Issue #16 and separately reviewed.
