from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.authority_bound_targets import AuthorityBoundTargetService
from services.binding_authority import _correlation_id, _reject
from services.binding_authority_types import BindingLease
from services.canary_envelope import CANARY_CONTRACT_VERSION, build_canary_envelope
from services.canary_transport import (
    ContractProbeTransport,
    NullCanaryTransport,
    RecordingCanaryTransport,
)
from services.dispatch_intent_store import DispatchIntentStore
from services.shadow_dispatch import DryRunShadowDispatcher
from services.shadow_dispatch_envelope import snapshot_fingerprint

_READ_ONLY_SOURCE = "read_only"
_AMBIGUOUS_DEFAULT_REASON = "ambiguous_boundary"
_ALLOWED_RECONCILIATION_RESULTS = ("succeeded", "failed")


@dataclass(frozen=True, slots=True)
class CanaryReconciliationEvidence:
    evidence_ref: str
    result: str
    source: str
    canary_idempotency_key: str
    pre_send_identity: str
    envelope_fingerprint: str


def _validate_evidence_value(value: str, label: str, correlation_id: str) -> None:
    AuthorityBoundTargetService._validate_reference(value, label, correlation_id)


def _normalize_evidence(evidence, correlation_id: str) -> CanaryReconciliationEvidence:
    if isinstance(evidence, CanaryReconciliationEvidence):
        return evidence
    if not isinstance(evidence, dict):
        raise _reject(correlation_id, "canary_evidence_invalid")
    try:
        normalized = CanaryReconciliationEvidence(
            evidence_ref=evidence["evidence_ref"],
            result=evidence["result"],
            source=evidence["source"],
            canary_idempotency_key=evidence["canary_idempotency_key"],
            pre_send_identity=evidence["pre_send_identity"],
            envelope_fingerprint=evidence["envelope_fingerprint"],
        )
    except (KeyError, TypeError):
        raise _reject(correlation_id, "canary_evidence_invalid") from None
    return normalized


def _require_trusted_evidence(evidence: CanaryReconciliationEvidence, correlation_id: str) -> None:
    if evidence.source != _READ_ONLY_SOURCE:
        raise _reject(correlation_id, "canary_reconciliation_untrusted")
    if evidence.result not in _ALLOWED_RECONCILIATION_RESULTS:
        raise _reject(correlation_id, "canary_evidence_invalid")
    _validate_evidence_value(evidence.evidence_ref, "evidence_ref", correlation_id)
    _validate_evidence_value(
        evidence.canary_idempotency_key, "canary_idempotency_key", correlation_id
    )
    _validate_evidence_value(
        evidence.pre_send_identity, "pre_send_identity", correlation_id
    )
    _validate_evidence_value(
        evidence.envelope_fingerprint, "envelope_fingerprint", correlation_id
    )


