# P4 Design-Freeze Addendum

Status: design freeze for review. No implementation authorization is granted by this document.
Date: 2026-09-09
Baseline: `AUTORELOG/p4_baseline.md`, merged in `main` at `31c0e6bc09fd25fcd3a2cc0a8995cb5fb3691fc9`

## Purpose

This addendum freezes the six architecture decisions required before a P4 implementation PR. It is deliberately separate from Phase 3 runtime state and from the immutable SQLite generation store.

P4 remains implementation-NO-GO until this addendum is reviewed and approved.

## Decision 1: separate mutable operational database

P4 uses a dedicated mutable SQLite database for queue, job, lease, checkpoint, manual-override, and worker-event state.

- Suggested path: `runtime/operational/p4_operational.sqlite3`.
- P3 generation files under `runtime/sqlite/<generation>.sqlite3` remain immutable, verified read-only artifacts.
- P4 must never insert, update, delete, or store queue/job/lease state in a P3 generation.
- The P4 database must have its own schema version, migration record, backup/restore policy, and integrity check.
- Operational DB writes must be transactional; a job claim and lease ownership change must commit atomically.
- P3 generation ID and source hash may be recorded as read-only references in P4 checkpoints, but the P3 file itself is not a mutable state store.

Minimum operational tables:

| Table | Purpose |
|---|---|
| `jobs` | Durable command state, idempotency key, attempt and result |
| `leases` | Scheduler/worker/supervisor ownership and heartbeat |
| `checkpoints` | Handoff, rollback and last-known reconciliation boundary |
| `manual_overrides` | Persisted manual intent/observation, precedence and expiry |
| `worker_events` | Append-only lifecycle and dispatch evidence |

## Decision 2: five authority/process roles

P4 has five logical authorities. A role may share a host process during implementation, but authority boundaries and test identities must remain distinct.

| Role | Authority | Explicit non-authority |
|---|---|---|
| Flask API/UI | Authenticate, validate, read state, enqueue jobs, return API-compatible responses | No remote WebSocket, no remote mutation, no worker spawn/kill in request handlers |
| Scheduler | Compute desired state from master, schedule and overrides; enqueue idempotent jobs | No remote WebSocket and no direct side effect |
| Remote I/O worker | Sole owner of remote WebSocket session and all remote reads/mutations; dispatch and result classification | No schedule policy, no direct master-edit decisions |
| Sync/materializer worker | Consume remote observations, persist live status/materialized views, reconcile identities | No remote mutation and no direct scheduler restart |
| Supervisor | Own process lifecycle, readiness, worker leases, graceful stop and restart policy | No business-level remote action and no schedule decision |

The remote I/O worker is the single remote owner. Sync reads are dispatched through that owner or use an explicitly shared read lease that still prevents a second remote session. No other role may open a remote WebSocket.

## Decision 3: scheduler isolation and AutoCycle shadow-canary

The scheduler computes and enqueues intent; it never performs remote actions.

Migration sequence:

1. **Shadow:** AutoCycle remains the only remote mutator. The P4 scheduler computes the same desired transitions, records decisions in the operational DB, and performs no dispatch.
2. **Compare:** Compare desired state, target set, ordering, fixed/orphan protections, schedule boundaries, and manual-override precedence over an agreed observation window.
3. **Handoff checkpoint:** Quiesce new dispatch, capture active group, live snapshot hash, current generation reference, pending jobs, and leases.
4. **Active P4:** Stop AutoCycle's mutation authority, acquire the scheduler and remote-worker leases, then enable one controlled canary group.
5. **Expansion:** Expand only after canary parity, no duplicate owner, crash/restart, and rollback evidence pass.

There must never be a period in which AutoCycle and the P4 remote worker can both dispatch mutations. AutoCycle may remain installed as the rollback path but must be observably non-owning while P4 is active.

## Decision 4: lease, dispatch, retry and `unknown`

Operational leases are durable and heartbeat-based. Every owner and job claim has an owner ID, acquisition time, heartbeat, expiry, and release reason.

