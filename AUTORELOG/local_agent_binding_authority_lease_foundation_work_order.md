# Local Agent Binding Authority / Lease Foundation

Status: design freeze and work-order for review. Implementation is NO-GO until
this document is explicitly approved.

Date: 2026-09-12
Baseline: `main` at `ffe8360bbc31ca72dfe5d1ae223cf81703080b84` after PR #29
release. Issue #1 checkpoint: `5637395388`.

This is the smallest next slice after the Host Configuration UI Foundation. It
defines a future fencing/eligibility check for local-agent ownership. It does
not grant runtime authority; actual runtime authority remains `LEGACY` until a
separate, approved handoff slice.

## 1. Objective

Freeze a fail-closed future-command context over:

```text
host_id + profile_id + verified_remote_identity
+ binding_generation + binding lease + idempotency_key
```

The first five values identify the binding and its fenced lease. The
`idempotency_key` is required for every future command context and prevents a
verified context from becoming an untracked replay. A positive result means
only `AUTHORITY_ELIGIBILITY / FENCED_BINDING_MATCH`; it is not an ACTIVE
transition, a runtime-owner selection, or permission to dispatch.

The fence identity is explicitly:

```text
fence_identity = (authority_epoch, fence_counter)
```

`authority_epoch` comes from a fresh coordinator/supervisor source that is not
inside the Operational SQLite backup or any restored database. The counter is
monotonic only within that epoch. The pair is never reused, and every
lease/heartbeat/eligibility/revalidation compares both values.

## 2. Existing boundaries

- `host_profile_bindings` remains portable domain state in the separate
  Portable Domain Store.
- `binding_generation` is a positive integer, unique for a `(host_id,
  profile_id)` lineage, and is never silently reused for a different binding.
- A verified remote identity is a stable, non-secret identity reference backed
  by an explicit verification result. It is not a password, cookie, session,
  bearer value, account credential or display name.
- The authoritative source for that identity is a future Portable Domain Store
  schema migration to version 3, adding non-secret `verified_identity_ref` to
  `remote_profiles`. A binding snapshot obtains it only by joining the exact
  `host_profile_bindings.profile_id` to that profile row. Existing
  `account_ref`, display names, session state and remote observations are never
  identity proofs or fallbacks. Existing v2 rows without the field remain
  ineligible until explicitly verified. The implementation slice defined by
  this work-order includes that schema-v3 migration; this document does not
  perform it and no separate proof store/adapter is permitted as a fallback.
- P4 operational state remains in the separate mutable Operational SQLite.
- Binding leases, fence values, heartbeat, expiry, jobs, checkpoints and worker
  events do not move into the Portable Domain Store or a P3 generation.
- P3 SQLite generations remain immutable and read-only.
- The existing P4 lease primitive is not automatically promoted to runtime
  authority. Its current `lease_name`, `owner_id` and timestamp fields are
  insufficient evidence until this scoped fence contract is implemented.
- One future remote I/O owner remains the only possible owner of a remote
  WebSocket. This slice does not choose, start or hand off that owner.

## 3. Frozen terminology

### Binding identity

The binding identity is the exact triple `(host_id, profile_id,
binding_generation)` read from an authoritative binding snapshot. Matching only
host and profile is invalid.

### Verified remote identity

The verified identity is the exact non-secret identity reference associated with
the binding at verification time. The reference must match the binding and the
future command context. Identity drift, missing proof or an account mismatch is
fail-closed. This work-order does not define login, session refresh or identity
acquisition.

### Fenced binding lease

A binding lease is a durable record in the canonical P4 Operational SQLite,
scoped to one exact binding identity and one owner identity. It must carry,
directly or through a transactionally linked row:

- owner ID;
- exact `host_id`, `profile_id` and `binding_generation`;
- exact verified remote identity reference;
- acquisition, heartbeat and expiry evidence;
- release/reconciliation state;
- `fence_identity = (authority_epoch, fence_counter)`.

The epoch is created by a fresh non-backup coordinator/supervisor source before
the first acquisition after startup/restore. The counter is coordinator/store
generated and increases within that epoch. Neither value is supplied by a
Local Agent, restored from portable data, or accepted from a caller as proof of
ownership. A holder with an old epoch or counter is rejected forever for that
authority scope, even if its process is still running or its old timestamps
appear live.

### Authority eligibility

The only positive result allowed by this slice is:

