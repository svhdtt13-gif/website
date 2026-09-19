# LCR1: LEGACY Canary Receiver and Evidence Contract

Status: design freeze for review. This document is contract-only. It does not
implement a receiver, extract a mutation primitive, change AutoCycle, or add a
materialized-state writer.

Baseline: `main@d0ed1a6b68083a9b2dacd71d66931d417398c118` after IS3B2 merge.
Branch: `p4/lcr1-legacy-canary-contract`.
Runtime owner: `LEGACY`.

IS3C remains blocked until LCR1 is approved and the LCR2 implementation proves
these contracts with fault and replay tests.

## 1. Objective and scope

LCR1 defines the smallest LEGACY-owned contract that can receive one manual
canary request, perform at most one target mutation, and leave evidence that
can later be reconciled without guessing whether a remote side effect occurred.

The allowed first operation is deliberately narrow:

```text
group_on
```

The first implementation may target exactly one non-fixed, identity-validated
client per invocation. `group_off` is explicitly not accepted by LCR1: the
discovery record shows that OFF uses the existing `Stop-RowsLocal` sequence,
which combines a conditional `row_toggle`, local process termination, and
verification/retry behavior. That sequence does not fit this one-primitive,
no-retry contract without a separate approved design. `alwaysrun`, `sync`,
`reconcile`, generic refresh, multi-target groups, and schedule decisions are
outside this contract.

LCR1 does not introduce:

- a P4 worker, dispatcher, queue, scheduler, or daemon;
- a retry or polling loop in the receiver;
- a P4 runtime-owner transition or ACTIVE mode;
- a second remote WebSocket reader;
- a new remote protocol or command semantic;
- an automatic corrective action after an ambiguous result.

## 2. Ownership and invocation boundary

The receiver is a thin, LEGACY-owned, manual one-shot process. The human
operator starts it for one canary run. It accepts one request, performs the
pre-send receipt protocol, invokes one extracted legacy single-target primitive
at most once, records the outcome, and exits.

The receiver is not a P4 worker. It must not own a durable queue, remain alive
for polling, schedule future work, or accept a second request in the same
process.

Before opening a remote connection or invoking a mutation primitive, the
receiver must prove all of the following:

1. The request is for one allowed operation and one exact target identity.
2. AutoCycle is quiesced and fenced from remote mutation for this canary run.
3. No competing LEGACY remote mutator owns the run.
4. The receiver holds the existing `Local\\AutoGhostStory_RemoteWs`
   exclusion for the complete remote action boundary.
5. The binding, target identity, and intended state are still valid.

The mutex is serialization evidence, not by itself ownership evidence. If the
quiesce/fence proof is missing, stale, conflicting, or unavailable, the
receiver fails closed before the receipt can become dispatchable and performs
zero remote mutation.

The quiesce fence is a durable canary-run fence, not a network-call lock. It
has a unique `canary_run_id` and `fence_identity` and remains held through all
of these phases:

```text
requested -> acquired -> receipt_accepted -> mutation -> receipt_terminal
                                      -> materialized_observation -> closed
                                                                     |-> abandoned
```

The receipt and the materialized snapshot must carry the same
`canary_run_id` and `fence_identity`. AutoCycle may resume only after the run
is durably `closed` or `abandoned`. `abandoned` is evidence-incomplete/`unknown`
and is never a retry permission. A missing observation closes the run as
`abandoned`; it never permits another actor's later state to be attributed to
this run.

The LEGACY canary authorization/fence coordinator is the sole lifecycle owner.
It creates and acquires the durable fence before the receipt can become
dispatchable, and it is the only actor allowed to close or abandon the run.
The receiver may request and present that fence but cannot self-grant it; the
IS3B2 producer and AutoCycle cannot create, close, or abandon it. The
coordinator acquires the existing `Local\AutoGhostStory_RemoteWs` exclusion
before committing `acquired`, then releases that exclusion only after the
durable fence is active. This prevents AutoCycle from entering a mutation
boundary between fence acquisition and receiver dispatch.

