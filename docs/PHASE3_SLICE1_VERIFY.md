# Phase 3 Slice 1 Verification

## Scope

This branch contains only a disposable SQLite candidate path. It is not wired
into Flask routes and does not change production reads, writes, worker
ownership, scheduler ownership, tunnel behavior, or the deferred Bundle 2/3/4
actions.

The candidate importer:

- fetches the fixed source matrix through authenticated HTTP APIs;
- uses the shared four-field public settings projection;
- takes a bounded two-pass stable snapshot using canonical hashes;
- preserves source bytes, hashes, order, JSONL sequence, and offsets;
- writes one new versioned SQLite database with WAL and repository-configured
  foreign keys;
- records an import receipt and fails shadow verification closed.

## Verification Command

Run from the repository root:

```bat
python tests\security\test_sqlite_import.py
python -m py_compile webapp\backend\repositories\sqlite.py webapp\backend\services\sqlite_import.py tests\security\test_sqlite_import.py
```

Result on commit `b9912f2` before the final documentation-only commit:

- `5` tests passed, `0` failed.
- Both importer modules compiled successfully.
- The test uses fake HTTP source values and temporary databases; it does not
  contact the live ai tool.

## Review Gate

A verified candidate is evidence only. Production read cutover, dual writes,
route rewrites, migration of worker ownership, and any runtime-control action
require a separate reviewed change.