```text
AUTHORITY_ELIGIBILITY / FENCED_BINDING_MATCH
```

This result is evidence for a separately approved future operation. It must not
set runtime ownership, change binding state, enqueue a job, open a WebSocket,
or expose secrets. Actual authority remains `LEGACY`.

## 4. Canonical coordinator and store

There must be one canonical lease coordinator backed by one authoritative
Operational SQLite for a given host/profile authority scope. A Local Agent must
not self-grant authority from a local cache, copied database, portable package
or independently writable SQLite. If the canonical coordinator is unavailable,
the result is read-only/fail-closed; it is not safe to let two PCs each believe
they own Profile A.

The coordinator owns:

- the authoritative clock used for acquisition, heartbeat and expiry;
- bounded TTL configuration;
- fresh `authority_epoch` creation outside restored DB/backup state;
- `fence_counter` allocation and monotonicity within that epoch;
- serialized acquisition, renewal, release and reconciliation;
- invalidation of stale owners and evidence recording.

Caller-supplied `now`, `heartbeat_at`, `expires_at`, TTL or fence values are
rejected as authority inputs. A caller may provide a request ID and
idempotency key, but the coordinator computes all authority timestamps and the
full fence identity.

The current P4 `claim_job()` behavior may remain a job-foundation primitive,
but an expired-row overwrite must not be treated as sufficient fencing for
runtime authority. The future implementation must add and verify scoped,
monotonic fencing before it can be used by a control path.

## 5. Cross-store verification protocol

Portable Domain Store and Operational SQLite are deliberately separate. There
is no cross-database atomic transaction, and `ATTACH`/`DETACH` is forbidden.
The future protocol must use revalidation and fencing instead of pretending
that the two stores commit atomically:

1. Read one authoritative Portable Domain Store snapshot for the exact host,
   profile, binding generation, binding state and verified identity.
2. Reject missing, conflicting, `OFFLINE`, `RETIRED` or revoked binding state.
3. Acquire or renew the scoped lease only in the canonical Operational SQLite,
   using the coordinator clock and a new/current `(authority_epoch,
   fence_counter)`.
4. Record the binding snapshot identity and verified identity beside the lease
   evidence; do not copy portable lease authority into the portable store.
5. Before any future dispatch, re-read the binding snapshot and the operational
   lease and compare host, profile, verified identity, generation, owner,
   `authority_epoch`, `fence_counter`, expiry and idempotency key.
6. Reject on any mismatch. Do not repair the mismatch inline, fall back to
   host/profile matching, or dispatch after a failed re-check.

Binding generation is the optimistic cross-store fence. A binding update can
invalidate an older lease through coordinator reconciliation, but the protocol
must never claim atomicity across the two databases. Every future dispatch
owner must enforce the fence again at dispatch time.

## 6. Required match and fencing contract

A future command context may receive `FENCED_BINDING_MATCH` only when all
conditions are proven in the applicable read/revalidation boundaries:

1. `host_id`, `profile_id`, `binding_generation`, verified identity and a
   stable `idempotency_key` are present and valid.
2. Exactly one binding row matches the full host/profile/generation identity.
3. The binding is not `OFFLINE`, `RETIRED`, revoked or otherwise non-authoritative.
   This check never changes it to `ACTIVE`.
4. Host, profile and verified identity records are mutually consistent.
5. The canonical operational lease carries the same binding triple and
   verified identity reference.
6. The lease owner is the expected owner for the requested future role.
7. The lease is unexpired according to the coordinator clock.
8. Heartbeat/expiry evidence is parseable and within bounded TTL policy.
9. The full fence identity `(authority_epoch, fence_counter)` is current for
   the scope. A different epoch always rejects, even when its counter is
   numerically higher or equal. No older pair is accepted after acquisition,
   takeover or restore.
10. No competing live lease claims the same authority scope.
11. The binding generation has not been superseded.
12. The idempotency key is unique for the intended command or resolves to the
   same previously recorded command; a new corrective action gets a new key.

The check must be read-only. Lease acquisition, renewal, release and takeover
are separate coordinator transactions and must revalidate the current binding
generation and verified identity before committing.

## 7. Expiry, takeover and fencing lifecycle

The future implementation must model these states explicitly:

```text
unbound -> requested -> acquired -> heartbeating -> released
                                      |
                                      +-> expired -> reconciliation-eligible
```