AutoCycle enforcement is a required read-only check of the canonical durable
fence, not a process-presence heuristic. LCR2 must place the check at the
common scheduler call sites `Execute-Slot` and `Resolve-Day`, at entry to each
mutation-capable helper (`Toggle-Rows`, `Stop-RowsLocal`,
`Stop-RowsViaMenu`, and `Kill-Local`), and immediately before every remote
`SW` or local `Stop-Process` after any wait, reconnect, or retry. An active
canary fence causes the AutoCycle path to fail closed with zero mutation. The
check is made while holding the existing remote-action exclusion for the
remote/local action boundary; a stale, missing, conflicting, or abandoned
fence never permits mutation. AutoCycle only observes and enforces this fence;
it does not own its lifecycle.

The receiver keeps the durable fence active while it releases the remote
exclusion for the sole LEGACY sync reader to capture the bounded observation.
The fence is not closed until that observation is captured and linked, or the
coordinator records the bounded observation as unavailable and abandons the
run.

The receiver must release all local resources in a `finally` path and exit
after the single request. It must not start, stop, or restart AutoCycle,
continuous sync, the watchdog, or any P4 process.

## 3. Request contract

IS3B2 already sends exactly four fields. LCR1 does not version or widen that
wire protocol. The receiver input remains exactly this canonical payload:

```json
{
  "pre_send_identity": "...",
  "canary_idempotency_key": "...",
  "envelope_fingerprint": "...",
  "contract_version": "is3b2.v1"
}
```

`pre_send_identity` is the primary dedupe identity. It identifies one intended
send, but is not itself a remote command. `contract_version` is the exact
IS3B2 transport constant `is3b2.v1`; a numeric `1` is not a valid receiver
value. `envelope_fingerprint` is an opaque producer value. The receiver must
not pretend to recompute it from the four fields.

Before IS3B2 sends the four-field payload, the LEGACY canary
authorization/fence coordinator creates the immutable authorization artifact
in the canonical LEGACY store. It independently validates the exact target,
allowed operation, requested state, binding, fence, and pre-operation
observation floor, then commits the artifact before the send is dispatchable.
The coordinator is the sole artifact writer; the receiver has read-only access
and cannot create, repair, or replace an artifact. The P4/IS3B2 sender is only
the transport producer and cannot bootstrap authorization from the payload.
Until this artifact exists, the receiver rejects the request before remote I/O.

The operation and target are supplied through one immutable LEGACY-owned
authorization artifact, keyed by `pre_send_identity`. This is the chosen
compatibility mechanism; LCR1 does not change the IS3B2 protocol. The artifact
is created before the send and contains at least:

```json
{
  "pre_send_identity": "...",
  "canary_idempotency_key": "...",
  "canary_run_id": "...",
  "fence_identity": "...",
  "source_identity_ref": "non-secret LEGACY source identity",
  "artifact_producer_identity": "non-secret LEGACY coordinator identity",
  "contract_version": "is3b2.v1",
  "operation_kind": "group_on",
  "target_ref": "one exact client identity",
  "requested_state": "running",
  "pre_operation_observation_generation_floor": 0,
  "envelope_fingerprint": "...",
  "authorization_artifact_fingerprint": "..."
}
```

The artifact is the only allowed mapping from the four-field IS3B2 payload to
`operation_kind` and `target_ref`. The receiver must load it by the exact
`pre_send_identity` and require exact equality for all four supplied values,
including the string `is3b2.v1` and the opaque `envelope_fingerprint`. It must
also verify the artifact's canonical-store provenance, immutable producer
identity, current run/fence, and artifact fingerprint. This is equality and
trust-path validation, not receiver-side recomputation of the envelope
fingerprint. It must not look up operation or target data from
master/config/materialized files ad hoc. A missing, duplicated, expired,
producer-invalid, or conflicting artifact fails closed before remote I/O.

