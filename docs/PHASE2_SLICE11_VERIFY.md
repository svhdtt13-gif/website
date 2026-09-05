# Phase 2 Slice 11 — guarded backup deletion

## Scope

Mở đúng một operation hiện có:
`DELETE /api/cycle/backup/<name>`.
Slice này không mở restore, scheduler/cycle control, SQLite, session, SSE, tunnel,
frontend redesign hoặc bất kỳ generic dynamic write nào.

## Golden contract

Golden `DELETE /api/cycle/backup/<name>` requires Basic Auth and uses only the decoded
name path segment. Query parameters and request body are ignored by golden behavior.
The webapp preserves that compatibility while never using or forwarding query/body.

- Existing valid backup: `200 application/json`, exact public shape
  `{"status":"OK","deleted":"<name>"}`.
- Invalid name grammar: `400 application/json`,
  `{"error":"Tên backup không hợp lệ"}`.
- Valid grammar but missing backup: `404 application/json`,
  `{"error":"Không tìm thấy backup"}`.
- Golden runtime exception: `500` JSON with its exception text; the webapp never exposes
  that runtime detail and maps upstream/runtime failure deterministically to
  `502 application/json`, `{"error":"backup deletion unavailable"}`.

## Dedicated route and transaction

The webapp has a separate Flask DELETE route for exactly
`/up/api/cycle/backup/<name>`. The existing generic `/up/<path:subpath>` write
dispatcher remains POST-only and does not receive a dynamic prefix rule.

The raw route path is decoded once with strict UTF-8 validation, must contain exactly
one segment, and must match the golden `[A-Za-z0-9_.-]+` name grammar. Slash/backslash,
NUL/control, malformed percent, residual percent, `.`/`..`, and traversal/separator
variants are rejected before upstream DELETE. The upstream path is rebuilt from the
canonical validated name with a fixed safe quote set.

Under `_BACKUP_LOCK`, the service fresh-reads the backup listing, requires the canonical
name to occur exactly once, and only then calls the repository's named `delete_backup()`
method. Unknown/stale/ambiguous names return `409` with zero upstream DELETE. The
repository has no generic DELETE primitive: it sends only Basic Auth, `X-DB-Editor: 1`,
the canonical URL, and an empty body.

## Automated verification

Executed on branch `phase2-slice11-backup-delete`:

- `python tests/security/test_backup_delete.py`: **39/39 pass**.
- `python tests/security/test_backup_delete_service.py`: **12/12 pass**.
- Existing Slice 1–10 security scripts: **295/295 pass**.
- Combined assertions: **346 passed, 0 failed**.
- `python -m py_compile webapp/backend/app.py webapp/backend/repositories/aitool.py webapp/backend/services/backup.py tests/security/test_backup_delete.py tests/security/test_backup_delete_service.py`: pass.

Coverage includes Bearer/auth ordering, exact success/error mapping, fresh membership,
query/body non-forwarding, raw path canonicalization, traversal/separator blocking,
malformed list/delete response sanitization, no retry, dedicated method/path blocking,
Basic Auth/editor marker, concurrency serialization and source boundary scans.

## Live disposable verification

Executed against the real golden ai tool on `127.0.0.1:8080` through the branch proxy
on `127.0.0.1:8091` with a temporary Bearer token. No AutoCycle process was stopped or
scheduler ownership changed.

Sequence and result:

1. Captured baseline backup names, protected state hashes, logs and AutoCycle process count.
2. Created only the generated disposable backup
   `cycle_20260905_072707_slice11_disposable_1788568027368.zip`: `200`, keys
   `backup,manifest,status`.
3. Fresh backup listing contained the generated name.
4. Called the new webapp DELETE route with ignored query/body; received exact `200`
   `{status:OK,deleted:<generated name>}`.
5. Fresh listing no longer contained the generated name; all pre-existing production
   backup names were unchanged.
6. `client_database.json`, `clients_master.json`, `config.json`, `cycle_state.json`,
   `manual_override.json`, `remote_session.json` and `remote_rooms.json` SHA-256 hashes
   were unchanged.
7. `cycle.log` line count was unchanged. `activity_history.jsonl` and
   `change_log.jsonl` each gained exactly two expected entries, both for the generated
   name: `source=web,event=cycle_backup` and `source=web,event=cycle_backup_delete`.
   No selector, sync, toggle or unrelated action event was added.
8. AutoCycle process count remained `1 -> 1`.

The generated disposable artifact was deleted and verified absent. Cleanup is complete;
no pre-existing production backup was deleted.

Do not merge until implementation review is complete.
