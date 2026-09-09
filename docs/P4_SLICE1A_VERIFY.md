# P4 Slice 1A Verification

## Scope

Slice 1A adds only the isolated mutable operational SQLite foundation for P4.
It does not wire the database into Flask, the scheduler, AutoCycle, remote I/O,
route adapters, or any write/control endpoint.

Operational state is fixed at:

```text
<runtime>/operational/p4_operational.sqlite3
<runtime>/operational/backups/*.sqlite3
```

P3 immutable generations remain under `<runtime>/sqlite/` and are never opened
as operational write targets.

## Implemented proof

- Schema version and checksum are persisted in `schema_meta`.
- `WAL`, foreign keys and SQLite integrity checks are required at open/create.
- Operational path guard rejects arbitrary paths and P3 generation paths.
- SQLite authorizer rejects `ATTACH` and `DETACH` on the operational connection.
- Explicit transaction primitive uses `BEGIN IMMEDIATE` and rolls back on error.
- Job claim and lease heartbeat update their related rows atomically.
- Online backup writes a sidecar manifest containing schema identity, size and
  SHA-256.
- Restore verifies the manifest and integrity-checks a temporary copy before
  replacing only the operational database.
- Backup/restore acceptance test records and compares the P3 fixture SHA-256
  and `st_mtime_ns`; both remain unchanged.
- The schema contains no credential, token, password or secret columns.

## Verification commands

Run from the repository root:

```bat
python tests\security\test_operational_sqlite.py
python -m py_compile webapp\backend\repositories\operational_sqlite.py tests\security\test_operational_sqlite.py
```

Result on branch `p4-slice1a-operational-runtime`:

```text
........
Ran 8 tests in 3.936s
OK
```

## Boundary evidence

The implementation diff adds only:

- `webapp/backend/repositories/operational_sqlite.py`
- `tests/security/test_operational_sqlite.py`
- this verification document

No `app.py`, `config.py`, `services/sqlite_runtime.py`, scheduler, AutoCycle,
remote I/O, WebSocket, route handler, or write endpoint was changed. Because
there is no Flask/control-path import or wiring in this slice, the existing
route behavior remains the baseline behavior.

## Not claimed by this slice

This PR does not claim acceptance for scheduler ownership, worker ownership,
shadow-canary comparison, remote dispatch/`unknown`, manual override
precedence, Flask enqueue-control, route migration, or cutover/handoff. Those
remain separate slices and gates.