The receiver must reject before any remote byte leaves when:

- a required field is missing, malformed, or duplicated;
- the operation is outside `group_on`;
- the target is not exactly one approved non-fixed target;
- the identity, binding, or desired-state check fails;
- the envelope fingerprint does not match the canonical payload;
- the authorization artifact is missing or does not match the identity,
  fingerprint, contract version, producer, run, fence, operation, or target;
- AutoCycle quiesce/fence evidence is absent;
- a competing remote owner is present.

## 4. Receipt state machine

The receipt is durable LEGACY-owned state keyed by `pre_send_identity`.
`envelope_fingerprint` and the identity binding are immutable after insertion.
`pre_send_identity` is the primary key (or an equivalent UNIQUE constraint) in
the receipt store. The receipt also binds `canary_run_id`, `fence_identity`,
`source_identity_ref`, `artifact_producer_identity`, `contract_version`,
`authorization_artifact_fingerprint`,
`pre_operation_observation_generation_floor`, and the exact target. These
values cannot be changed by a replay.
The minimum externally visible outcome set is:

```text
accepted
applied
not_applied_proven
unknown
```

`accepted` is written before the legacy primitive can be invoked. It proves
acceptance and the immutable request binding, not remote success.

Terminal evidence fields are append-only and are written only after the
`dispatching` boundary has been entered. An `applied` receipt is not
evidence-ready until the coordinator appends
`post_dispatch_observation_boundary_id` and
`post_dispatch_observation_generation_floor`; a non-applied terminal state
carries the explicit reason and no success boundary. These fields are
terminal-CAS data, not permission to change the immutable request binding.

The receiver must enforce these transitions:

```text
absent -> accepted -> dispatching -> applied
                      |            |-> unknown
                      |-> not_applied_proven
                      |-> unknown
```

The following replay rules are mandatory:

| Existing receipt | Same fingerprint | Different fingerprint |
|---|---|---|
| `accepted` | return the stored receipt; do not invoke mutation again | fail closed; no mutation |
| `dispatching` | return the stored receipt; no mutation | fail closed; no mutation |
| `applied` | return the stored receipt; no mutation | fail closed; no mutation |
| `not_applied_proven` | return the stored receipt; no mutation | fail closed; no mutation |
| `unknown` | return the stored receipt; no mutation | fail closed; no mutation |

There is no receiver retry. A process crash, timeout, connection reset,
missing acknowledgement, or ambiguous response after mutation may have begun
must result in `unknown` or leave a non-dispatchable receipt that is treated as
ambiguous. It must never cause a blind replay.

### Atomic winner and terminal CAS

The pre-send receipt transaction is the cross-process winner election:

1. Start one database transaction and atomically insert `accepted` with the
   primary key `pre_send_identity` and all immutable binding fields.
2. Only the process whose insert commits successfully becomes the mutation
   winner. It commits the accepted row before opening the remote connection.
3. A concurrent unique/primary-key loser must read the committed receipt and
   return replay without invoking the primitive. If the winner row is not yet
   visible, the loser fails closed without remote I/O; it does not become a
   second winner through a retry.
4. The winner marks the receipt `dispatching` with a CAS before invoking the
   primitive. That marker is the durable boundary after which a crash is
   ambiguous.
5. Terminal writes use CAS over the exact identity, fingerprint, run/fence
   identity, and expected state. An `applied` terminal write must be followed
   by the coordinator's post-dispatch observation-boundary handoff; a terminal
   update affecting zero rows is a replay/conflict outcome, never permission to
   invoke mutation again.

An `accepted` receipt with no committed `dispatching` marker proves the
primitive was never entered and may be classified `not_applied_proven` during
explicit evidence handling. A `dispatching` receipt without a terminal result
is always `unknown`.