- A job is retried automatically only when it is proven not dispatched, such as validation failure, queue transaction failure, or worker failure before dispatch begins.
- After remote dispatch begins, an acknowledgement is received, or the connection outcome is ambiguous, the job is never blindly retried.
- Such a job becomes `unknown` with dispatch evidence, remote action ID, timestamps, and worker event references.
- Reconciliation reads the remote state and resolves `unknown` to `succeeded`, `failed`, or a new explicitly-created corrective job.
- Lease expiry makes ownership eligible for takeover, but does not itself authorize replay of an ambiguous side effect.
- All retries use the original idempotency key; a new corrective action gets a new job ID and records its parent.
- Lease and dispatch timeouts are configuration, not hard-coded request-handler behavior, and must be visible in evidence.

Required job lifecycle:

`queued -> claimed -> running -> dispatched -> succeeded|failed|unknown`

Cancellation is allowed only before dispatch. A cancellation after dispatch changes the job to `unknown` and requires reconciliation.

## Decision 5: persisted manual remote-change semantics

Manual remote changes are persisted in the operational DB rather than represented only by a temporary JSON banner.

Each record includes target identity, observed/requested state, source, actor/request ID when known, created time, expiry, precedence, evidence reference, and resolution state.

Precedence is fixed as follows:

1. Safety invariants: orphan hard-block and fixed-client protection cannot be bypassed by a scheduled close.
2. Explicit authenticated user intent: a web/remote control request is durable and wins over scheduled intent until cleared or expired.
3. Detected manual remote change: a status transition not attributable to the system is persisted as an observation/override for its configured cooldown; the scheduler does not immediately fight it.
4. Pause/stop policy: a pause suppresses scheduler mutations and preserves the observed state.
5. Scheduled desired state is the fallback when no higher-precedence record is active.

Manual records must be visible in status, auditable, idempotent by request ID where applicable, and resolved explicitly by expiry, clear action, or reconciliation. A banner-only/manual-memory path is not sufficient for P4.

## Decision 6: route-by-route Flask migration

The public API contract is preserved while authority moves out of Flask. Migration is performed by route group, not by a flag that changes every route at once.

### Phase A: read/status routes

- Keep response shapes and authentication.
- Read operational status from the P4 DB and existing P3/read stores as appropriate.
- No remote side effect.

### Phase B: scheduler and worker controls

- Validate request and authorization in Flask.
- Create an idempotent operational job.
- Return the existing success/error shape plus a compatible job reference where possible.
- Do not spawn, kill, or directly invoke `AutoCycle`, remote sync, or remote action scripts from the request handler.

### Phase C: master/config writes

- Preserve `MasterEdit` and materialization ordering.
- Enqueue downstream reconcile/materialize work after the master transaction commits.
- Do not let a stale worker overwrite a newer master edit.

### Phase D: rollback and deprecation

- Keep the legacy route adapter available behind an explicit rollback switch during canary.
- Route-level rollback must stop new enqueueing, reconcile in-flight/unknown work, and restore the previous owner without changing the HTTP contract.
- Remove direct Flask worker/remote authority only after route-specific evidence and closeout.

The first implementation PR must name the route group it migrates and prove untouched routes remain behaviorally unchanged.

## Frozen invariants

- Operational SQLite is mutable and separate from P3 immutable SQLite generations.
- There is one remote I/O owner.
- Scheduler code never performs remote action.
- AutoCycle shadow-canary precedes handoff; no dual mutator period is allowed.
- Ambiguous post-dispatch work becomes `unknown`; no blind retry.
- Manual changes are persisted, prioritized, and reconciled by contract.
- Flask migrates route groups incrementally while preserving the API contract.
- Fixed clients remain protected and orphan clients remain blocked.
- No P4 job or credential is stored in a P3 generation or exposed through a public response.

## Required design-gate evidence

Before implementation authorization, the coder must provide:

1. Operational DB schema and migration/backup plan, with an explicit proof it cannot write P3 generation files.
2. Process/role graph and lease ownership table.
3. Shadow-canary comparison format and observation window.
4. Dispatch state machine, timeout values, and `unknown` reconciliation procedure.
5. Manual override precedence examples, persistence and expiry tests.
6. First route-group migration scope, unchanged API fixtures, and rollback procedure.

Until all six evidence groups are approved, P4 implementation PRs remain NO-GO.