class CanaryArmingService:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        coordinator,
    ):
        self.operational = operational
        self.coordinator = coordinator
        self.store = DispatchIntentStore(operational)
        self.shadow = DryRunShadowDispatcher(portable_path, operational, coordinator)

    def arm(
        self,
        shadow_evaluation_id: str,
        lease: BindingLease,
        owner_id: str,
        transport: NullCanaryTransport | RecordingCanaryTransport | ContractProbeTransport,
        request_id: str | None = None,
    ):
        correlation_id = _correlation_id(request_id)
        AuthorityBoundTargetService._validate_reference(
            shadow_evaluation_id, "shadow_evaluation_id", correlation_id
        )
        AuthorityBoundTargetService._validate_reference(
            owner_id, "owner_id", correlation_id
        )
        if type(transport) not in (
            NullCanaryTransport,
            RecordingCanaryTransport,
            ContractProbeTransport,
        ):
            raise _reject(correlation_id, "canary_transport_invalid")

        with self.operational.transaction():
            shadow = self._shadow(shadow_evaluation_id)
            if shadow is None:
                raise _reject(correlation_id, "canary_shadow_not_found")
            intent = self.store.intent(shadow["intent_id"])
            if intent is None:
                raise _reject(correlation_id, "intent_not_found")
            execution, attempt, target = self.store.execution_context(
                intent["execution_id"], correlation_id
            )
            snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self._require_matched_lineage(
                shadow, intent, execution, attempt, target,
                snapshot_fingerprint(snapshot), correlation_id,
            )
            if transport.contract_version != CANARY_CONTRACT_VERSION:
                raise _reject(correlation_id, "canary_transport_contract_mismatch")
            envelope = build_canary_envelope(
                shadow, intent, execution, attempt, target, correlation_id
            )
            existing = self._candidate(shadow_evaluation_id)
            if existing is not None:
                self._require_replay(
                    existing, shadow, intent, envelope, transport, correlation_id
                )
                return self._result(existing, "idempotent_replay")
            conflict = self.operational.connection.execute(
                "SELECT canary_candidate_id FROM authority_bound_canary_candidates "
                "WHERE canary_idempotency_key=? OR pre_send_identity=?",
                (envelope.canary_idempotency_key, envelope.pre_send_identity),
            ).fetchone()
            if conflict is not None:
                raise _reject(correlation_id, "canary_identity_conflict")
            pre_observe_snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self._require_matched_lineage(
                shadow, intent, execution, attempt, target,
                snapshot_fingerprint(pre_observe_snapshot), correlation_id,
            )
            self._require_upstream_stable(shadow, intent, correlation_id)
            receipt = transport.observe(envelope.canonical_json)
            if receipt is not None and receipt.fingerprint != envelope.fingerprint:
                raise _reject(correlation_id, "canary_transport_contract_mismatch")
            final_snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            final_digest = snapshot_fingerprint(final_snapshot)
            self._require_matched_lineage(
                shadow, intent, execution, attempt, target,
                final_digest, correlation_id,
            )
            self._require_upstream_stable(shadow, intent, correlation_id)
            return self._persist(shadow, intent, envelope, transport, final_digest)

    def record_ambiguous(
        self,
        canary_candidate_id: str,
        lease: BindingLease,
        owner_id: str,
        evidence_ref: str,
        reason_class: str = _AMBIGUOUS_DEFAULT_REASON,
        pre_send_identity: str | None = None,
        request_id: str | None = None,
    ):
        correlation_id = _correlation_id(request_id)
        AuthorityBoundTargetService._validate_reference(
            canary_candidate_id, "canary_candidate_id", correlation_id
        )
        AuthorityBoundTargetService._validate_reference(
            owner_id, "owner_id", correlation_id
        )
        AuthorityBoundTargetService._validate_reference(
            evidence_ref, "evidence_ref", correlation_id
        )
        AuthorityBoundTargetService._validate_reference(
            reason_class, "reason_class", correlation_id
        )
        if pre_send_identity is not None:
            AuthorityBoundTargetService._validate_reference(
                pre_send_identity, "pre_send_identity", correlation_id
            )
        with self.operational.transaction():
            candidate = self._candidate_by_id(canary_candidate_id)
            if candidate is None:
                raise _reject(correlation_id, "canary_candidate_not_found")
            shadow = self._shadow(candidate["shadow_evaluation_id"])
            if shadow is None:
                raise _reject(correlation_id, "canary_shadow_not_found")
            intent = self.store.intent(shadow["intent_id"])
            if intent is None:
                raise _reject(correlation_id, "intent_not_found")
            execution, attempt, target = self.store.execution_context(
                intent["execution_id"], correlation_id
            )
            snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self._require_candidate_lineage(
                candidate, shadow, intent, execution, attempt, target,
                snapshot_fingerprint(snapshot), correlation_id,
            )
            if (
                pre_send_identity is not None
                and candidate["pre_send_identity"] != pre_send_identity
            ):
                raise _reject(correlation_id, "canary_ambiguity_conflict")
            if candidate["state"] == "unknown":
                if (
                    candidate["ambiguity_evidence_ref"] == evidence_ref
                    and candidate["ambiguity_reason_class"] == reason_class
                ):
                    return self._result(candidate, "idempotent_replay")
                raise _reject(correlation_id, "canary_ambiguity_conflict")
            if candidate["state"] != "armed":
                raise _reject(correlation_id, "canary_ambiguity_state_invalid")
            late_snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self._require_candidate_lineage(
                candidate, shadow, intent, execution, attempt, target,
                snapshot_fingerprint(late_snapshot), correlation_id,
            )
            self._require_upstream_stable(shadow, intent, correlation_id)
            now = self.coordinator._now("canary").isoformat()
            self.operational.connection.execute(
                "UPDATE authority_bound_canary_candidates SET state='unknown', "
                "reason_class=?, ambiguity_evidence_ref=?, "
                "ambiguity_reason_class=?, unknown_at=? "
                "WHERE canary_candidate_id=? AND state='armed'",
                (
                    reason_class, evidence_ref, reason_class, now,
                    canary_candidate_id,
                ),
            )
            updated = self._candidate_by_id(canary_candidate_id)
            if updated is None or updated["state"] != "unknown":
                raise _reject(correlation_id, "canary_ambiguity_fenced")
            return self._result(updated, "ambiguous_recorded")

    def reconcile_unknown(
        self,
        canary_candidate_id: str,
        lease: BindingLease,
        owner_id: str,
        evidence,
        request_id: str | None = None,
    ):
        correlation_id = _correlation_id(request_id)
        AuthorityBoundTargetService._validate_reference(
            canary_candidate_id, "canary_candidate_id", correlation_id
        )
        AuthorityBoundTargetService._validate_reference(
            owner_id, "owner_id", correlation_id
        )
        normalized = _normalize_evidence(evidence, correlation_id)
        _require_trusted_evidence(normalized, correlation_id)
        with self.operational.transaction():
            candidate = self._candidate_by_id(canary_candidate_id)
            if candidate is None:
                raise _reject(correlation_id, "canary_candidate_not_found")
            shadow = self._shadow(candidate["shadow_evaluation_id"])
            if shadow is None:
                raise _reject(correlation_id, "canary_shadow_not_found")
            intent = self.store.intent(shadow["intent_id"])
            if intent is None:
                raise _reject(correlation_id, "intent_not_found")
            execution, attempt, target = self.store.execution_context(
                intent["execution_id"], correlation_id
            )
            snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self._require_candidate_lineage(
                candidate, shadow, intent, execution, attempt, target,
                snapshot_fingerprint(snapshot), correlation_id,
            )
            self._require_evidence_binding(candidate, normalized, correlation_id)
            if candidate["state"] == "reconciled":
                if (
                    candidate["reconciliation_evidence_ref"]
                    == normalized.evidence_ref
                    and candidate["reconciliation_result"] == normalized.result
                ):
                    return self._result(candidate, "idempotent_replay")
                raise _reject(correlation_id, "canary_reconciliation_conflict")
            if candidate["state"] != "unknown":
                raise _reject(correlation_id, "canary_reconciliation_state_invalid")
            late_snapshot = self.shadow._guard_current(
                intent, lease, owner_id, correlation_id
            )
            self._require_candidate_lineage(
                candidate, shadow, intent, execution, attempt, target,
                snapshot_fingerprint(late_snapshot), correlation_id,
            )
            self._require_evidence_binding(candidate, normalized, correlation_id)
            self._require_upstream_stable(shadow, intent, correlation_id)
            now = self.coordinator._now("canary").isoformat()
            self.operational.connection.execute(
                "UPDATE authority_bound_canary_candidates "
                "SET state='reconciled', reconciliation_evidence_ref=?, "
                "reconciliation_result=?, reconciled_at=? "
                "WHERE canary_candidate_id=? AND state='unknown'",
                (
                    normalized.evidence_ref, normalized.result, now,
                    canary_candidate_id,
                ),
            )
            updated = self._candidate_by_id(canary_candidate_id)
            if updated is None or updated["state"] != "reconciled":
                raise _reject(correlation_id, "canary_reconciliation_fenced")
            return self._result(updated, "reconciled")

    @staticmethod
    def _require_matched_lineage(
        shadow, intent, execution, attempt, target, snapshot_digest, correlation_id
    ):
        if shadow["outcome"] != "matched":
            raise _reject(correlation_id, "canary_shadow_not_matched")
        expected = (
            intent["intent_id"], execution["execution_id"], attempt["attempt_id"],
            target["target_id"], target["job_id"], intent["idempotency_key"],
            target["operation_kind"], target["target_ref"], intent["host_id"],
            intent["profile_id"], intent["binding_generation"],
            intent["verified_identity_ref"], intent["verified_identity_revision"],
            intent["authority_epoch"], intent["fence_counter"], intent["owner_id"],
            intent["lease_id"], snapshot_digest, intent["request_fingerprint"],
        )
        actual = tuple(
            shadow[field]
            for field in (
                "intent_id", "execution_id", "attempt_id", "target_id", "job_id",
                "intent_idempotency_key", "operation_kind", "target_ref", "host_id",
                "profile_id", "binding_generation", "verified_identity_ref",
                "verified_identity_revision", "authority_epoch", "fence_counter",
                "owner_id", "lease_id", "portable_snapshot_fingerprint",
                "prepared_request_fingerprint",
            )
        )
        fingerprints_match = (
            shadow["envelope_fingerprint"] == intent["request_fingerprint"]
            and shadow["transport_fingerprint"] == shadow["envelope_fingerprint"]
        )
        if actual != expected or not fingerprints_match:
            raise _reject(correlation_id, "canary_shadow_lineage_mismatch")

    @staticmethod
    def _require_replay(existing, shadow, intent, envelope, transport, correlation_id):
        if existing["transport_kind"] != transport.kind:
            raise _reject(correlation_id, "canary_transport_conflict")
        expected = (
            shadow["shadow_evaluation_id"], intent["intent_id"],
            envelope.execution_id, envelope.attempt_id, envelope.target_id,
            envelope.job_id, intent["idempotency_key"],
            shadow["request_idempotency_key"], envelope.operation_kind,
            envelope.destination_ref, envelope.host_id, envelope.profile_id,
            envelope.binding_generation, envelope.verified_identity_ref,
            envelope.verified_identity_revision, envelope.authority_epoch,
            envelope.fence_counter, envelope.owner_id, envelope.lease_id,
            shadow["portable_snapshot_fingerprint"], shadow["envelope_fingerprint"],
            envelope.canary_idempotency_key, envelope.pre_send_identity,
            envelope.canonical_json, envelope.fingerprint,
            envelope.contract_version, transport.contract_version,
        )
        actual = tuple(
            existing[field]
            for field in (
                "shadow_evaluation_id", "intent_id", "execution_id", "attempt_id",
                "target_id", "job_id", "intent_idempotency_key",
                "shadow_idempotency_key", "operation_kind", "destination_ref",
                "host_id", "profile_id", "binding_generation",
                "verified_identity_ref", "verified_identity_revision",
                "authority_epoch", "fence_counter", "owner_id", "lease_id",
                "portable_snapshot_fingerprint", "shadow_envelope_fingerprint",
                "canary_idempotency_key", "pre_send_identity", "envelope_json",
                "envelope_fingerprint", "contract_version",
                "transport_contract_version",
            )
        )
        if actual != expected:
            raise _reject(correlation_id, "canary_replay_conflict")

    @staticmethod
    def _require_evidence_binding(candidate, evidence, correlation_id):
        if (
            candidate["canary_idempotency_key"] != evidence.canary_idempotency_key
            or candidate["pre_send_identity"] != evidence.pre_send_identity
            or candidate["envelope_fingerprint"] != evidence.envelope_fingerprint
        ):
            raise _reject(correlation_id, "canary_reconciliation_conflict")

    def _require_candidate_lineage(
        self, candidate, shadow, intent, execution, attempt, target,
        snapshot_digest, correlation_id,
    ):
        self._require_matched_lineage(
            shadow, intent, execution, attempt, target,
            snapshot_digest, correlation_id,
        )
        expected = (
            shadow["shadow_evaluation_id"], intent["intent_id"],
            execution["execution_id"], attempt["attempt_id"],
            target["target_id"], target["job_id"], intent["idempotency_key"],
            shadow["request_idempotency_key"], candidate["canary_idempotency_key"],
            candidate["pre_send_identity"], candidate["envelope_json"],
            candidate["envelope_fingerprint"], intent["host_id"],
            intent["profile_id"], intent["binding_generation"],
            intent["verified_identity_ref"], intent["verified_identity_revision"],
            intent["authority_epoch"], intent["fence_counter"],
            intent["owner_id"], intent["lease_id"],
        )
        actual = (
            candidate["shadow_evaluation_id"], candidate["intent_id"],
            candidate["execution_id"], candidate["attempt_id"],
            candidate["target_id"], candidate["job_id"],
            candidate["intent_idempotency_key"],
            candidate["shadow_idempotency_key"],
            candidate["canary_idempotency_key"], candidate["pre_send_identity"],
            candidate["envelope_json"], candidate["envelope_fingerprint"],
            candidate["host_id"], candidate["profile_id"],
            candidate["binding_generation"],
            candidate["verified_identity_ref"],
            candidate["verified_identity_revision"],
            candidate["authority_epoch"], candidate["fence_counter"],
            candidate["owner_id"], candidate["lease_id"],
        )
        if actual != expected:
            raise _reject(correlation_id, "canary_candidate_lineage_mismatch")
        if candidate["portable_snapshot_fingerprint"] != snapshot_digest:
            raise _reject(correlation_id, "canary_candidate_lineage_mismatch")

    def _require_upstream_stable(self, shadow, intent, correlation_id):
        fresh_shadow = self._shadow(shadow["shadow_evaluation_id"])
        fresh_intent = self.store.intent(intent["intent_id"])
        if fresh_shadow is None or fresh_intent is None:
            raise _reject(correlation_id, "canary_upstream_changed")
        if dict(fresh_shadow) != dict(shadow) or dict(fresh_intent) != dict(intent):
            raise _reject(correlation_id, "canary_upstream_changed")

    def _shadow(self, shadow_evaluation_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_shadow_evaluations "
            "WHERE shadow_evaluation_id=?", (shadow_evaluation_id,),
        ).fetchone()

    def _candidate(self, shadow_evaluation_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_canary_candidates "
            "WHERE shadow_evaluation_id=?", (shadow_evaluation_id,),
        ).fetchone()

    def _candidate_by_id(self, canary_candidate_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?", (canary_candidate_id,),
        ).fetchone()

    def _persist(self, shadow, intent, envelope, transport, snapshot_digest):
        now = self.coordinator._now("canary").isoformat()
        candidate_id = "canary-" + envelope.canary_idempotency_key.removeprefix(
            "canary-key-"
        )[:32]
        values = (
            candidate_id, shadow["shadow_evaluation_id"], intent["intent_id"],
            envelope.execution_id, envelope.attempt_id, envelope.target_id,
            envelope.job_id, intent["idempotency_key"],
            shadow["request_idempotency_key"], envelope.canary_idempotency_key,
            envelope.operation_kind, envelope.destination_ref, envelope.host_id,
            envelope.profile_id, envelope.binding_generation,
            envelope.verified_identity_ref, envelope.verified_identity_revision,
            envelope.authority_epoch, envelope.fence_counter, envelope.owner_id,
            envelope.lease_id, snapshot_digest,
            shadow["envelope_fingerprint"], CANARY_CONTRACT_VERSION, transport.kind,
            transport.contract_version, envelope.pre_send_identity,
            envelope.canonical_json, envelope.fingerprint, "armed", None, now, now,
        )
        self.operational.connection.execute(
            "INSERT INTO authority_bound_canary_candidates ("
            "canary_candidate_id, shadow_evaluation_id, intent_id, execution_id, "
            "attempt_id, target_id, job_id, intent_idempotency_key, shadow_idempotency_key, "
            "canary_idempotency_key, operation_kind, destination_ref, host_id, profile_id, "
            "binding_generation, verified_identity_ref, verified_identity_revision, "
            "authority_epoch, fence_counter, owner_id, lease_id, "
            "portable_snapshot_fingerprint, shadow_envelope_fingerprint, contract_version, "
            "transport_kind, transport_contract_version, pre_send_identity, envelope_json, "
            "envelope_fingerprint, state, reason_class, created_at, armed_at) VALUES ("
            + ",".join("?" for _ in values) + ")",
            values,
        )
        return self._result(self._candidate(shadow["shadow_evaluation_id"]), "armed")

    @staticmethod
    def _result(row, outcome):
        result = dict(row)
        result["outcome"] = outcome
        return result