- `requested` is input only and grants no authority.
- `acquired` allocates a new fence identity in the canonical store.
- `heartbeating` may extend only the exact owner/scope/current
  `(authority_epoch, fence_counter)` pair.
- `released` is terminal for that lease instance.
- `expired` makes reconciliation eligible; it does not grant takeover.
- Expiry never dispatches, retries, or proves that a prior remote action
  failed.
- A future takeover requires fresh verified identity, current binding
  generation, canonical coordinator serialization, a fresh authority epoch or
  strictly newer counter within the current epoch, and explicit invalidation
  of the old holder. The old fence identity must remain rejected even if the
  old process continues running.
- An ambiguous post-dispatch result remains `unknown` and requires the
  existing reconciliation contract. It must not be replayed from a new lease.

## 8. Restart, backup and restore rules

Lease rows in a backup are historical evidence, not live authority. A restart,
copy or restore must never revive mutate authority:

- A normal process restart begins unbound and must reacquire through the
  canonical coordinator with a fresh fencing token.
- An operational DB restore opens in quarantine/read-only mode.
- Before any lease can be acquired after restore, the supervisor/coordinator
  must establish a fresh non-backup `authority_epoch`, reset the
  epoch-local counter, invalidate all restored lease rows, and reconcile the
  current binding generation and verified identity.
- The fresh epoch/restore marker must not be sourced solely from the restored
  database or a portable package. If it cannot be established, authority stays
  disabled.
- Every restored lease/heartbeat/revalidation must compare the new epoch and
  its counter; a restored row from any prior epoch is historical evidence and
  can never match. No `(authority_epoch, fence_counter)` pair may be reused.
- Restored jobs with post-dispatch ambiguity remain `unknown`; restore cannot
  mark them successful or replay them.
- Backup/portable export never transfers live lease ownership, fence tokens,
  runtime epochs, session material, PID state, mutex state or pending remote
  acknowledgements.

## 9. Fail-closed mismatch matrix

| Condition | Required result | Forbidden behavior |
|---|---|---|
| Missing host, profile, generation, verified identity, lease or idempotency key | Reject | Fill a default or use the viewed profile |
| Unknown host, profile or verified identity | Reject | Create identity during verification |
| No exact binding triple | Reject | Match by host/profile only |
| Multiple exact binding rows | Reject | Pick newest/arbitrary row |
| Generation is zero, negative, malformed or reused | Reject | Coerce or renumber silently |
| Binding is `OFFLINE`, `RETIRED`, revoked or non-authoritative | Reject | Convert it to `ACTIVE` or treat reads as authority |
| Verified identity differs or is missing | Reject | Fall back to account/display name |
| Lease scope or identity differs from binding | Reject | Repair the lease or fall back to host/profile |
| Lease missing, released, expired or malformed | Reject/reconcile only | Renew or takeover implicitly in the check |
| Lease owner differs from expected owner | Reject | Steal, impersonate or continue work |
| Fence epoch differs from coordinator epoch | Reject | Accept a restored or stale epoch |
| Fence counter is stale, missing, reused or non-monotonic within the epoch | Reject | Accept old owner after takeover |
| Fence pair is reused after restore | Reject | Treat equal integers as equal authority |
| Competing live lease exists | Reject | Use a mutex or local cache as proof |
| Newer binding generation exists | Reject | Replay against the older generation |
| Caller supplies authority timestamps, TTL or fence | Reject | Trust client clock or client token |
| Canonical coordinator is unavailable | Read-only/reject | Self-grant from a local SQLite |
| Snapshot changes across revalidation | Reject | Commit from mixed snapshots |
| Operational DB integrity/schema/path/restore check fails | Reject | Open a P3 generation or continue degraded |
| Restored lease row or old runtime epoch is encountered | Reject/reconcile | Revive historical mutate authority |

All rejection paths must produce sanitized evidence containing only a reason
class and request/correlation ID. No bearer value, session data, cookie,
password, secret path, raw remote payload or identity credential may be logged.

## 10. Explicit non-goals and safety boundary

This work-order must not add or authorize:

- an ACTIVE switch, `OFFLINE -> ACTIVE` transition, profile activation or
  runtime-owner selection;
- mutate authority from the existence of a lease;
- remote WebSocket reads or mutations;
- Start/Stop Cycle, AlwaysRun, reconnect, login, client selection or group
  controls;
