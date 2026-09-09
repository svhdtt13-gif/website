# P4 Design-Gate Evidence Pack

Status: evidence submitted for review. This document does not authorize P4 implementation.
Date: 2026-09-09
Base: `main` after P4 Design-Freeze Addendum merge `189367f7fcc5db31e61dc66cb4d0e53f939b31ed`
Runtime policy: read-only evidence collection only; no write endpoint was called.

## Evidence status legend

- `DESIGN`: concrete design artifact ready for review before implementation.
- `FIXTURE`: captured from the current production-compatible Flask read path without mutation.
- `IMPLEMENTATION_REQUIRED`: acceptance test that must run after the implementation exists; it is specified here but is not claimed as passed.

All six evidence groups are supplied below. The final P4 implementation GO decision remains with the reviewer after checking the implementation-required items.

## Gate 1: operational DB schema, migration and backup

Status: `DESIGN`.

### Database boundary

The operational DB is a separate mutable file:

```text
P4 operational state: C:\ProgramData\AutoGhostStoryWebsite\runtime\operational\p4_operational.sqlite3
P3 immutable state:   C:\ProgramData\AutoGhostStoryWebsite\runtime\sqlite\<generation>.sqlite3
```

The P4 process configuration must open only the operational path for writes. P3 generation paths are opened read-only and only through the existing runtime reader. No SQLite `ATTACH`, cross-file trigger, or shared connection may make a P3 generation writable.

### Initial schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  target_json TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  parent_job_id TEXT REFERENCES jobs(job_id),
  status TEXT NOT NULL CHECK (status IN
    ('queued','claimed','running','dispatched','succeeded','failed','cancelled','unknown')),
  attempt INTEGER NOT NULL DEFAULT 0,
  owner_id TEXT,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  started_at TEXT,
  dispatched_at TEXT,
  finished_at TEXT,
  lease_until TEXT,
  remote_action_id TEXT,
  result_json TEXT,
  error_json TEXT
);

CREATE INDEX jobs_status_idx ON jobs(status, created_at);
CREATE INDEX jobs_owner_idx ON jobs(owner_id, lease_until);

CREATE TABLE leases (
  lease_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  release_reason TEXT
);

CREATE TABLE checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  p3_generation_id TEXT,
  p3_source_hash TEXT,
  active_group TEXT,
  live_snapshot_hash TEXT,
  pending_jobs_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data_json TEXT NOT NULL
);

CREATE TABLE manual_overrides (
  override_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  source TEXT NOT NULL,
  requested_state TEXT,
  observed_state TEXT,
  precedence INTEGER NOT NULL,
  request_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  resolved_at TEXT,
  resolution TEXT,
  evidence_json TEXT NOT NULL
);

CREATE INDEX manual_override_active_idx
  ON manual_overrides(target_id, resolved_at, expires_at, precedence);

CREATE TABLE worker_events (
  event_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  job_id TEXT REFERENCES jobs(job_id),
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data_json TEXT NOT NULL
);
```

### Migration and backup contract

- Every schema change runs inside a transaction and records a monotonic `schema_meta` version.
- Startup refuses to claim leases if migration is incomplete or `PRAGMA integrity_check` is not `ok`.
- Backup uses SQLite online backup into `runtime/operational/backups/`, followed by a manifest containing schema version, creation time, size and SHA-256.
- Restore is supervisor-owned: stop new claims, drain or mark active jobs according to the `unknown` rule, verify the backup, atomically replace only the operational DB, then run integrity check.
- Restore never replaces or mutates `runtime/sqlite/<generation>.sqlite3`.

### Proof obligations

`IMPLEMENTATION_REQUIRED`:

1. File-path allowlist test rejects a P3 generation as an operational DB target.
2. SQLite authorizer/integration test rejects `ATTACH` and write statements against a P3 connection.
3. Transaction test proves job claim and lease heartbeat commit atomically.
4. Backup/restore test proves P3 generation SHA-256 and file mtime are unchanged.

## Gate 2: role/process and lease ownership graph

Status: `DESIGN`.

### Authority graph

```text
Authenticated Flask API/UI
  -> validates request and writes one jobs row
  -> reads operational status and immutable P3 references

Scheduler
  -> reads master/materialized state and manual overrides
  -> writes desired-state jobs; never opens WebSocket

Supervisor
  -> owns process lifecycle and readiness
  -> grants/renews role leases; never performs business actions

Remote I/O worker  <== sole remote WebSocket owner
  -> claims jobs and performs remote reads/mutations
  -> records dispatch/result/unknown evidence

Sync/materializer worker
  -> consumes remote observations from the remote I/O owner
  -> writes live status/materialized views; never mutates remote state
