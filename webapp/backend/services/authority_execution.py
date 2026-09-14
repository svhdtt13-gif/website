from __future__ import annotations

import secrets
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.authority_execution_context import (
    ownership_values,
    require_claimed_target,
    require_execution_context,
    validate_reference,
)
from services.binding_authority import (
    BindingAuthorityCoordinator,
    _correlation_id,
    _reject,
)
from services.binding_authority_types import (
    AUTHORITY_ELIGIBILITY,
    AuthorityRejected,
    BindingLease,
)


class AuthorityExecutionService:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        coordinator: BindingAuthorityCoordinator,
    ):
        self.portable_path = Path(portable_path)
        self.operational = operational
        self.coordinator = coordinator

    def create_execution(
        self,
        target_id: str,
        lease: BindingLease,
        owner_id: str,
        request_id: str | None = None,
    ) -> dict[str, object]:
        correlation_id = _correlation_id(request_id)
        validate_reference(target_id, "target_id", correlation_id)
        validate_reference(owner_id, "owner_id", correlation_id)
        snapshot = self.coordinator._require_snapshot(lease.scope, correlation_id)

        with self.operational.transaction():
            target = self._target(target_id)
            if target is None:
                raise _reject(correlation_id, "target_not_found")
            require_claimed_target(target, lease, owner_id, correlation_id)
            self.coordinator._current_lease_row(
                lease, owner_id, self.coordinator._now(correlation_id), correlation_id
            )

            existing = self._execution_for_target(target_id)
            if existing is not None:
                attempt = self._attempt_for_execution(existing["execution_id"])
                require_execution_context(
                    existing, attempt, target, lease, owner_id, correlation_id
                )
                self.coordinator._current_lease_row(
                    lease, owner_id, self.coordinator._now(correlation_id), correlation_id
                )
                self.coordinator._revalidate_snapshot(
                    lease.scope, snapshot, correlation_id
                )
                return self._result(existing, attempt, "idempotent_replay")

            execution_id = "execution-" + secrets.token_hex(16)
            attempt_id = "attempt-" + secrets.token_hex(16)
            now = self.coordinator._now(correlation_id).isoformat()
            values = ownership_values(target, lease, owner_id)
            self.operational.connection.execute(
                "INSERT INTO authority_bound_executions ("
                "execution_id, target_id, job_id, idempotency_key, host_id, "
                "profile_id, binding_generation, verified_identity_ref, "
                "verified_identity_revision, authority_epoch, fence_counter, "
                "owner_id, lease_id, status, attempt_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'claimed', 1, ?)",
                (execution_id, target["target_id"], target["job_id"], *values, now),
            )
            self.operational.connection.execute(
                "INSERT INTO authority_bound_attempts ("
                "attempt_id, execution_id, target_id, attempt_number, "
                "idempotency_key, host_id, profile_id, binding_generation, "
                "verified_identity_ref, verified_identity_revision, "
                "authority_epoch, fence_counter, owner_id, lease_id, status, "
                "created_at) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "'claimed', ?)",
                (attempt_id, execution_id, target["target_id"], *values, now),
            )
            try:
                self.coordinator._revalidate_snapshot(
                    lease.scope, snapshot, correlation_id
                )
                self.coordinator._current_lease_row(
                    lease, owner_id, self.coordinator._now(correlation_id), correlation_id
                )
            except AuthorityRejected as rejected:
                self._quarantine(execution_id, rejected.evidence.reason_class)
            execution = self._execution_for_target(target_id)
            attempt = self._attempt_for_execution(execution_id)
            return self._result(execution, attempt, "created")

    def validate_execution(
        self,
        execution_id: str,
        lease: BindingLease,
        owner_id: str,
        request_id: str | None = None,
    ) -> str:
        correlation_id = _correlation_id(request_id)
        validate_reference(execution_id, "execution_id", correlation_id)
        validate_reference(owner_id, "owner_id", correlation_id)
        snapshot = self.coordinator._require_snapshot(lease.scope, correlation_id)
        with self.operational.transaction(immediate=False):
            execution = self._execution(execution_id)
            if execution is None:
                raise _reject(correlation_id, "execution_not_found")
            attempt = self._attempt_for_execution(execution_id)
            target = self._target(execution["target_id"])
            if target is None:
                raise _reject(correlation_id, "target_not_found")
            require_execution_context(
                execution, attempt, target, lease, owner_id, correlation_id
            )
            if execution["status"] != "claimed" or attempt["status"] != "claimed":
                raise _reject(correlation_id, "execution_not_claimable")
            self.coordinator._current_lease_row(
                lease, owner_id, self.coordinator._now(correlation_id), correlation_id
            )
            self.coordinator._revalidate_snapshot(
                lease.scope, snapshot, correlation_id
            )
        return AUTHORITY_ELIGIBILITY

    def _target(self, target_id: str):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_targets WHERE target_id=?", (target_id,)
        ).fetchone()

    def _execution(self, execution_id: str):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()

    def _execution_for_target(self, target_id: str):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_executions WHERE target_id=?", (target_id,)
        ).fetchone()

    def _attempt_for_execution(self, execution_id: str):
        return self.operational.connection.execute(
            "SELECT a.*, t.job_id FROM authority_bound_attempts a "
            "JOIN authority_bound_targets t ON t.target_id=a.target_id "
            "WHERE a.execution_id=?",
            (execution_id,),
        ).fetchone()

    def _quarantine(self, execution_id: str, reason: str):
        self.operational.connection.execute(
            "UPDATE authority_bound_targets SET status='quarantined', "
            "claimed_at=NULL, claimed_by=NULL, claim_lease_id=NULL, "
            "quarantine_reason=? WHERE target_id=(SELECT target_id "
            "FROM authority_bound_executions WHERE execution_id=?)",
            (reason, execution_id),
        )
        self.operational.connection.execute(
            "UPDATE authority_bound_executions SET status='quarantined', "
            "quarantine_reason=? WHERE execution_id=?",
            (reason, execution_id),
        )
        self.operational.connection.execute(
            "UPDATE authority_bound_attempts SET status='quarantined', "
            "quarantine_reason=? WHERE execution_id=?",
            (reason, execution_id),
        )

    @staticmethod
    def _result(execution, attempt, outcome):
        result = dict(execution)
        result.update(
            {
                "attempt_id": attempt["attempt_id"],
                "attempt_status": attempt["status"],
                "attempt_number": attempt["attempt_number"],
                "outcome": outcome,
                "authority_eligibility": AUTHORITY_ELIGIBILITY
                if execution["status"] == "claimed"
                and attempt["status"] == "claimed"
                else None,
            }
        )
        return result
