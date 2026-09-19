# LCR1 Acceptance Matrix

Status: design-only acceptance gate. No row in this matrix is claimed as
implemented by this branch.

Baseline: `main@d0ed1a6b68083a9b2dacd71d66931d417398c118`.
Scope: receiver/evidence contract only; no live mutation is authorized.

## 1. Review and scope gates

| ID | Area | Acceptance condition | Evidence required | Failure result |
|---|---|---|---|---|
| LCR1-001 | Baseline | LCR1 starts from merged IS3B2 main and leaves `p4/is3b2-single-shot-canary` and `p4/is3c-observed-integration` unchanged | Exact base/head and clean source-branch status | Reject LCR1 |
| LCR1-002 | Scope | Branch contains only contract/matrix documentation until LCR1 approval | Diff contains no receiver, AutoCycle, materializer, route, scheduler, or worker code | Reject LCR1 |
| LCR1-003 | Runtime owner | Runtime owner remains `LEGACY`; no ACTIVE or ownership transition | Static diff scan and runtime-owner statement | Reject LCR1 |
| LCR1-004 | Operation allowlist | Initial family is only one-target `group_on` / `group_off` | Contract review | Reject any broader family |
| LCR1-005 | Approval boundary | LCR1 approval authorizes LCR2 only, not live mutation or IS3C | Signed review decision records | Treat as NO-GO |

## 2. Ownership and quiesce gates

| ID | Area | Acceptance condition | Evidence required | Failure result |
|---|---|---|---|---|
| LCR1-010 | Owner | Receiver is a manual one-shot LEGACY process, not a P4 worker | Process contract and lifecycle test plan | Reject receiver design |
| LCR1-011 | Single request | Process accepts exactly one request and exits after terminal receipt handling | Invocation contract and static scan | Reject receiver design |
| LCR1-012 | Quiesce | Receiver refuses mutation unless AutoCycle is observably quiesced and fenced | Concrete LEGACY fence evidence, not only process absence | Zero mutation |
| LCR1-013 | Competing owner | A live competing LEGACY mutator causes fail-closed refusal | Concurrent-owner test with zero remote actions | Zero mutation |
| LCR1-014 | Mutex | `Local\\AutoGhostStory_RemoteWs` is held for the complete remote action boundary | Lock acquisition/release trace | Zero mutation |
| LCR1-015 | Mutex semantics | Mutex is not treated as proof of ownership without the quiesce/fence check | Negative test with mutex-only evidence | Zero mutation |
| LCR1-016 | Process cleanup | Receiver releases connection, mutex, and local resources in all exit paths | Success, error, timeout, and crash cleanup evidence | Mark run unknown; no retry |
| LCR1-017 | Fence lifetime | Durable `canary_run_id` / `fence_identity` remains active through materialized observation and run closure | Fence lifecycle trace proving AutoCycle cannot resume before observation closure | Zero mutation / reject attribution |
| LCR1-018 | Attribution binding | Receipt and snapshot carry the same run, fence, and non-secret source identity | Cross-artifact lineage test | Result unknown |

## 3. Request and receipt gates