- scheduler ownership, AutoCycle handoff, shadow-canary execution or worker
  spawn/kill;
- Flask route wiring, enqueue behavior, API contract changes or UI controls;
- P4 Slice 1B implementation, production migration, dual writes or cutover;
- lease/fence copying through portable export/import or backup/restore;
- credential, session, cookie, token, PID, mutex or tunnel persistence;
- a fallback to legacy global host/account identity.

The current runtime authority remains `LEGACY`. Any future `ACTIVE` domain
meaning requires a separately approved state machine, verified identity,
current binding generation, canonical lease and current fencing token. This
document never transitions a binding or runtime into that state.

## 11. Required implementation evidence

After this design is approved, a separate implementation branch must provide
all of the following before its review can request a GO decision:

1. A schema/contract fixture proving Portable Domain Store and Operational
   SQLite remain separate, with no writes to P3 generations and no cross-DB
   atomicity claim.
2. Exact-match tests for host, profile, verified identity and generation,
   including newer-generation and duplicate-row rejection.
3. Canonical-coordinator concurrency tests for Agent A/Agent B proving only
   one scoped lease can acquire the current fence.
4. Fencing tests proving a stale holder is rejected after acquisition,
   takeover, restart and restore, even when its process remains alive.
5. Lease tests for acquisition, heartbeat isolation, bounded TTL, release,
   expiry, reconciliation and competing-owner rejection.
6. Clock tests proving caller timestamps, TTL and fence values cannot control
   authority; all accepted time comes from the coordinator/store.
7. Takeover tests proving expiry alone does not grant authority and that a
   takeover requires fresh verified identity, current generation and a newer
   fence.
8. Backup/restore tests proving restored lease rows cannot become live without
   a fresh non-backup runtime/restore epoch and reconciliation.
9. Idempotency tests proving a duplicate key resolves to the same recorded
   command-context or fails closed on conflict; eligibility/acquisition never
   enqueue or dispatch, and remote dispatch count for this slice is exactly
   zero. The future dispatch slice must separately prove no duplicate remote
   dispatch.
10. A no-side-effect test proving eligibility verification does not open a
    WebSocket, enqueue a job, change binding state, start/stop a process,
    invoke a scheduler, or mutate a remote system.
11. Portable schema-v3 tests proving `verified_identity_ref` is the sole
    authoritative identity source for this contract and that `account_ref`,
    display name, session and observation values cannot satisfy the check.
12. Sanitized audit evidence proving rejected mismatches expose no secret,
    session or identity credential material.
13. Regression evidence that Host Configuration, Portable Domain Store,
    Remote Profile Manager and read-only route contracts remain unchanged.

## 12. Review gate

This document is ready for formal design review, not implementation. Approval
must explicitly confirm:

- verified remote identity and idempotency are required in future command
  context;
- `verified_identity_ref` is sourced only from the approved Portable Domain
  Store schema-v3 migration and never inferred from `account_ref` or
  observations;
- a lease is not runtime authority and actual authority remains `LEGACY`;
- fencing identity is `(authority_epoch, fence_counter)`, with an epoch from
  outside restored DB/backup state, monotonic counter per epoch, and permanent
  stale-holder rejection;
- expiry is only reconciliation-eligible and never an automatic takeover;
- one canonical coordinator controls the authority store and its clock;
- no cross-database atomicity is assumed; revalidation and fencing are used;
- backup/restore/restart cannot revive mutate authority;
- this slice's eligibility/acquisition path has exactly zero remote dispatch;
- every mismatch is fail-closed;
- no ACTIVE/runtime/scheduler/remote control is included;
- the twelve implementation evidence groups above are sufficient.

Until approval is recorded, do not create the implementation branch, edit
runtime code, open P4 Slice 1B, or change the existing ownership model.

## References

- `AUTORELOG/p4_design_freeze_addendum.md`
- `AUTORELOG/p4_design_gate_evidence.md`
- `AUTORELOG/p4_baseline.md`
- `docs/PORTABLE_DOMAIN_STORE_FOUNDATION.md`
- `docs/REMOTE_PROFILE_MANAGER_FOUNDATION.md`
- `docs/P4_SLICE1A_VERIFY.md`
- `docs/U1_PROFILE_CONTEXT.md`
- `docs/DATA_OWNERSHIP_PORTABILITY_MULTI_REMOTE.md`
- `docs/DEPLOY_PLAN.md`
