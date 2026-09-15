from __future__ import annotations

import secrets
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.authority_bound_targets import AuthorityBoundTargetService
from services.authority_execution_context import (
    require_execution_context,
    validate_reference,
)
from services.binding_authority import (
    _correlation_id,
    _reject,
)
from services.binding_authority_types import AuthorityRejected, BindingLease
from services.dispatch_intent_store import DispatchIntentStore
from services.shadow_dispatch_envelope import (
    ShadowEnvelope,
    build_shadow_envelope,
    snapshot_fingerprint,
)
from services.shadow_dispatch_transport import (
    NullShadowTransport,
    RecordingShadowTransport,
)


class DryRunShadowDispatcher:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        coordinator,
    ):
        self.portable_path = Path(portable_path)
        self.operational = operational
        self.coordinator = coordinator
        self.store = DispatchIntentStore(operational)

    def evaluate(
        self,
        intent_id: str,
        lease: BindingLease,
        owner_id: str,
        request_idempotency_key: str,
        transport: NullShadowTransport | RecordingShadowTransport,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        validate_reference(intent_id, "intent_id", correlation_id)
        validate_reference(owner_id, "owner_id", correlation_id)
        AuthorityBoundTargetService._validate_reference(
            request_idempotency_key, "request_idempotency_key", correlation_id
        )
        if type(transport) not in (NullShadowTransport, RecordingShadowTransport):
            raise _reject(correlation_id, "shadow_transport_invalid")

        with self.operational.transaction():
            intent = self.store.intent(intent_id)
            if intent is None:
                raise _reject(correlation_id, "intent_not_found")
            existing = self._shadow_for_intent(intent_id)
            if existing is not None:
                execution, attempt, target = self.store.execution_context(
                    intent["execution_id"], correlation_id
                )
                self._guard_authority(
                    intent, execution, attempt, target, lease, owner_id, correlation_id
                )
                self._require_replay_key(existing, request_idempotency_key, transport, correlation_id)
                if existing["outcome"] in {"blocked", "mismatched", "quarantined"}:
                    return self._result(existing, "idempotent_replay")
                try:
                    self._guard_current(intent, lease, owner_id, correlation_id)
                except AuthorityRejected as rejected:
                    self._quarantine(existing, rejected.evidence.reason_class)
                    return self._result(
                        self._shadow_for_intent(intent_id), "quarantined"
                    )
                return self._result(existing, "idempotent_replay")

            execution, attempt, target = self.store.execution_context(
                intent["execution_id"], correlation_id
            )
            self._guard_authority(
                intent, execution, attempt, target, lease, owner_id, correlation_id
            )
            conflicting = self._shadow_for_request_key(
                request_idempotency_key, intent_id
            )
            if conflicting is not None:
                raise _reject(correlation_id, "shadow_idempotency_conflict")
            envelope = build_shadow_envelope(execution, target, correlation_id)
            if intent["status"] != "prepared":
                return self._persist(
                    intent, execution, attempt, target, request_idempotency_key,
                    transport.kind, "blocked", "intent_not_prepared",
                )

            try:
                snapshot = self._guard_current(
                    intent, lease, owner_id, correlation_id
                )
            except AuthorityRejected as rejected:
                self.store.block(intent_id, rejected.evidence.reason_class)
                return self._persist(
                    intent, execution, attempt, target, request_idempotency_key,
                    transport.kind, "blocked", rejected.evidence.reason_class,
                )

            snapshot_digest = snapshot_fingerprint(snapshot)
            if intent["request_fingerprint"] != envelope.fingerprint:
                self.store.block(intent_id, "request_fingerprint_mismatch")
                return self._persist(
                    intent, execution, attempt, target, request_idempotency_key,
                    transport.kind, "mismatched", "request_fingerprint_mismatch",
                    snapshot_digest, envelope,
                )

            receipt = transport.record(envelope.canonical_json)
            outcome = "evaluated"
            transport_digest = None
            reason = None
            if type(transport) is RecordingShadowTransport:
                if receipt is None:
                    raise _reject(correlation_id, "shadow_transport_invalid")
                transport_digest = receipt.fingerprint
                outcome = "matched" if transport_digest == envelope.fingerprint else "mismatched"
                reason = None if outcome == "matched" else "transport_fingerprint_mismatch"
                if outcome == "mismatched":
                    self.store.block(intent_id, reason)
            return self._persist(
                intent, execution, attempt, target, request_idempotency_key,
                transport.kind, outcome, reason, snapshot_digest, envelope,
                transport_digest,
            )

    def _guard_current(self, intent, lease, owner_id, correlation_id):
        execution, attempt, target = self.store.execution_context(
            intent["execution_id"], correlation_id
        )
        snapshot = self._guard_authority(
            intent, execution, attempt, target, lease, owner_id, correlation_id
        )
        if intent["status"] != "prepared":
            raise _reject(correlation_id, "intent_not_prepared")
        return snapshot

    def _guard_authority(
        self, intent, execution, attempt, target, lease, owner_id, correlation_id
    ):
        self._guard_lineage(
            intent, execution, attempt, target, lease, owner_id, correlation_id
        )
        snapshot = self.coordinator._require_snapshot(lease.scope, correlation_id)
        self.coordinator._current_lease_row(
            lease, owner_id, self.coordinator._now(correlation_id), correlation_id
        )
        self.coordinator._revalidate_snapshot(
            lease.scope, snapshot, correlation_id
        )
        return snapshot

    @staticmethod
    def _guard_lineage(intent, execution, attempt, target, lease, owner_id, correlation_id):
        require_execution_context(
            execution, attempt, target, lease, owner_id, correlation_id
        )
        DispatchIntentStore.require_claimed(execution, attempt, correlation_id)
        DispatchIntentStore.require_context(intent, target, lease, owner_id, correlation_id)
        if (
            intent["attempt_id"] != attempt["attempt_id"]
            or intent["execution_id"] != execution["execution_id"]
            or intent["request_fingerprint"] != DispatchIntentStore.request_fingerprint(target, execution)
        ):
            raise _reject(correlation_id, "shadow_intent_context_mismatch")

    @staticmethod
    def _require_replay_key(existing, request_idempotency_key, transport, correlation_id):
        if existing["request_idempotency_key"] != request_idempotency_key:
            raise _reject(correlation_id, "shadow_idempotency_conflict")
        if existing["transport_kind"] != transport.kind:
            raise _reject(correlation_id, "shadow_transport_conflict")

    def _shadow_for_intent(self, intent_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_shadow_evaluations WHERE intent_id=?",
            (intent_id,),
        ).fetchone()

    def _shadow_for_request_key(self, request_key, intent_id):
        return self.operational.connection.execute(
            "SELECT shadow_evaluation_id FROM authority_bound_shadow_evaluations "
            "WHERE request_idempotency_key=? AND intent_id<>?",
            (request_key, intent_id),
        ).fetchone()

    def _persist(
        self, intent, execution, attempt, target, request_key, transport_kind,
        outcome, reason, snapshot_digest=None, envelope: ShadowEnvelope | None = None,
        transport_digest=None,
    ):
        now = self.coordinator._now("shadow").isoformat()
        shadow_id = "shadow-" + secrets.token_hex(16)
        self.operational.connection.execute(
            "INSERT INTO authority_bound_shadow_evaluations ("
            "shadow_evaluation_id, intent_id, execution_id, attempt_id, target_id, job_id, "
            "intent_idempotency_key, request_idempotency_key, operation_kind, target_ref, "
            "host_id, profile_id, binding_generation, verified_identity_ref, "
            "verified_identity_revision, authority_epoch, fence_counter, owner_id, lease_id, "
            "envelope_version, transport_kind, portable_snapshot_fingerprint, "
            "prepared_request_fingerprint, envelope_json, envelope_fingerprint, "
            "transport_fingerprint, outcome, reason_class, created_at, evaluated_at) "
            "VALUES (" + ", ".join("?" for _ in range(30)) + ")",
            (
                shadow_id, intent["intent_id"], execution["execution_id"], attempt["attempt_id"],
                target["target_id"], target["job_id"], intent["idempotency_key"], request_key,
                target["operation_kind"], target["target_ref"], intent["host_id"],
                intent["profile_id"], intent["binding_generation"], intent["verified_identity_ref"],
                intent["verified_identity_revision"], intent["authority_epoch"],
                intent["fence_counter"], intent["owner_id"], intent["lease_id"], 1, transport_kind,
                snapshot_digest, intent["request_fingerprint"],
                envelope.canonical_json if envelope else None,
                envelope.fingerprint if envelope else None, transport_digest, outcome, reason,
                now, now if outcome in {"evaluated", "matched", "mismatched"} else None,
            ),
        )
        return self._result(self._shadow_for_intent(intent["intent_id"]), outcome)

    def _quarantine(self, row, reason):
        self.operational.connection.execute(
            "UPDATE authority_bound_shadow_evaluations SET outcome='quarantined', "
            "quarantine_reason=?, quarantined_at=? WHERE shadow_evaluation_id=? "
            "AND outcome IN ('evaluated', 'matched')",
            (reason, self.coordinator._now("shadow").isoformat(), row["shadow_evaluation_id"]),
        )

    @staticmethod
    def _result(row, outcome):
        safe_fields = (
            "shadow_evaluation_id", "intent_id", "execution_id", "attempt_id",
            "target_id", "job_id", "operation_kind", "target_ref", "envelope_version",
            "transport_kind", "portable_snapshot_fingerprint",
            "prepared_request_fingerprint", "envelope_json", "envelope_fingerprint",
            "transport_fingerprint", "outcome", "reason_class", "created_at",
            "evaluated_at", "quarantined_at", "quarantine_reason",
        )
        result = {field: row[field] for field in safe_fields}
        result["status"] = row["outcome"]
        result["outcome"] = outcome
        return result