| ID | Area | Acceptance condition | Evidence required | Failure result |
|---|---|---|---|---|
| LCR1-020 | Canonical input | Four-field IS3B2 payload is paired with one immutable LEGACY authorization artifact binding exactly one target and allowed operation | Canonical serialization, artifact, and validation tests | Reject before remote I/O |
| LCR1-020A | IS3B2 protocol | Receiver accepts exactly IS3B2's four fields: `pre_send_identity`, `canary_idempotency_key`, `envelope_fingerprint`, `contract_version` | Exact request-shape test | Reject widened/ad hoc protocol |
| LCR1-020B | Authorization mapping | Immutable LEGACY artifact keyed by `pre_send_identity` supplies operation, target, run, fence, and requested state | Artifact lookup, fingerprint, and conflict tests | Reject before remote I/O |
| LCR1-021 | Pre-send receipt | `accepted` is durably persisted before mutation can begin | Ordered crash-seam test | Zero mutation or unknown; never blind retry |
| LCR1-022 | Same replay | Same identity and same fingerprint returns stored receipt and performs zero mutation | Replay test for every receipt state | Zero additional mutations |
| LCR1-023 | Conflict replay | Same identity with a different fingerprint fails closed without overwrite | Conflict test | Zero mutation |
| LCR1-024 | Identity binding | Operation and exact target are covered by the immutable fingerprint | Tamper test over each request field | Conflict/fail-closed |
| LCR1-025 | One target | Batch, group expansion, and multiple target references are rejected | Input boundary tests | Zero mutation |
| LCR1-026 | No retry | Receiver has no retry/backoff/polling behavior after dispatch may begin | Static scan plus timeout/reset tests | Unknown, no resend |
| LCR1-026A | Atomic winner | Primary key/UNIQUE `pre_send_identity` elects exactly one insert winner; only that winner may invoke the primitive | Two receiver processes, same identity, synchronized race | Exactly one invocation |
| LCR1-026B | Loser replay | Unique-insert loser reads the winner receipt and performs zero mutation | Two-process race with receipt replay assertion | Zero loser invocation |
| LCR1-026C | Terminal CAS | Terminal update requires exact identity, fingerprint, run/fence, and expected state | CAS race and zero-row update tests | No second invocation |
| LCR1-027 | Applied terminal | `applied` is written only from a positive legacy primitive result | Primitive-result fixture | No positive result without receipt |
| LCR1-028 | Not-applied terminal | `not_applied_proven` requires proof the primitive was never entered | Pre-dispatch crash/proof test | Otherwise unknown |
| LCR1-029 | Ambiguous terminal | Timeout, reset, lost response, or post-dispatch crash yields `unknown` | Fault injection at each boundary | No retry |
| LCR1-030 | Receipt secrets | Receipt stores no credential, token, cookie, raw URL, or raw session value | Byte scan and schema review | Reject schema |

## 4. Primitive extraction gates

| ID | Area | Acceptance condition | Evidence required | Failure result |
|---|---|---|---|---|
| LCR1-040 | Source | Extraction is from `tools/AutoCycle.ps1` `Toggle-Rows` action logic, not a parallel implementation | Diff and provenance review | Reject implementation |
| LCR1-041 | Single action | Extracted primitive invokes one target action once | Invocation counter and wire capture | Reject implementation |
| LCR1-042 | Wire compatibility | Extracted one-target primitive preserves the existing `row_toggle` payload and epoch behavior; AutoCycle-only `scr_start` fallback remains outside receiver scope | Golden payload tests | Reject implementation |
| LCR1-042A | One-target mapping | `group_on` and `group_off` are labels for one exact client, not group expansion | Artifact and target-selection tests | Reject implementation |
| LCR1-042B | Action selection | Receiver uses exactly one `row_toggle`; it never inherits `scr_start` fallback, menu action, local kill, or second remote action | Static call graph and one-action wire trace | Reject implementation |
| LCR1-043 | Safety compatibility | Fixed-client, orphan, identity, and epoch protections remain intact | Existing plus extraction regression tests | Zero mutation |
| LCR1-044 | Wrapper compatibility | AutoCycle retains its existing batch/retry/verification wrapper semantics | AutoCycle regression evidence | Reject extraction |
| LCR1-045 | Receiver isolation | Receiver does not call the batch wrapper, fallback menu path, or generic actuator | Static call-graph scan | Reject implementation |

## 5. Materialized evidence gates