```

### Lease ownership

| Lease | Sole owner | Allowed action | Explicitly forbidden |
|---|---|---|---|
| `supervisor` | Supervisor | Start/stop/restart role processes | Remote business action |
| `scheduler` | Scheduler | Compute/enqueue desired state | WebSocket or dispatch |
| `remote_io` | Remote I/O worker | All remote WebSocket reads/mutations | Schedule policy |
| `sync_materializer` | Sync worker | Persist observations/materialized views | Remote mutation or worker restart |
| `flask_adapter` | Flask process/request adapter | Validate/enqueue/read | WebSocket, spawn/kill, direct scripts |

Required invariant: a process without the relevant lease exits or remains read-only. A second remote I/O process must fail closed before opening a WebSocket.

`IMPLEMENTATION_REQUIRED`:

1. Start two copies of each singleton role concurrently and capture lease rows/events.
2. Prove exactly one remote I/O owner and zero remote actions from the loser.
3. Kill the lease owner and prove takeover requires expiry plus fresh ownership, not blind job replay.
4. Verify Flask request logs contain no worker spawn/kill or remote WebSocket activity.

## Gate 3: shadow-canary comparison format and window

Status: `DESIGN`.

### Shadow record

Each scheduler decision is recorded without dispatch:

```json
{
  "shadow_id": "uuid",
  "observed_at": "ISO-8601",
  "schedule_revision": "sha256",
  "p3_generation_id": "verified-generation-or-null",
  "live_snapshot_hash": "sha256",
  "active_group": "gr1",
  "desired_by_client": {"client_1": "running", "client_2": "stopped"},
  "planned_jobs": ["group_on:gr1", "group_off:gr4"],
  "manual_override_ids": [],
  "fixed_protected": true,
  "orphans_blocked": true
}
```

### Comparison dimensions

AutoCycle remains the only mutator during shadow. Compare P4 shadow decisions to observed AutoCycle decisions on:

- active group and schedule boundary;
- exact target client set and action ordering;
- fixed-client protection and orphan hard-block;
- manual override precedence and expiry;
- current live snapshot/source hash and P3 generation reference;
- duplicate/coalesced jobs and idempotency keys;
- pause/stop behavior and midnight/wraparound behavior.

### Window and handoff

- Minimum observation window: three complete schedule cycles, including one midnight/wraparound boundary and one manual override expiry.
- Canary scope: one non-fixed scheduled group; fixed and orphan behavior remains protected and out of mutation scope.
- Handoff requires zero unexplained decision mismatches, zero duplicate remote owner events, and no unresolved shadow safety violations.
- At handoff, AutoCycle is quiesced before the P4 scheduler and remote I/O leases are acquired. No dual mutator window is allowed.

`IMPLEMENTATION_REQUIRED`:

1. Produce a comparison report for the complete window.
2. Prove shadow mode creates no remote action event.
3. Prove canary handoff has one owner and a rollback checkpoint.

## Gate 4: dispatch state machine, timeouts and `unknown`

Status: `DESIGN`.

### State machine

```text
queued
  -> claimed       (transaction: owner + lease)
  -> running       (worker started)
  -> dispatched    (remote send began or remote action id exists)
  -> succeeded     (remote result + reconciliation agree)
  -> failed        (proven not applied)
  -> unknown       (dispatch/connection outcome is ambiguous)
