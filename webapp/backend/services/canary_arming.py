from __future__ import annotations

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
            receipt = transport.observe(envelope.canonical_json)
            if receipt is not None and receipt.fingerprint != envelope.fingerprint:
                raise _reject(correlation_id, "canary_transport_contract_mismatch")
            return self._persist(shadow, intent, envelope, transport)

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

    def _persist(self, shadow, intent, envelope, transport):
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
            envelope.lease_id, shadow["portable_snapshot_fingerprint"],
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
