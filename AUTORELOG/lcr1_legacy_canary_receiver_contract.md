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

The allowed first operation family is deliberately narrow:

```text
group_on | group_off
```

The first implementation may target exactly one non-fixed, identity-validated
client per invocation. `alwaysrun`, `sync`, `reconcile`, generic refresh,
multi-target groups, and schedule decisions are outside this contract.

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

The receiver must release all local resources in a `finally` path and exit
after the single request. It must not start, stop, or restart AutoCycle,
continuous sync, the watchdog, or any P4 process.

## 3. Request contract

The receiver input is a canonical envelope containing exactly the fields needed
to bind a single canary action:

```json
{
  "contract_version": 1,
  "pre_send_identity": "...",
  "canary_idempotency_key": "...",
  "envelope_fingerprint": "...",
  "operation_kind": "group_on|group_off",
  "target_ref": "..."
}
```

`pre_send_identity` is the primary dedupe identity. It identifies one intended
send, but is not itself a remote command. `envelope_fingerprint` is immutable
for that identity and covers the complete canonical request, including the
operation and exact target reference. Credentials, session tokens, raw URLs,
or unbounded client lists are forbidden in the envelope.

The receiver must reject before any remote byte leaves when:

- a required field is missing, malformed, or duplicated;
- the operation is outside `group_on|group_off`;
- the target is not exactly one approved non-fixed target;
- the identity, binding, or desired-state check fails;
- the envelope fingerprint does not match the canonical payload;
- AutoCycle quiesce/fence evidence is absent;
- a competing remote owner is present.

## 4. Receipt state machine

The receipt is durable LEGACY-owned state keyed by `pre_send_identity`.
`envelope_fingerprint` and the identity binding are immutable after insertion.
The minimum externally visible outcome set is:

```text
accepted
applied
not_applied_proven
unknown
```

`accepted` is written before the legacy primitive can be invoked. It proves
acceptance and the immutable request binding, not remote success.

The receiver must enforce these transitions:

```text
absent -> accepted -> applied
                  |-> not_applied_proven
                  |-> unknown
```

The following replay rules are mandatory:

| Existing receipt | Same fingerprint | Different fingerprint |
|---|---|---|
| `accepted` | return the stored receipt; do not invoke mutation again | fail closed; no mutation |
| `applied` | return the stored receipt; no mutation | fail closed; no mutation |
| `not_applied_proven` | return the stored receipt; no mutation | fail closed; no mutation |
| `unknown` | return the stored receipt; no mutation | fail closed; no mutation |

There is no receiver retry. A process crash, timeout, connection reset,
missing acknowledgement, or ambiguous response after mutation may have begun
must result in `unknown` or leave a non-dispatchable receipt that is treated as
ambiguous. It must never cause a blind replay.

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
- AutoCycle invokes the wrapper from `Execute-Slot` and `Resolve-Day`.

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
  "captured_at": "ISO-8601 timestamp",
  "source_identity_ref": "non-secret LEGACY source/session reference",
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

`/api/status` and `/api/sync_status` may provide freshness/readiness context,
but they are not by themselves target-state evidence. A status response that
contains only counts, worker metadata, or a stale `lastUpdated` cannot prove a
canary result.

## 7. Reconciliation predicate

For a receipt with `completed_at`, the exact target must be present in a fresh
materialized snapshot captured after that completion boundary.

The only positive predicates are:

```text
succeeded
  = receipt.applied
  + snapshot.captured_at > receipt.completed_at
  + exact target identity matches
  + observed_state == requested desired state

failed
  = receipt.not_applied_proven
  + durable evidence proves the legacy primitive was never entered
  + no remote side effect could have started
```

Every other case is `unknown`, including:

- receipt absent or still only `accepted`;
- timeout, reset, crash, or lost response after dispatch could have begun;
- snapshot missing, stale, delayed, or from a conflicting source identity;
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