```

Cancellation is allowed only before `dispatched`. After dispatch, cancellation and worker loss produce `unknown`, never an automatic retry.

### Timeout policy

Initial values are configuration, not hard-coded API behavior:

| Timeout | Initial value | Expiry action |
|---|---:|---|
| queue claim | 30 s | release claim if not started |
| worker heartbeat | 15 s | mark lease expired after 45 s without heartbeat |
| remote connect | 8 s | fail before dispatch if no socket/action send |
| remote acknowledgement | 10 s | `unknown` if dispatch began but no decisive result |
| state reconciliation | 60 s | keep `unknown`, schedule visible reconcile |
| job retention | 30 days | archive only after evidence retention policy |

No timeout may infer `failed` after remote dispatch unless a fresh remote read proves the side effect was not applied.

### Reconciliation

An `unknown` job records remote action ID, request payload hash, socket/session reference, timestamps and worker events. A reconcile read maps it to `succeeded` or `failed`; if neither is safe, it remains `unknown` and requires an explicit corrective job.

`IMPLEMENTATION_REQUIRED`:

1. Test crash before dispatch: safe retry with same idempotency key.
2. Test crash after dispatch/ambiguous socket: `unknown`, no automatic replay.
3. Test remote read resolves `unknown` to success/failure.
4. Test lease takeover does not replay an ambiguous job.

## Gate 5: manual override persistence, precedence and expiry

Status: `DESIGN`.

### Precedence

Higher number wins, except safety invariants always reject unsafe actions:

| Priority | Source | Behavior |
|---:|---|---|
| safety | orphan/fixed invariant | Cannot be overridden by schedule |
| 50 | explicit authenticated user intent | Durable target intent until clear/expiry |
| 40 | detected unattributed remote change | Persisted cooldown; scheduler does not immediately fight it |
| 30 | explicit pause/stop policy | Suppress scheduler mutation and preserve state |
| 10 | scheduled desired state | Fallback |

Records are keyed by target and active precedence. Expired records remain for audit but are excluded from desired-state evaluation. A new authenticated request creates a new request ID and resolves/replaces only its own prior active intent.

### Test vectors

| Scenario | Expected result |
|---|---|
| Scheduled close conflicts with fixed client | No stop job; safety event recorded |
| Scheduled start conflicts with explicit user stop | No start until user intent clears/expires |
| Unattributed remote stop during active schedule | Persist observation/cooldown; do not immediately restart |
| Manual override expires | Scheduler resumes from current desired state after fresh read |
| Pause active while manual change occurs | Preserve state; no scheduler mutation until resume |
| Orphan appears in remote snapshot | Persist observation, remain blocked, never enqueue start |

`IMPLEMENTATION_REQUIRED`:

1. Persist/reload override across worker restart.
2. Verify precedence and expiry with the vectors above.
3. Verify status exposes active override source/expiry without credentials.
4. Verify clear/resolve is idempotent and auditable.

## Gate 6: first route-group migration, API fixtures and rollback

Status: `FIXTURE` for current response shapes; `DESIGN` for migration plan.

### First route group

The first implementation group is read/status only:

- Public website: `GET /up/api/status`
- Public website: `GET /up/api/sync_status`
- Public website: `GET /up/api/cycle_status`
- Public website: `GET /up/api/cycle/status`
- Public website: `GET /up/api/ai_fix/status`

The namespace mapping is explicit:

```text
public website /up/<subpath> -> internal handler key <subpath> -> upstream fixture source /<subpath>
public website /up/api/status -> api/status -> upstream /api/status
```

The public `/up/` prefix is part of the website dispatch contract. The internal handler key is the `READ_HANDLERS` subpath (for example `api/status`); it is not itself the public URL.

`GET /up/api/remote_live` is intentionally excluded because the current handler opens a remote read path. All POST/PUT/PATCH/DELETE routes are excluded from the first group.

### Captured API fixtures

Captured from both the public website and the upstream/internal fixture source with authenticated GET requests on 2026-09-09 15:06 local time. No mutation request was made. All five public routes and their upstream counterparts returned `200 application/json` with matching top-level keys.

| Public website route | Internal handler key | Upstream fixture source | Status | Required top-level shape |
|---|---|---|---:|---|
| `/up/api/status` | `api/status` | `/api/status` | 200/200 | `clients`, `lastUpdated`, `time` |
| `/up/api/sync_status` | `api/sync_status` | `/api/sync_status` | 200/200 | `continuous_running`, `continuous_pid`, `interval_sec`, `status_interval_sec`, `last_sync`, `extraction_method`, `total_clients`, `source` |
| `/up/api/cycle_status` | `api/cycle_status` | `/api/cycle_status` | 200/200 | `running`, `status`, `stopped`, `cycle_paused`, `alwaysrun_paused`, `alwaysrun_disabled` |
| `/up/api/cycle/status` | `api/cycle/status` | `/api/cycle/status` | 200/200 | `checked_at`, `cycle_running`, `cycle_pid`, `sync_running`, `sync_pid`, `360auto`, `qnyh`, `cycle_paused`, `alwaysrun_paused`, `alwaysrun_disabled`, `stop_flag`, `last_log`, `state`, `manual_overrides` |
| `/up/api/ai_fix/status` | `api/ai_fix/status` | `/api/ai_fix/status` | 200/200 | `watcher`, `models`, `pending`, `recent_done`, `recent_failed` |

Fixture assertions for the first implementation PR:

- public `/up/...` authentication and status code remain unchanged;
- public route maps to the intended internal handler key and upstream `/api/...` fixture source;
- required keys and JSON types remain unchanged across public and upstream responses;
- no new write or remote side effect is introduced;
- dynamic PID/time/log values are compared by type and invariant, not literal value;
- no P3 generation path, token, password or secret field appears in the response.

### Route rollback

- First group is guarded by a route-group adapter switch, defaulting to the current implementation until parity evidence exists.
- Rollback disables the adapter, leaves all write/control routes untouched, and restores the original handler path without changing the HTTP contract.
- If the adapter has a lease/read failure, it returns the existing error shape and does not spawn a worker or call a remote script.
- Rollback evidence includes public `/up/...` versus upstream `/api/...` route mapping, route-by-route fixture diff, process command-line check, remote WebSocket event count, and P3 generation hash/mtime stability.

`IMPLEMENTATION_REQUIRED`:

1. Add exact redacted JSON fixtures and contract tests for all five routes.
2. Run old/new handler parity with the same captured state.
3. Prove Flask restart and route GETs do not spawn/kill workers or open remote WebSockets.
4. Exercise adapter rollback and verify untouched control routes remain unchanged.

## Review conclusion

All six design-gate evidence groups are now concretely specified and supported by current read-only route fixtures where applicable. The implementation-required checks are explicit acceptance gates, not claimed as passed. P4 implementation remains **NO-GO** until a reviewer approves this pack and the coder produces the listed runtime evidence during implementation.
