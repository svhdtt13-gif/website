# P4 Baseline: Flask / Worker / Scheduler Separation

Status: baseline only. No runtime, API, process, or GitHub state changes are part of this document.
Date: 2026-09-09

## Scope and invariants

P4 starts only after Phase 3 Wave 1 is closed. The following are fixed invariants for the baseline:

- `tools/clients_master.json` remains the configuration source of truth.
- `client_database.json` and `config.json` remain generated/materialized views until a replacement contract is approved.
- `fixed` clients are always-on and must never be stopped by a scheduled-group close.
- Orphan clients remain hard-blocked.
- Every remote WebSocket action must remain serialized.
- No local `qnyh.exe` start is allowed; opening a client is a remote-row operation.
- AI-tool state and the P3 SQLite read cutover are out of scope for P4.

## Current process model

| Component | Entry point | Current responsibility | Current authority |
|---|---|---|---|
| Flask API/UI | `WebAppControl/flask/app_public.py` | Authenticated editor, reads, master writes, sync commands, cycle/AlwaysRun controls | Direct file writes; can start/stop workers and issue remote actions |
| Scheduler | `tools/AutoCycle.ps1` | 20-second reconciliation and time-slot close/open | Remote WebSocket mutations; cycle state; local process cleanup |
| Remote sync worker | `continuous_sync_remote.ps1` | Remote snapshot, live status, identity merge, structural change detection | Writes DB/master mirrors and can restart AutoCycle |
| Startup watchdog | `tools/start_onboot.ps1` | Starts and monitors Flask, tunnel, 360Auto, workers and AI-fix watcher | Can kill/restart workers and react to network/process failure |
| Legacy actuator | `FullAutoActuator.ps1` | Historical remote actuator | Mutation path is explicitly disabled |
| AUTORELOG executor | `AUTORELOG/Executor-Agent.ps1` | Observation/reconciliation model and crash/zombie classification | Local start is explicitly blocked; not an active production owner |

The current system has mutex serialization, not ownership isolation. Flask, scheduler, sync worker and watchdog can all influence worker lifecycle. A mutex prevents simultaneous access but does not define which component is authoritative for a job or recovery decision.

## Current ownership and data flow

### Configuration path

1. Web master save validates the schedule.
2. Flask acquires `Local\\AutoGhostStory_MasterEdit` and `Local\\AutoGhostStory_DbWrite`.
3. Flask writes `tools/clients_master.json` and `AUTORELOG/clients_master.json`.
4. Flask invokes `tools/sync_master.ps1`.
5. `sync_master.ps1` materializes `client_database.json`, `config.json`, and the CSV mirror.
6. AutoCycle reloads the generated DB when its signature changes.

### Remote read path

1. `continuous_sync_remote.ps1` acquires `Local\\AutoGhostStory_RemoteSync`.
2. It takes the shared remote WebSocket mutex, reads `scr_list_res`, and releases the mutex.
3. It writes live status and may merge remote structure into master.
4. On structural change it invokes `sync_master.ps1` and restarts AutoCycle.

### Scheduled control path

1. AutoCycle holds `Local\\AutoGhostStory_AutoCycle` for singleton ownership.
2. It reloads materialized configuration and computes active windows in the current implementation.
3. It acquires `Local\\AutoGhostStory_RemoteWs` for each remote action.
4. It closes the outgoing group, verifies it, opens the incoming group, waits for boot, and records cycle state/activity.

### Web control path

Authenticated Flask POST routes can directly invoke sync, cycle, AlwaysRun, pause/resume, backup/restore, and AI-fix operations. Some of these operations launch PowerShell or call the remote action script from the request process. This is the primary boundary P4 must remove or explicitly retain as a controlled exception.

## Existing state and queue artifacts

These are file-based coordination artifacts, not one uniform job queue:

