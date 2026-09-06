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

The current live source shape and read-only audit are recorded in
`docs/PHASE3_GOLDEN_DELTA_20260906.md`.

## Verification Commands

Run from the repository root:

```bat
python tests\security\test_sqlite_import.py
python -m py_compile webapp\backend\repositories\sqlite.py webapp\backend\services\sqlite_import.py tests\security\test_sqlite_import.py
```

Result on the reviewed head:

- `11` tests passed, `0` failed.
- All three edited Python files compiled successfully.
- Tests use fake source values and temporary databases; the separate golden
  audit also ran the importer against the current live HTTP source without
  writing upstream.

## Review Gate

A verified candidate is evidence only. Production read cutover, dual writes,
route rewrites, migration of worker ownership, and any runtime-control action
require a separate reviewed change.
