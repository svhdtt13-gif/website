from __future__ import annotations

import re
import secrets
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.authority_bound_targets import AuthorityBoundTargetService
from services.authority_execution_context import (
    require_execution_context,
    validate_reference,
)
from services.binding_authority import (
    BindingAuthorityCoordinator,
    _correlation_id,
    _reject,
)
from services.binding_authority_types import (
    AuthorityRejected,
    BindingLease,
)
from services.dispatch_intent_store import DispatchIntentStore

_RECONCILIATION_RESULT = {"succeeded", "failed"}
_UNKNOWN_REASON = re.compile(r"[a-z][a-z0-9_]{2,63}\Z")
_EVIDENCE_REFERENCE = re.compile(
    r"(?:evidence|reconcile)-[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
)


class DispatchPreparationService:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        coordinator: BindingAuthorityCoordinator,
    ):
        self.portable_path = Path(portable_path)
        self.operational = operational
        self.coordinator = coordinator
        self.store = DispatchIntentStore(operational)

    def prepare_intent(
        self,
        execution_id: str,
        lease: BindingLease,
        owner_id: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        validate_reference(execution_id, "execution_id", correlation_id)
        validate_reference(owner_id, "owner_id", correlation_id)

        with self.operational.transaction():
            execution, attempt, target = self.store.execution_context(
                execution_id, correlation_id
            )
            existing = self.store.intent_for_execution(execution_id)
            require_execution_context(
                execution, attempt, target, lease, owner_id, correlation_id
            )
            self.store.require_claimed(execution, attempt, correlation_id)
            if existing is not None:
                self.store.require_context(
                    existing, target, lease, owner_id, correlation_id
                )
            try:
                snapshot = self.coordinator._require_snapshot(
                    lease.scope, correlation_id
                )
                self.coordinator._current_lease_row(
                    lease,
                    owner_id,
                    self.coordinator._now(correlation_id),
                    correlation_id,
                )
            except AuthorityRejected as rejected:
                if existing is None or existing["status"] != "prepared":
                    raise
                self.store.block(
                    existing["intent_id"], rejected.evidence.reason_class
                )
                return self.store.result(
                    self.store.intent(existing["intent_id"]), "blocked"
                )
            if existing is not None:
                if existing["status"] != "prepared":
                    return self.store.result(existing, "idempotent_replay")
                try:
                    self.coordinator._revalidate_snapshot(
                        lease.scope, snapshot, correlation_id
                    )
                except AuthorityRejected as rejected:
                    self.store.block(
                        existing["intent_id"], rejected.evidence.reason_class
                    )
                    return self.store.result(
                        self.store.intent(existing["intent_id"]), "blocked"
                    )
                return self.store.result(existing, "idempotent_replay")

            now = self.coordinator._now(correlation_id).isoformat()
            intent_id = "intent-" + secrets.token_hex(16)
            idempotency_key = "dispatch-" + execution_id
            request_fingerprint = self.store.request_fingerprint(target, execution)
            self.operational.connection.execute(
                "INSERT INTO authority_bound_dispatch_intents ("
                "intent_id, execution_id, attempt_id, target_id, job_id, "
                "idempotency_key, operation_kind, target_ref, request_fingerprint, "
                "host_id, profile_id, binding_generation, verified_identity_ref, "
                "verified_identity_revision, authority_epoch, fence_counter, "
                "owner_id, lease_id, status, created_at, prepared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'prepared', ?, ?) ",
                (
                    intent_id, execution_id, attempt["attempt_id"], target["target_id"],
                    target["job_id"], idempotency_key, target["operation_kind"],
                    target["target_ref"], request_fingerprint, lease.scope.host_id,
                    lease.scope.profile_id, lease.scope.binding_generation,
                    lease.scope.verified_identity_ref,
                    lease.scope.verified_identity_revision,
                    lease.fence.authority_epoch, lease.fence.fence_counter,
                    owner_id, lease.lease_id, now, now,
                ),
            )
            try:
                self.coordinator._revalidate_snapshot(
                    lease.scope, snapshot, correlation_id
                )
                self.coordinator._current_lease_row(
                    lease, owner_id, self.coordinator._now(correlation_id), correlation_id
                )
            except AuthorityRejected as rejected:
                self.store.block(intent_id, rejected.evidence.reason_class)
            return self.store.result(self.store.intent(intent_id), "prepared")

    def record_unknown(
        self,
        intent_id: str,
        lease: BindingLease,
        owner_id: str,
        reason: str,
        evidence_ref: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        self._validate_unknown_fields(reason, evidence_ref, correlation_id)
        snapshot = self.coordinator._require_snapshot(lease.scope, correlation_id)
        with self.operational.transaction():
            row = self._guard_intent(
                intent_id, lease, owner_id, snapshot, correlation_id
            )
            if row["status"] == "unknown":
                if (
                    row["unknown_reason"] != reason
                    or row["evidence_ref"] != evidence_ref
                ):
                    raise _reject(correlation_id, "unknown_conflict")
                return self.store.result(row, "idempotent_replay")
            if row["status"] != "prepared":
                raise _reject(correlation_id, "intent_not_unknownable")
            now = self.coordinator._now(correlation_id).isoformat()
            self.operational.connection.execute(
                "UPDATE authority_bound_dispatch_intents SET status='unknown', "
                "unknown_reason=?, evidence_ref=?, unknown_at=? WHERE intent_id=?",
                (reason, evidence_ref, now, intent_id),
            )
            return self.store.result(self.store.intent(intent_id), "unknown_recorded")

    def reconcile_unknown(
        self,
        intent_id: str,
        lease: BindingLease,
        owner_id: str,
        result: str,
        evidence_ref: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        self._validate_unknown_fields(result, evidence_ref, correlation_id)
        if result not in _RECONCILIATION_RESULT:
            raise _reject(correlation_id, "reconciliation_result_invalid")
        snapshot = self.coordinator._require_snapshot(lease.scope, correlation_id)
        with self.operational.transaction():
            row = self._guard_intent(
                intent_id, lease, owner_id, snapshot, correlation_id
            )
            if row["status"] == "reconciled":
                if (
                    row["reconciliation_result"] != result
                    or row["reconciliation_evidence_ref"] != evidence_ref
                ):
                    raise _reject(correlation_id, "reconciliation_conflict")
                return self.store.result(row, "idempotent_replay")
            if row["status"] != "unknown":
                raise _reject(correlation_id, "intent_not_reconcilable")
            now = self.coordinator._now(correlation_id).isoformat()
            self.operational.connection.execute(
                "UPDATE authority_bound_dispatch_intents SET status='reconciled', "
                "reconciliation_result=?, reconciliation_evidence_ref=?, "
                "reconciled_at=? "
                "WHERE intent_id=?",
                (result, evidence_ref, now, intent_id),
            )
            return self.store.result(self.store.intent(intent_id), "reconciled")

    def _guard_intent(self, intent_id, lease, owner_id, snapshot, correlation_id):
        validate_reference(intent_id, "intent_id", correlation_id)
        row = self.store.intent(intent_id)
        if row is None:
            raise _reject(correlation_id, "intent_not_found")
        _execution, attempt, target = self.store.execution_context(
            row["execution_id"], correlation_id
        )
        require_execution_context(
            _execution, attempt, target, lease, owner_id, correlation_id
        )
        self.store.require_context(row, target, lease, owner_id, correlation_id)
        self.coordinator._current_lease_row(
            lease, owner_id, self.coordinator._now(correlation_id), correlation_id
        )
        self.coordinator._revalidate_snapshot(
            lease.scope, snapshot, correlation_id
        )
        return row

    @staticmethod
    def _validate_unknown_fields(reason, evidence_ref, correlation_id):
        if not isinstance(reason, str) or not _UNKNOWN_REASON.fullmatch(reason):
            raise _reject(correlation_id, "unknown_reason_invalid")
        try:
            AuthorityBoundTargetService._validate_reference(
                evidence_ref, "evidence_ref", correlation_id
            )
        except AuthorityRejected:
            raise _reject(correlation_id, "evidence_reference_invalid") from None
        if not _EVIDENCE_REFERENCE.fullmatch(evidence_ref):
            raise _reject(correlation_id, "evidence_reference_invalid")
