from __future__ import annotations

import hashlib
import json

from repositories.operational_sqlite import OperationalSQLiteRepository

from services.binding_authority import _reject
from services.binding_authority_types import AUTHORITY_ELIGIBILITY, BindingLease


class DispatchIntentStore:
    def __init__(self, operational: OperationalSQLiteRepository):
        self.operational = operational

    def execution_context(self, execution_id, correlation_id):
        execution = self.operational.connection.execute(
            "SELECT * FROM authority_bound_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if execution is None:
            raise _reject(correlation_id, "execution_not_found")
        attempt = self.operational.connection.execute(
            "SELECT a.*, t.job_id FROM authority_bound_attempts a "
            "JOIN authority_bound_targets t ON t.target_id=a.target_id "
            "WHERE a.execution_id=?",
            (execution_id,),
        ).fetchone()
        target = self.operational.connection.execute(
            "SELECT * FROM authority_bound_targets WHERE target_id=?",
            (execution["target_id"],),
        ).fetchone()
        if target is None:
            raise _reject(correlation_id, "target_not_found")
        return execution, attempt, target

    def intent_for_execution(self, execution_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_dispatch_intents WHERE execution_id=?",
            (execution_id,),
        ).fetchone()

    def intent(self, intent_id):
        return self.operational.connection.execute(
            "SELECT * FROM authority_bound_dispatch_intents WHERE intent_id=?",
            (intent_id,),
        ).fetchone()

    def block(self, intent_id, reason):
        self.operational.connection.execute(
            "UPDATE authority_bound_dispatch_intents SET status='blocked', "
            "blocked_reason=? WHERE intent_id=?",
            (reason, intent_id),
        )

    @staticmethod
    def request_fingerprint(target, execution):
        values = {
            "execution_id": execution["execution_id"],
            "job_id": target["job_id"],
            "operation_kind": target["operation_kind"],
            "target_ref": target["target_ref"],
            "idempotency_key": target["idempotency_key"],
        }
        return hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    @staticmethod
    def require_context(row, target, lease: BindingLease, owner_id, correlation_id):
        expected = (
            target["target_id"], target["job_id"], target["operation_kind"],
            target["target_ref"], lease.scope.host_id, lease.scope.profile_id,
            lease.scope.binding_generation, lease.scope.verified_identity_ref,
            lease.scope.verified_identity_revision, lease.fence.authority_epoch,
            lease.fence.fence_counter, owner_id, lease.lease_id,
        )
        actual = (
            row["target_id"], row["job_id"], row["operation_kind"], row["target_ref"],
            row["host_id"], row["profile_id"], row["binding_generation"],
            row["verified_identity_ref"], row["verified_identity_revision"],
            row["authority_epoch"], row["fence_counter"], row["owner_id"],
            row["lease_id"],
        )
        if actual != expected:
            raise _reject(correlation_id, "dispatch_intent_authority_mismatch")

    @staticmethod
    def require_claimed(execution, attempt, correlation_id):
        if execution["status"] != "claimed" or attempt["status"] != "claimed":
            raise _reject(correlation_id, "execution_not_claimed")

    @staticmethod
    def result(row, outcome):
        result = dict(row)
        result["outcome"] = outcome
        result["authority_eligibility"] = (
            AUTHORITY_ELIGIBILITY if row["status"] == "prepared" else None
        )
        return result
