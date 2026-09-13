from __future__ import annotations

import hashlib
import json
import re
import secrets
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.binding_authority import (
    BindingAuthorityCoordinator,
    _correlation_id,
    _reject,
)
from services.binding_authority_types import (
    AuthorityRejected,
    BindingLease,
    BindingScope,
)

_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


class AuthorityBoundTargetService:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        coordinator: BindingAuthorityCoordinator,
    ):
        self.portable_path = Path(portable_path)
        self.operational = operational
        self.coordinator = coordinator

    def record_target(
        self,
        scope: BindingScope,
        lease: BindingLease,
        operation_kind: str,
        target_ref: str,
        requested_by: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        self._validate_reference(operation_kind, "operation_kind", correlation_id)
        self._validate_reference(target_ref, "target_ref", correlation_id)
        self._validate_reference(requested_by, "requested_by", correlation_id)
        self._validate_reference(idempotency_key, "idempotency_key", correlation_id)
        self._validate_lease_scope(lease, scope, correlation_id)
        with self.operational.transaction():
            existing = self._by_idempotency(idempotency_key)
            if existing is not None:
                self._require_same_context(
                    existing, scope, lease, operation_kind, target_ref, correlation_id
                )
                return self._result(existing, "idempotent_replay")

        snapshot = self.coordinator._require_snapshot(scope, correlation_id)
        now = self.coordinator._now(correlation_id).isoformat()
        snapshot_digest = self._snapshot_digest(snapshot)
        context_fingerprint = self._context_fingerprint(
            scope, lease, operation_kind, target_ref, idempotency_key
        )
        target_id = "target-" + secrets.token_hex(16)
        job_id = "job-" + secrets.token_hex(16)
        provenance = json.dumps(
            {
                "contract_version": 1,
                "correlation_id": correlation_id,
                "requested_by": requested_by,
                "source_snapshot_digest": snapshot_digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

        with self.operational.transaction():
            if self._by_idempotency(idempotency_key) is not None:
                existing = self._by_idempotency(idempotency_key)
                self._require_same_context(
                    existing, scope, lease, operation_kind, target_ref, correlation_id
                )
                return self._result(existing, "idempotent_replay")
            self.coordinator._authority_state(correlation_id)
            self.coordinator._current_lease_row(
                lease, lease.owner_id, self.coordinator._now(correlation_id),
                correlation_id,
            )
            self.operational.connection.execute(
                "INSERT INTO authority_bound_targets ("
                "target_id, job_id, operation_kind, target_ref, requested_by, "
                "idempotency_key, host_id, profile_id, binding_generation, "
                "verified_identity_ref, verified_identity_revision, authority_epoch, "
                "fence_counter, context_fingerprint, source_snapshot_digest, "
                "provenance_json, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'requested', ?)",
                (
                    target_id, job_id, operation_kind, target_ref, requested_by,
                    idempotency_key, scope.host_id, scope.profile_id,
                    scope.binding_generation, scope.verified_identity_ref,
                    scope.verified_identity_revision, lease.fence.authority_epoch,
                    lease.fence.fence_counter, context_fingerprint, snapshot_digest,
                    provenance, now,
                ),
            )
            try:
                self.coordinator._revalidate_snapshot(scope, snapshot, correlation_id)
                self.coordinator._current_lease_row(
                    lease, lease.owner_id, self.coordinator._now(correlation_id),
                    correlation_id,
                )
            except AuthorityRejected as rejected:
                self._quarantine(target_id, rejected.evidence.reason_class)
            else:
                self.operational.connection.execute(
                    "UPDATE authority_bound_targets SET status='claimable', recorded_at=? "
                    "WHERE target_id=?",
                    (self.coordinator._now(correlation_id).isoformat(), target_id),
                )
            return self._result(self._by_target(target_id), "recorded")

    def claim_target(
        self,
        target_id: str,
        lease: BindingLease,
        owner_id: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        self._validate_reference(target_id, "target_id", correlation_id)
        self._validate_reference(owner_id, "owner_id", correlation_id)
        snapshot = self.coordinator._require_snapshot(lease.scope, correlation_id)
        with self.operational.transaction():
            row = self._by_target(target_id)
            if row is None:
                raise _reject(correlation_id, "target_not_found")
            if row["status"] != "claimable":
                self._require_same_context(
                    row, lease.scope, lease, row["operation_kind"],
                    row["target_ref"], correlation_id,
                )
                if row["status"] == "quarantined":
                    return self._result(row, "terminal_replay")
                if (
                    row["status"] == "claimed"
                    and row["claimed_by"] == owner_id
                    and row["claim_lease_id"] == lease.lease_id
                ):
                    return self._result(row, "terminal_replay")
                raise _reject(correlation_id, "target_not_claimable")
            self._require_same_context(
                row, lease.scope, lease, row["operation_kind"],
                row["target_ref"], correlation_id,
            )
            self.coordinator._current_lease_row(
                lease, owner_id, self.coordinator._now(correlation_id),
                correlation_id,
            )
            now = self.coordinator._now(correlation_id).isoformat()
            self.operational.connection.execute(
                "UPDATE authority_bound_targets SET status='claimed', claimed_at=?, "
                "claimed_by=?, claim_lease_id=? WHERE target_id=? AND status='claimable'",
                (now, owner_id, lease.lease_id, target_id),
            )
            try:
                self.coordinator._revalidate_snapshot(lease.scope, snapshot, correlation_id)
                self.coordinator._current_lease_row(
                    lease, owner_id, self.coordinator._now(correlation_id),
                    correlation_id,
                )
            except AuthorityRejected as rejected:
                self._quarantine(target_id, rejected.evidence.reason_class)
            return self._result(self._by_target(target_id), "claimed")

    def attempt_dispatch(self, target_id: str) -> dict[str, object]:
        row = self._by_target(target_id)
        if row is None:
            raise ValueError("target not found")
        return {
            "target_id": target_id,
            "status": row["status"],
            "dispatched": False,
            "dispatch_count": 0,
            "reason": "slice1b_no_dispatch",
        }

    def _by_idempotency(self, key: str):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_targets WHERE idempotency_key=?", (key,)
        ).fetchone()

    def _by_target(self, target_id: str):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_targets WHERE target_id=?", (target_id,)
        ).fetchone()

    def _quarantine(self, target_id: str, reason: str) -> None:
        self.operational.connection.execute(
            "UPDATE authority_bound_targets SET status='quarantined', "
            "claimed_at=NULL, claimed_by=NULL, claim_lease_id=NULL, "
            "quarantine_reason=? WHERE target_id=?",
            (reason, target_id),
        )

    @staticmethod
    def _result(row, outcome: str) -> dict[str, object]:
        result = dict(row)
        result["outcome"] = outcome
        result["authority_result"] = {
            "claimable": "AUTHORITY_BOUND_TARGET / RECORDED",
            "claimed": "AUTHORITY_BOUND_TARGET / CLAIMED",
            "quarantined": "AUTHORITY_BOUND_TARGET / QUARANTINED",
        }[row["status"]]
        return result

    @staticmethod
    def _snapshot_digest(snapshot: dict[str, object]) -> str:
        fields = {
            key: snapshot.get(key)
            for key in (
                "binding_id", "host_id", "profile_id", "binding_generation",
                "state", "verified_identity_ref", "verified_identity_event_ref",
                "verified_identity_revision", "status",
            )
        }
        return hashlib.sha256(
            json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _context_fingerprint(scope, lease, operation_kind, target_ref, key):
        values = (
            operation_kind, target_ref, key, scope.host_id, scope.profile_id,
            scope.binding_generation, scope.verified_identity_ref,
            scope.verified_identity_revision, lease.fence.authority_epoch,
            lease.fence.fence_counter,
        )
        return hashlib.sha256(
            json.dumps(values, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def _validate_reference(value, label, correlation_id):
        if not isinstance(value, str) or not _REFERENCE.fullmatch(value):
            raise _reject(correlation_id, label + "_invalid")

    @staticmethod
    def _validate_lease_scope(lease, scope, correlation_id):
        if lease.scope != scope:
            raise _reject(correlation_id, "authority_context_mismatch")

    @staticmethod
    def _require_same_context(row, scope, lease, operation_kind, target_ref, correlation_id):
        expected = AuthorityBoundTargetService._context_fingerprint(
            scope, lease, operation_kind, target_ref, row["idempotency_key"]
        )
        if row["context_fingerprint"] != expected:
            raise _reject(correlation_id, "idempotency_key_conflict")
