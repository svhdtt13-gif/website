# Phase 3 Implementation: Guarded SQLite Reads

This PR implements the approved Issue #16 baseline. It is not a production enablement change.

## Runtime Boundary

- `SQLITE_READ_ENABLED=false` by default.
- `SQLITE_MASTER_DATABASE_READ_ENABLED=false` and `SQLITE_PUBLIC_SETTINGS_READ_ENABLED=false` by default.
- Wave 1 SQLite routes are `/up/api/master`, `/up/client_database.json`, and `/up/api/settings`.
- `/up/clients_master.json` remains HTTP-only.
- All other routes remain on the typed authenticated HTTP repository.
- App startup queues a single-flight refresh for every enabled group without an eligible candidate. A group discovered while another refresh is running remains queued.

## Generation Lifecycle

`SQLiteRuntimeCoordinator` acquires the explicit mutex for state checks and final publication, builds the candidate outside the mutex under one bounded deadline, requires the existing import/shadow verification plus normalized route parity to return `verified`, checkpoints/closes the writable WAL candidate, then atomically renames the database and replaces `current.json`. A slow local importer cannot extend mutex ownership; a timed-out build is not published and its staging file is cleaned when the worker exits.

The manifest contains generation identity, candidate path, receipt/snapshot IDs, source hash, schema identity, `captured_at`, `completed_at`, and status. Source snapshots are used for stored-candidate hash integrity. Route responses are reconstructed from normalized SQLite tables, including `master_meta`, ordered master/database rows, and redacted public settings. On every read, the canonical hash of the reconstructed normalized response is compared with the verified source endpoint hash; normalized tampering therefore fails closed.

Published readers use SQLite URI `mode=ro` and `PRAGMA query_only=ON`. Failed, partial, corrupt, stale, or mismatched candidates fall back to HTTP and are never served as SQLite.

## Write Fence

Master and settings services pass an expected mutation observation into the narrow pre-upstream callback. The callback persists runtime ineligibility, a fence token, expected fields/changes, and an expected revision before `ai_tool.post` is called, and holds `Local\\WebsiteSQLiteGenerationMutex` through write completion bookkeeping. A later candidate may clear that fence only when its HTTP snapshot contains the expected mutation. A stable two-pass snapshot that still contains old data remains fenced and is not publishable for that group.

The callback is not injected when the relevant SQLite read group is not configured, preserving the Phase 2 write path while all flags are false. A crash after upstream dispatch leaves the durable fence in place; startup and reads remain HTTP-only until a verified replacement generation observes the mutation. Other fenced groups remain ineligible when a different group publishes a generation.

## Refresh and Fallback

The in-process coordinator queues one bounded refresh worker and the Windows named mutex serializes state transitions and publication across processes. Each HTTP request receives only the remaining refresh budget, including the redacted settings request. Local acquisition, import, and normalized verification are executed under the same deadline but outside the cross-process mutex. Refresh failures leave the old generation unpublished and the group ineligible. If HTTP also fails, the existing typed HTTP error is returned; stale SQLite is never served.

## Verification Status

The disposable candidate tests and runtime tests cover normalized route reconstruction and integrity binding, same-generation reads, read-only access, staging isolation, pre-dispatch mutation fencing/reconciliation, crash-state persistence, startup queueing, runtime/config separation, HTTP error fallback, bounded local builds, schema mismatch, normalized corruption, and Windows two-process mutex contention.

Live acceptance remains a deployment gate outside this PR:

- Three consecutive live parity imports with zero mismatches.
- Route parity against the current production HTTP contracts.
- Crash recovery/fencing evidence.
- Same-generation master/database evidence.
- Two-process contention evidence on the Windows deployment.
- Fallback and rollback evidence, including no-secret review.

No production flags should be enabled until all evidence is attached to Issue #16 and separately reviewed.
