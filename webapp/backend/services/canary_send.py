"""IS3B2 single-shot canary send boundary.

Manual, one-identity, one-mutation-at-most dispatch for a single armed
IS3B1 canary candidate. This is not a dispatcher: no loop, no queue, no
worker, no scheduler, no Flask trigger, no automatic second attempt.

Durable order per invocation:

1. Authority-first guard and lineage validation, then replay classification.
2. Durable pre-send intent commit (identity = pre_send_identity, UNIQUE).
3. Final fresh-authority guard with the SQLite transaction closed.
4. At most one network transmission with no open transaction.
5. Durable outcome recording: success only on proved success, else unknown.

A second invocation for the same identity never transmits: it replays the
durable state or fails closed. Ambiguity is recorded as unknown, never as
a reason to transmit again.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.authority_bound_targets import AuthorityBoundTargetService
from services.binding_authority import _correlation_id, _reject
from services.binding_authority_types import BindingLease
from services.canary_arming import CanaryArmingService
from services.canary_send_transport import (
    CANARY_SEND_CONTRACT_VERSION,
    CanaryDestinationRefused,
    CanarySendAmbiguous,
    SingleShotCanaryTransport,
)
from services.dispatch_intent_store import DispatchIntentStore
from services.shadow_dispatch_envelope import snapshot_fingerprint

_COMMITTED_POLL_ROUNDS: int = 40
_COMMITTED_POLL_INTERVAL_SECONDS: float = 0.05


def _send_body(candidate) -> str:
    return json.dumps(
        {
            "pre_send_identity": candidate["pre_send_identity"],
            "canary_idempotency_key": candidate["canary_idempotency_key"],
            "envelope_fingerprint": candidate["envelope_fingerprint"],
            "contract_version": CANARY_SEND_CONTRACT_VERSION,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )


class CanarySingleShotService:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        coordinator,
        arming: CanaryArmingService | None = None,
        committed_wait_seconds: float = 2.0,
    ):
        self.operational = operational
        self.coordinator = coordinator
        self.store = DispatchIntentStore(operational)
        self.arming = arming or CanaryArmingService(
            portable_path, operational, coordinator
        )
        self.shadow = self.arming.shadow
        self.committed_wait_seconds = committed_wait_seconds

    def send(
        self,
        canary_candidate_id: str,
        lease: BindingLease,
        owner_id: str,
        transport: SingleShotCanaryTransport,
        request_id: str | None = None,
    ):
        correlation_id = _correlation_id(request_id)
        AuthorityBoundTargetService._validate_reference(
            canary_candidate_id, "canary_candidate_id", correlation_id
        )
        AuthorityBoundTargetService._validate_reference(
            owner_id, "owner_id", correlation_id
        )
        if type(transport) is not SingleShotCanaryTransport:
            raise _reject(correlation_id, "canary_send_transport_invalid")
        if transport.contract_version != CANARY_SEND_CONTRACT_VERSION:
            raise _reject(correlation_id, "canary_send_contract_mismatch")

        try:
            with self.operational.transaction():
                candidate = self._candidate(canary_candidate_id)
                if candidate is None:
                    raise _reject(correlation_id, "canary_candidate_not_found")
                shadow, intent, execution, attempt, target = self._lineage(
                    candidate, correlation_id
                )
                snapshot = self.shadow._guard_current(
                    intent, lease, owner_id, correlation_id
                )
                self.arming._require_candidate_lineage(
                    candidate, shadow, intent, execution, attempt, target,
                    snapshot_fingerprint(snapshot), correlation_id,
                )
                existing = self._send_intent(candidate["pre_send_identity"])
                if existing is None:
                    if candidate["state"] != "armed":
                        raise _reject(correlation_id, "canary_send_not_actionable")
                    self._insert_intent(candidate, transport, correlation_id)
        except _SendContended:
            return self._replay_outside(
                canary_candidate_id, lease, owner_id, correlation_id
            )
        if existing is not None:
            return self._replay_outside(
                canary_candidate_id, lease, owner_id, correlation_id
            )
        try:
            with self.operational.transaction():
                self._final_guard(candidate, lease, owner_id, correlation_id)
        except Exception:
            self._guarded_mark_unknown(
                candidate["canary_candidate_id"], lease, owner_id,
                "authority_stale_before_network", correlation_id,
            )
            raise
        if self.operational.connection.in_transaction:
            raise RuntimeError("network boundary entered with open transaction")
        try:
            status_class, response_fingerprint = transport.single_post(
                _send_body(candidate),
                candidate["canary_idempotency_key"],
                candidate["pre_send_identity"],
            )
        except CanaryDestinationRefused:
            self._guarded_mark_unknown(
                candidate["canary_candidate_id"], lease, owner_id,
                "destination_refused", correlation_id,
            )
            raise
        except CanarySendAmbiguous as ambiguous:
            self._record_ambiguous_outcome(
                candidate, lease, owner_id, ambiguous.status_class, correlation_id
            )
            return self._result(
                self._send_intent(candidate["pre_send_identity"]), "unknown"
            )
        self._record_success_outcome(
            candidate, status_class, response_fingerprint, correlation_id
        )
        return self._result(
            self._send_intent(candidate["pre_send_identity"]), "succeeded"
        )

    def _lineage(self, candidate, correlation_id):
        shadow = self.arming._shadow(candidate["shadow_evaluation_id"])
        if shadow is None:
            raise _reject(correlation_id, "canary_shadow_not_found")
        intent = self.store.intent(shadow["intent_id"])
        if intent is None:
            raise _reject(correlation_id, "intent_not_found")
        execution, attempt, target = self.store.execution_context(
            intent["execution_id"], correlation_id
        )
        return shadow, intent, execution, attempt, target

    def _final_guard(self, candidate, lease, owner_id, correlation_id):
        fresh_candidate = self._candidate(candidate["canary_candidate_id"])
        if fresh_candidate is None:
            raise _reject(correlation_id, "canary_candidate_not_found")
        shadow, intent, execution, attempt, target = self._lineage(
            fresh_candidate, correlation_id
        )
        snapshot = self.shadow._guard_current(intent, lease, owner_id, correlation_id)
        self.arming._require_candidate_lineage(
            fresh_candidate, shadow, intent, execution, attempt, target,
            snapshot_fingerprint(snapshot), correlation_id,
        )
        self.arming._require_upstream_stable(shadow, intent, correlation_id)
        if fresh_candidate["state"] != "armed":
            raise _reject(correlation_id, "canary_send_not_actionable")

    def _replay_outside(
        self, canary_candidate_id, lease, owner_id, correlation_id
    ):
        current = self._guarded_intent_state(
            canary_candidate_id, lease, owner_id, correlation_id
        )
        if current["state"] != "committed":
            return self._result(current, "idempotent_replay")
        self._resolve_committed(
            current, canary_candidate_id, lease, owner_id, correlation_id
        )
        final = self._guarded_intent_state(
            canary_candidate_id, lease, owner_id, correlation_id
        )
        return self._result(final, "idempotent_replay")

    def _guarded_intent_state(
        self, canary_candidate_id, lease, owner_id, correlation_id
    ):
        with self.operational.transaction():
            candidate = self._candidate(canary_candidate_id)
            if candidate is None:
                raise _reject(correlation_id, "canary_candidate_not_found")
            shadow, intent, execution, attempt, target = self._lineage(
                candidate, correlation_id
            )
            snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self.arming._require_candidate_lineage(
                candidate, shadow, intent, execution, attempt, target,
                snapshot_fingerprint(snapshot), correlation_id,
            )
            current = self._send_intent(candidate["pre_send_identity"])
            if current is None:
                raise _reject(correlation_id, "canary_send_intent_missing")
            return current

    def _resolve_committed(
        self, existing, canary_candidate_id, lease, owner_id, correlation_id
    ):
        identity = existing["pre_send_identity"]
        rounds = max(
            1,
            int(self.committed_wait_seconds / _COMMITTED_POLL_INTERVAL_SECONDS),
        )
        rounds = min(rounds, _COMMITTED_POLL_ROUNDS)
        current = existing
        for _ in range(rounds):
            if current["state"] != "committed":
                return current
            time.sleep(_COMMITTED_POLL_INTERVAL_SECONDS)
            current = self._send_intent(identity)
            if current is None:
                raise _reject(correlation_id, "canary_send_intent_missing")
        if current["state"] != "committed":
            return current
        return self._guarded_mark_unknown(
            canary_candidate_id, lease, owner_id,
            "committed_unresolved", correlation_id,
        )

    def _insert_intent(self, candidate, transport, correlation_id):
        now = self.coordinator._now("canary").isoformat()
        try:
            self.operational.connection.execute(
                "INSERT INTO authority_bound_canary_send_intents ("
                "pre_send_identity, canary_candidate_id, shadow_evaluation_id, "
                "intent_id, execution_id, attempt_id, target_id, job_id, "
                "canary_idempotency_key, envelope_fingerprint, destination_ref, "
                "host_id, profile_id, binding_generation, verified_identity_ref, "
                "verified_identity_revision, authority_epoch, fence_counter, "
                "owner_id, lease_id, transport_contract_version, state, "
                "created_at, committed_at) VALUES ("
                + ",".join("?" for _ in range(24)) + ")",
                (
                    candidate["pre_send_identity"],
                    candidate["canary_candidate_id"],
                    candidate["shadow_evaluation_id"], candidate["intent_id"],
                    candidate["execution_id"], candidate["attempt_id"],
                    candidate["target_id"], candidate["job_id"],
                    candidate["canary_idempotency_key"],
                    candidate["envelope_fingerprint"],
                    transport.destination_url, candidate["host_id"],
                    candidate["profile_id"], candidate["binding_generation"],
                    candidate["verified_identity_ref"],
                    candidate["verified_identity_revision"],
                    candidate["authority_epoch"], candidate["fence_counter"],
                    candidate["owner_id"], candidate["lease_id"],
                    transport.contract_version, "committed", now, now,
                ),
            )
        except sqlite3.IntegrityError:
            existing = self._send_intent(candidate["pre_send_identity"])
            if existing is None:
                raise _reject(correlation_id, "canary_send_identity_conflict")
            raise _SendContended(existing) from None

    def _guarded_mark_unknown(
        self, canary_candidate_id, lease, owner_id, reason_class, correlation_id
    ):
        with self.operational.transaction():
            candidate = self._candidate(canary_candidate_id)
            if candidate is None:
                raise _reject(correlation_id, "canary_candidate_not_found")
            shadow, intent, execution, attempt, target = self._lineage(
                candidate, correlation_id
            )
            snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self.arming._require_candidate_lineage(
                candidate, shadow, intent, execution, attempt, target,
                snapshot_fingerprint(snapshot), correlation_id,
            )
            updated = self.operational.connection.execute(
                "UPDATE authority_bound_canary_send_intents SET state='unknown', "
                "reason_class=?, unknown_at=? "
                "WHERE pre_send_identity=? AND state='committed'",
                (
                    reason_class,
                    self.coordinator._now("canary").isoformat(),
                    candidate["pre_send_identity"],
                ),
            ).rowcount
            if updated != 1:
                current = self._send_intent(candidate["pre_send_identity"])
                if current is None:
                    raise _reject(correlation_id, "canary_send_intent_missing")
                return current
            return self._send_intent(candidate["pre_send_identity"])

    def _record_ambiguous_outcome(
        self, candidate, lease, owner_id, status_class, correlation_id
    ):
        identity = candidate["pre_send_identity"]
        with self.operational.transaction():
            updated = self.operational.connection.execute(
                "UPDATE authority_bound_canary_send_intents SET state='unknown', "
                "status_class=?, reason_class=?, unknown_at=? "
                "WHERE pre_send_identity=? AND (state='committed' OR "
                "(state='unknown' AND reason_class='committed_unresolved'))",
                (
                    status_class,
                    status_class,
                    self.coordinator._now("canary").isoformat(),
                    identity,
                ),
            ).rowcount
            if updated != 1:
                raise _reject(correlation_id, "canary_send_fenced")
        self.arming.record_ambiguous(
            candidate["canary_candidate_id"], lease, owner_id,
            f"send-outcome-{status_class[:32]}", "ambiguous_boundary",
            candidate["pre_send_identity"],
        )

    def _record_success_outcome(
        self, candidate, status_class, response_fingerprint, correlation_id
    ):
        with self.operational.transaction():
            updated = self.operational.connection.execute(
                "UPDATE authority_bound_canary_send_intents SET state='succeeded', "
                "status_class=?, response_fingerprint=?, reason_class=?, "
                "succeeded_at=? WHERE pre_send_identity=? AND (state='committed' "
                "OR (state='unknown' AND reason_class='committed_unresolved'))",
                (
                    status_class, response_fingerprint, "single_shot_succeeded",
                    self.coordinator._now("canary").isoformat(),
                    candidate["pre_send_identity"],
                ),
            ).rowcount
            if updated != 1:
                raise _reject(correlation_id, "canary_send_fenced")

    def _candidate(self, canary_candidate_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?", (canary_candidate_id,),
        ).fetchone()

    def _send_intent(self, pre_send_identity):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_canary_send_intents "
            "WHERE pre_send_identity=?", (pre_send_identity,),
        ).fetchone()

    @staticmethod
    def _result(row, outcome):
        result = dict(row)
        result["outcome"] = outcome
        return result


class _SendContended(Exception):
    """Lost the single-winner insert race; the caller must replay durable state."""

    def __init__(self, existing):
        super().__init__("send identity already committed")
        self.existing = existing