| Artifact | Writer(s) | Meaning |
|---|---|---|
| `cache/cycle_state.json` | AutoCycle | Daily slot ledger and completion state |
| `cache/autocycle.pid` | AutoCycle | Process hint, protected by cycle mutex |
| `cache/remote_sync.pid` | Remote sync | Process hint, protected by sync mutex |
| `cache/cycle_stopped.flag` | Flask/UI | Global cycle stop request |
| `cache/cycle_paused.flag` | Flask/UI | Scheduler reconciliation pause |
| `cache/alwaysrun_paused.flag` | Flask/UI | Fixed-client repair pause |
| `cache/alwaysrun_override.json` | Flask, AutoCycle, sync | Temporary fixed-client disable set |
| `cache/manual_override.json` | Remote sync | 15-minute manual remote-change banner |
| `cache/taskkill_pending.json` | AutoCycle/watchdog | Deferred local process termination |
| `cache/workers_restart.flag` | Flask/maintenance | Watchdog restart request |
| `cache/activity_history.jsonl` | Flask, AutoCycle, sync, watchdog | Operational event history |
| `cache/change_log.jsonl` | Multiple paths | Compatibility/change audit log |

P4 must not silently reinterpret these files. Each artifact needs an owner, schema, durability requirement, and migration/rollback rule before implementation.

## Locks and single-owner baseline

| Lock | Current purpose | P4 implication |
|---|---|---|
| `Local\\AutoGhostStory_RemoteWs` | Serialize all remote WebSocket reads/actions | Must become a worker lease or be held only by the designated remote owner |
| `Local\\AutoGhostStory_AutoCycle` | Prevent duplicate scheduler process | Preserve as a singleton lease or replace with an equivalent durable lease |
| `Local\\AutoGhostStory_RemoteSync` | Prevent duplicate sync worker | Preserve as a singleton lease or replace with an equivalent durable lease |
| `Local\\AutoGhostStory_DbWrite` | Serialize JSON materialization writes | Keep during migration; define transaction/atomicity for the new queue state |
| `Local\\AutoGhostStory_MasterEdit` | Serialize user-owned master edits | Keep around the master write path; do not let a stale sync overwrite a newer edit |
| `Local\\AutoGhostStory_OnBoot` | Prevent duplicate startup watchdogs | Keep one supervisor, or replace it with an explicitly chosen service model |

Required P4 invariant: at most one active remote mutation owner, and a second process must fail closed without issuing a remote action. The existing shared mutex is necessary but is not sufficient evidence of this invariant.

## Crash and restart semantics observed today

- AutoCycle exits immediately when its singleton mutex is already held; loop errors are logged and retried after 30 seconds.
- Remote sync exits when its singleton mutex is already held; the startup watchdog restarts missing workers.
- The watchdog can stop workers on network loss, invalid remote session, or an explicit restart flag, then recreate them after readiness checks.
- AutoCycle persists a daily slot ledger and can reconcile current desired state after restart.
- Remote sync persists live status and structural master data, but has no shared job ID or replay ledger with AutoCycle.
- Deferred local kills use `taskkill_pending.json`; retry and completion are inferred from later watchdog passes.
- WebSocket action IDs are generated per connection, but there is no cross-process idempotency key or durable action result contract.

These semantics are useful compatibility constraints, not a complete P4 contract. In particular, a process crash between remote acknowledgement and local state write can currently leave recovery dependent on the next reconciliation pass.

## Proposed P4 ownership boundary

This is the baseline proposal for approval, not implementation:

- Flask owns authentication, UI/API request validation, read endpoints, and enqueueing commands.
- A designated worker owns all remote WebSocket reads and mutations, action retries, action result recording, and remote-session lifecycle.
- A designated scheduler owns time-window decisions and emits idempotent jobs; it does not open a second remote connection.
- A sync worker owns remote snapshot ingestion and materialization of live status; it does not directly restart another worker without a supervisor/job contract.
- One supervisor owns process lifecycle and readiness checks; request handlers do not kill or spawn workers directly.
- Legacy actuator and AUTORELOG observation paths remain disabled/non-owning until explicitly adopted by a later PR.