| ID | Area | Acceptance condition | Evidence required | Failure result |
|---|---|---|---|---|
| LCR1-050 | Sole reader | `continuous_sync_remote.ps1` remains the only LEGACY remote observation reader | Process/WebSocket ownership scan | Reject second reader |
| LCR1-051 | Snapshot identity | Snapshot persists non-secret `snapshot_id`, `captured_at`, and `source_identity_ref` | Materialized schema fixture | Evidence unavailable |
| LCR1-051A | Ordering token | Snapshot persists a durable monotonic `observation_generation` or equivalent ordering token | Generation monotonicity and restart tests | Evidence unavailable |
| LCR1-052 | Target record | Snapshot persists exact `client_id` and `observed_state` | Materialized fixture with target lookup | Evidence unavailable |
| LCR1-053 | Atomic materialization | Snapshot metadata and target state are published as one consistent materialized view | Crash/partial-write test | Evidence unavailable |
| LCR1-054 | Freshness | Reconciliation snapshot ordering token is strictly later than the receipt's pre-operation floor | Monotonic ordering test; timestamp is audit-only | Result unknown |
| LCR1-055 | Exact target | Snapshot target identity equals the receipt target identity | Identity binding test | Result unknown |
| LCR1-055A | Snapshot lineage | Snapshot `canary_run_id`, `fence_identity`, and `source_identity_ref` match the receipt | Cross-lineage mismatch test | Result unknown |
| LCR1-056 | Desired state | `succeeded` requires exact observed state equal to requested state | On/off positive fixtures | Result unknown |
| LCR1-057 | Not-applied proof | `failed` requires `not_applied_proven` plus no possible side effect | Negative-side-effect fixture | Result unknown |
| LCR1-058 | Stale state | Unchanged, delayed, missing, or stale state never proves failure after dispatch may begin | Stale snapshot test | Result unknown |
| LCR1-059 | Contradiction | Conflicting receipt/snapshot evidence fails closed | Conflict fixture | Result unknown |
| LCR1-060 | Existing endpoints | `/api/status` and `/api/sync_status` are treated as context only, not target proof | Contract review and negative tests | Reject false proof |
| LCR1-061 | No extra WebSocket | P4/receiver does not open a second read session for reconciliation | Process/network trace | Reject implementation |

## 6. Restart, replay, and ownership fault gates

| ID | Area | Acceptance condition | Evidence required | Failure result |
|---|---|---|---|---|
| LCR1-070 | Crash before primitive | A crash before primitive entry cannot cause an automatic resend | Crash seam and replay test | Zero resend |
| LCR1-071 | Crash after entry | A crash after mutation may begin is `unknown`, never retried | Crash seam and receipt test | Zero resend |
| LCR1-072 | Restart | Restart does not revive an `accepted` or `unknown` request into mutation | Restart/replay test | Zero mutation |
| LCR1-073 | Lease/fence drift | Binding, owner, epoch, counter, or target drift fails closed before mutation | Authority mutation tests | Zero mutation |
| LCR1-074 | Restore | Restored receipt/evidence is historical and cannot grant live mutation authority | Restore/quarantine test | Zero mutation |
| LCR1-075 | Fixed protection | Fixed clients are never selected by the first canary operation family | Target-selection test | Zero mutation |

## 7. Explicit forbidden implementation checks

Each item below must remain absent from LCR2 and later LCR1-derived work:

| ID | Forbidden behavior | Required result |
|---|---|---|
| LCR1-080 | P4 dispatcher, queue, worker, scheduler, or daemon | Static scan fails the change |
| LCR1-081 | AutoCycle restart, takeover, or concurrent ownership | Static/runtime isolation fails the change |
| LCR1-082 | ACTIVE or runtime-owner transition | Static scan fails the change |
| LCR1-083 | Receiver retry, resend, or corrective mutation | Fault test fails the change |
| LCR1-084 | Failure inferred from time or unchanged snapshot | Predicate test fails the change |
| LCR1-085 | New remote WebSocket reader | Ownership test fails the change |
| LCR1-086 | Generic operation or multi-target expansion | Input boundary fails the change |
| LCR1-087 | Secret/session persistence | Byte/schema scan fails the change |

## 8. LCR2 entry gate

LCR2 may start only after every applicable LCR1 row has an approved design
answer and the following artifacts exist:

1. A concrete AutoCycle quiesce/fence proof.
2. A provenance-preserving single-target primitive extraction plan.
3. A receipt schema and immutable conflict behavior.
4. A materialized snapshot schema with target identity and freshness evidence.
5. Fault/replay test fixtures for pre-dispatch, post-dispatch, timeout, reset,
   restart, restore, stale snapshot, and conflicting fingerprint cases.

LCR2 approval does not authorize IS3C. IS3C remains blocked until the receiver
and evidence acceptance rows pass with implementation evidence.
