# Phase 2 Close-out

## Status

Phase 2 is complete at the approved implementation boundary. Completion means
that the safe write actions were implemented through the repository abstraction,
while unsafe runtime-control and destructive actions were audited, frozen, and
deliberately deferred. Deferred is an intentional safety result, not missing
work to be opened without evidence.

Baseline main commit before this documentation close-out:

`161e92d93d4a818c276fc1b88570f77bc354561a`

The source of truth remains the local `ai tool` API and its `db.html` behavior.
The website never writes the ai tool filesystem directly.

## Implemented Scope

### Slice 1-11

- Read abstraction and regression-preserving proxy.
- `POST /api/log`.
- `GET/POST /api/cycle/backup` for listing and creation.
- `GET/POST /api/settings` with public projection and safe partial update.
- `POST /api/master` for guarded display-name-only CAS changes, not full master
  or schedule editing.
- Canonical remote live read and guarded selector read.
- Guarded `DELETE /api/cycle/backup/<name>`.

### Bundle 1

- `POST /api/settings/test_telegram`.
- `POST /api/settings/open_browser`.

### Bundle 2

- `POST /api/ai_fix` for the frozen `cycle`, `web`, and `userimport` queue
  creation schema.
- Sync, answer deletion, and watcher control remain deferred.

## Current Website Route Inventory

The current `main` dispatch contains 11 read handlers:

- 9 API reads: cycle status, simple cycle status, sync status, general status,
  AI-fix status, backup listing, master read, remote live read, and settings.
- 2 static JSON reads: `clients_master.json` and `client_database.json`.

The current `main` dispatch contains 8 enabled write/action boundaries:

- `POST /up/api/log`
- `POST /up/api/cycle/backup`
- `POST /up/api/settings`
- `POST /up/api/master` (display-name-only CAS)
- `DELETE /up/api/cycle/backup/<name>`
- `POST /up/api/settings/test_telegram`
- `POST /up/api/settings/open_browser`
- `POST /up/api/ai_fix`

No generic dynamic write dispatcher exists. All enabled writes require the
website Bearer token and then use typed service/repository calls.

## Deferred Registry

These routes are intentionally not in the website write dispatch and remain
blocked with the existing write-method rejection:

### Bundle 2

- `POST /up/api/sync_remote`
- `POST /up/api/sync_all`
- `DELETE /up/api/ai_fix/answers`
- `POST /up/api/ai_fix/watcher`

Reason: cross-process RemoteWs/continuous-sync exclusion, watcher ownership,
heartbeat/PID identity, queue exclusion, archive, and preimage evidence are not
complete.

### Bundle 3

- `POST /up/api/clear_history`
- `POST /up/api/cycle/backup/<name>/restore`
- `POST /up/api/cycle/fix`
- `GET/POST /up/api/cycle/url`

Reason: destructive write-set coverage, process/remote state rollback, tunnel
ownership, cross-process exclusion, sanitized errors, and disposable
verification gates are not complete.

### Bundle 4

- `POST /up/api/sync_continuous/<start|stop>`
- `POST /up/api/cycle/<start|stop>`
- `POST /up/api/alwaysrun/<open|stop>`

Reason: PID/mutex ownership, watchdog and lifecycle races, durable stop intent,
stale-state handling, override CAS, process/remote preimage, sanitized errors,
and disposable harness evidence are not complete.

## Evidence

- Recorded golden contract smoke: **16 passed, 0 failed**. This is approved
  historical evidence and was not rerun during the close-out audit.
- Documented security aggregate before Bundle 2 AI Create: **384 passed, 0
  failed**.
- Bundle 2 AI Create verification: **29 passed, 0 failed**.
- Therefore the close-out evidence is the **documented aggregate 384 + 29 =
  413 passed, 0 failed**, not a claim that one combined test command was run.
- No GitHub check runs were configured for the Bundle 2 PR; the counts come from
  repository verification documents and the PR verification record.
- `tests/contract/API_INVENTORY.md` remains the historical Phase 0 golden
  inventory. This close-out document is the current website implementation and
  deferred overlay.

## Phase 3 Gate

Phase 3 SQLite/WAL, one-way sync redesign, session authentication, SSE, and
named tunnel work were not started in Phase 2. Phase 3 remains gated until this
documentation-only close-out PR is reviewed, approved, and merged. Even after
that gate opens, SQLite must be introduced behind the existing repository
abstraction without rewriting the approved Phase 2 route contracts.