The open architectural choice is whether the remote worker and scheduler are one process or two processes sharing a durable queue. P4 acceptance must work either way, provided the single-owner invariant is observable.

## Minimum command/job contract

Every mutating request should eventually produce a durable record with at least:

```json
{
  "job_id": "uuid",
  "kind": "group_on|group_off|alwaysrun_on|alwaysrun_off|sync|reconcile",
  "target_ids": ["client_..."],
  "requested_by": "web|scheduler|sync|supervisor",
  "idempotency_key": "stable caller key",
  "created_at": "ISO-8601",
  "status": "queued|claimed|running|succeeded|failed|cancelled|unknown",
  "attempt": 0,
  "lease_owner": null,
  "started_at": null,
  "finished_at": null,
  "result": null,
  "error": null
}
```

The contract must define duplicate submission, timeout after remote acknowledgement, cancellation, retry limits, and the meaning of `unknown`. A job must never be retried blindly when the remote side may already have applied it.

## Handoff and rollback contract

### Handoff to P4

1. Freeze the current ownership map and capture active process/lock/state evidence.
2. Stop accepting new direct Flask remote actions.
3. Drain or explicitly cancel in-flight actions.
4. Record a handoff checkpoint: current schedule, live snapshot hash, active group, pending jobs, and worker lease.
5. Start the P4 owner in read-only/canary mode first.
6. Enable one controlled group only after ownership and parity evidence pass.
7. Expand by group; keep the existing path available as rollback until closeout.

### Rollback from P4

1. Stop enqueueing new P4 jobs.
2. Wait for a bounded drain period, then mark unresolved jobs `unknown`, never silently successful.
3. Release the P4 owner lease and verify no P4 process can issue remote actions.
4. Restore the pre-P4 scheduler/sync ownership and existing mutexes.
5. Reconcile from a fresh remote snapshot before allowing scheduled mutations.
6. Verify fixed-client protection, schedule parity, and no duplicate remote owner.
7. Preserve all job and handoff evidence.

## P4 acceptance evidence

Before implementation PR approval, the baseline must be extended with evidence for:

1. Process graph: one supervisor, named worker processes, startup command lines, and explicit owner for every artifact above.
2. Queue contract: schema, persistence location, idempotency, retry, timeout, cancellation, and `unknown` handling.
3. Single-owner test: two workers launched concurrently; exactly one owns the remote lease and only one can mutate.
4. Flask isolation test: Flask restart and authenticated API calls do not open a remote WebSocket or spawn a worker directly.
5. Scheduler test: duplicate scheduler start is rejected; crash/restart resumes without replaying a completed slot.
6. Worker crash test: kill during queued, running, acknowledged, and post-result phases; recovery is deterministic.
7. Sync race test: stale sync cannot overwrite a newer master edit; materialized DB remains atomic.
8. Handoff test: drain, checkpoint, acquire ownership, and resume without duplicate actions.
9. Rollback test: P4 disabled, legacy path restored, fresh reconcile completed, and no cross-owner activity observed.
10. Security/no-secret test: queue, logs, job results, and handoff artifacts contain no credentials, session tokens, or forbidden paths.

## Decisions required before implementation

- One combined remote worker versus separate remote-action and sync workers.
- Durable queue format: SQLite, append-only JSONL, or another local store.
- Windows process model: existing watchdog, Windows service, or a new supervisor.
- Whether AutoCycle remains the scheduler during canary or is wrapped behind the new contract.
- Exact semantics for manual remote changes versus scheduled reconciliation.
- Lease timeout, retry budget, action timeout, and `unknown` recovery policy.
- Whether Flask control routes become enqueue-only in the first P4 PR or are removed in a staged deprecation.

## Baseline conclusion

P4 is ready for design review, not implementation. The main technical risk is not the absence of locks; it is multiple components having overlapping authority over remote actions and worker lifecycle. No implementation PR should open until the ownership boundary, job contract, handoff sequence, and acceptance evidence above are approved.
