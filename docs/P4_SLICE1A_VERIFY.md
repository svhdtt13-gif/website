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
- Actual schema identity is derived from `sqlite_master` plus table columns,
  indexes and foreign keys; metadata alone is not trusted.
- `WAL`, foreign keys and SQLite integrity checks are required at open/create.
- Operational path guard rejects arbitrary paths and P3 generation paths.
- SQLite authorizer rejects `ATTACH` and `DETACH` on the operational connection.
- Explicit transaction primitive uses `BEGIN IMMEDIATE` and rolls back on error.
- Each claimed job persists its exact `lease_name` alongside `owner_id`.
- Job claim and lease heartbeat update their related rows atomically.
- Heartbeat is scoped to the exact `(owner_id, lease_name)` binding.
- `backup_to()` validates the live source schema and integrity before checkpoint
  or copying it.
- Restore validates manifest, hash, integrity and actual schema on the source
  backup, then validates the copied candidate before `os.replace()`.
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

Result after the restore-preflight fix on branch `p4-slice1a-operational-runtime`:

```text
............
Ran 12 tests in 6.046s
OK
```

`py_compile` passed for both edited Python files. The Phase 3 importer regression
also passed independently:

```text
................
Ran 16 tests in 5.455s
OK
```

The 12 tests include:

- same-owner, different-lease heartbeat isolation;
- schema tamper with unchanged `schema_meta`, failing closed;
- invalid live source rejected before backup output is written;
- hash-valid, integrity-valid but schema-invalid restore rejected without
  replacing the current operational database;
- P3 hash and `st_mtime_ns` preservation through backup/restore;
- path, `ATTACH`, WAL/FK/integrity, transaction, active-lease and corrupt-backup checks.

The restore-preflight fix is covered by commits `4f5399dbe6991d40dadbad2aefde7c3facfd496f`,
`e49bd8fc2a5621c204ebcff40ddba644f74c49a9`, `48b5429794c4ffed4fd60f9e62acef1ebd5b31d9`,
`60047cc794b138bd9afa0068d1df9eac9c55b86c`, `c3dbbfb0e4906a31094edc525e4177ff2b75cd11`
and `d572b0f9f20e0362238f5c05de1bf6205543ae6e`.

## Boundary evidence

The implementation diff remains limited to:

- `webapp/backend/repositories/operational_sqlite.py`
- `tests/security/test_operational_sqlite.py`
- this verification document

No `app.py`, `config.py`, `services/sqlite_runtime.py`, scheduler, AutoCycle,
remote I/O, WebSocket, route handler, or write endpoint was changed. Because
there is no Flask/control-path import or wiring in this slice, existing route
behavior remains the baseline behavior.

## Not claimed by this slice

This PR does not claim acceptance for scheduler ownership, worker ownership,
shadow-canary comparison, remote dispatch/`unknown`, manual override
precedence, Flask enqueue-control, route migration, or cutover/handoff. Those
remain separate slices and gates.