`not_applied_proven` is allowed only when durable evidence proves the legacy
primitive was not entered and no remote side effect could have started. A stale
snapshot, absent receipt, or receiver timeout is not proof of non-application.

Conflicting identity/fingerprint input is a permanent fail-closed conflict for
that request. It must not overwrite, repair, or delete the original receipt.

## 5. Mutation primitive extraction boundary

The current legacy implementation is embedded in `tools/AutoCycle.ps1`:

- `Toggle-Rows` at line 402 is the batch wrapper;
- `Stop-RowsLocal` at line 230 is the OFF wrapper;
- the ON path emits `row_toggle` at line 422 and has a `scr_start` fallback at
  line 433;
- AutoCycle invokes the wrapper from `Execute-Slot` and `Resolve-Day`, and those
  paths also call `Kill-Local` directly for OFF cleanup.

LCR2 may extract the existing single-target wire/action portion as a shared
legacy primitive, but extraction must preserve the current wire payload,
identity validation, fixed-client protection, orphan blocking, and epoch
checking. It must not invent a new remote operation.

The extracted primitive contract is:

- one target per invocation;
- one legacy action invocation;
- no internal retry;
- no internal polling loop;
- no batch expansion;
- no schedule decision;
- no receipt ownership;
- no P4 imports or runtime-owner transition.

### One-target operation mapping

`group_on` is a compatibility label only. It does not mean a group and never
expands to a client list; the immutable authorization artifact maps it to
exactly one `target_ref`.

The LCR1 receiver uses exactly one existing `row_toggle` remote action for
`group_on`. It must not inherit AutoCycle's `scr_start` fallback, menu path,
batch expansion, or verification retry. If the target's precondition does not
permit that `row_toggle` action, the receiver fails closed before mutation. If
the action does not reach the requested materialized state, the result is
evidence-incomplete/`unknown`; a second remote action, local kill, or other
corrective action is forbidden.

`group_off` is not an LCR1 operation. Discovery shows that its existing
`Stop-RowsLocal` behavior is a single-target sequence consisting of a
conditional `row_toggle`, local `Kill-Local`, and verification/retry behavior.
LCR1 therefore does not invent an OFF primitive or claim that OFF is equivalent
to `row_toggle`; a future separate contract must preserve that sequence or
explicitly approve a narrower action. Until then, `group_off` is rejected
before remote I/O.

AutoCycle may retain its existing batch, retry, and verification wrapper around
the extracted primitive. The receiver may not call that wrapper and may not
inherit its retry/fallback behavior. The receiver calls the extracted
single-target primitive once, after its own receipt and ownership gates pass.

An implementation that cannot extract the existing primitive without changing
AutoCycle behavior is an LCR1/LCR2 blocker. Creating a parallel mutation
implementation is not an acceptable workaround.

## 6. Materialized evidence contract

The existing LEGACY reader remains the sole source of remote observations:
`continuous_sync_remote.ps1` reads `scr_list_res` and materializes local state.
LCR2 must extend that materialization rather than open another WebSocket reader.

Each materialized snapshot must carry at least:

```json
{
  "snapshot_id": "stable non-secret snapshot identity",
  "observation_generation": 0,
  "observation_boundary_id": "post-dispatch boundary identity",
  "captured_at": "ISO-8601 timestamp",
  "source_identity_ref": "non-secret LEGACY source/session reference",
  "canary_run_id": "...",
  "fence_identity": "...",
  "targets": {
    "client_7": {
      "client_id": "client_7",
      "observed_state": "running|offline|unknown"
    }
  }
}
```

`source_identity_ref` must not contain a token, cookie, password, or raw session
value. The snapshot must be atomically materialized with a target record that
can be read without opening a second remote session.

`observation_generation` is a durable, monotonically increasing materializer
sequence (or an equivalent durable ordering token). It is the freshness proof;
`captured_at` is audit metadata only. The snapshot's run and fence identity
must match the active canary run, not merely the source process. A snapshot is
not canary evidence merely because its generation is greater than the
pre-operation floor.

After the primitive has been entered and the receipt has reached its terminal
CAS, the LEGACY coordinator creates one immutable post-dispatch observation
boundary. The boundary records the terminal receipt identity, run/fence and a
`post_dispatch_observation_generation_floor` allocated after that terminal
CAS. The sole materializer must acknowledge that boundary and publish the
next matching snapshot with the boundary ID and a strictly greater generation.
This ordering is the post-dispatch proof; a generation that existed before the
primitive or before terminal CAS cannot satisfy it. The receiver performs no
polling loop or second read session: it receives only the bounded observation
handoff defined by the existing LEGACY reader, then the coordinator closes or
abandons the fence.

`/api/status` and `/api/sync_status` may provide freshness/readiness context,
but they are not by themselves target-state evidence. A status response that
contains only counts, worker metadata, or a stale `lastUpdated` cannot prove a
canary result.

## 7. Reconciliation predicate

For an `applied` receipt, the exact target must be present in a materialized
snapshot whose durable observation ordering token is strictly later than the
receipt's post-dispatch boundary floor, whose `observation_boundary_id`
matches the receipt's immutable boundary, and whose run/fence identity matches
the receipt. Wall-clock comparison alone is insufficient.

The only positive predicates are:

```text
succeeded
  = receipt.applied
  + receipt.post_dispatch_observation_boundary_id != null
  + snapshot.observation_generation > receipt.post_dispatch_observation_generation_floor
  + snapshot.observation_boundary_id == receipt.post_dispatch_observation_boundary_id
  + snapshot.canary_run_id == receipt.canary_run_id
  + snapshot.fence_identity == receipt.fence_identity
  + snapshot.source_identity_ref == receipt.source_identity_ref
  + exact target identity matches
  + observed_state == requested desired state

`captured_at` and `completed_at` remain audit fields, but cannot independently
prove ordering.

failed
  = receipt.not_applied_proven
  + durable evidence proves the legacy primitive was never entered
  + no remote side effect could have started
```

Every other case is `unknown`, including:

- receipt absent or still only `accepted`;
- receipt is still `dispatching`, or has no post-dispatch boundary;
- timeout, reset, crash, or lost response after dispatch could have begun;
- snapshot missing, stale, delayed, or from a conflicting source identity;
- snapshot ordering token is not strictly later than the post-dispatch boundary floor;
- snapshot was only greater than the pre-operation floor but lacks the
  post-dispatch boundary link;
- snapshot run or fence identity does not match the receipt;
- target identity mismatch;
- snapshot state contradicts the receipt;
- target has not changed yet;
- `/api/status` or `/api/sync_status` is unavailable or incomplete.

An unchanged or stale snapshot is never sufficient to infer `failed` after a
mutation may have started. No reconciliation result creates a retry or a new
corrective mutation.

## 8. Explicit no-go rules

LCR1 and LCR2 must not:

- make AutoCycle and the receiver co-own remote mutation;
- replace the LEGACY owner with P4 or set `ACTIVE`;
- call `Toggle-Rows` as a receiver shortcut;
- call `remote_toggle_rows.ps1` as a new generic receiver API;
- add a daemon, queue, worker, scheduler, polling loop, or retry loop;
- open a second remote read WebSocket;
- infer failure from elapsed time or unchanged state;
- revive, resend, or repair an `unknown` receipt automatically;
- persist credentials, remote tokens, or raw session identifiers;
- begin IS3C orchestration before LCR2 acceptance is complete.

## 9. Approval gate

LCR1 is approved only when reviewers agree that the receiver owner, quiesce /
fence proof, extracted primitive boundary, immutable receipt semantics, and
materialized evidence predicate are implementable without new remote semantics.

Approval authorizes LCR2 design/implementation only. It does not authorize
production mutation, ACTIVE, scheduler handoff, P4 Remote I/O ownership, or
IS3C.
