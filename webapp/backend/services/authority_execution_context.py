from __future__ import annotations

from services.authority_bound_targets import AuthorityBoundTargetService
from services.binding_authority import _reject


def require_claimed_target(target, lease, owner_id, correlation_id):
    if (
        target["status"] != "claimed"
        or target["claimed_by"] != owner_id
        or target["claim_lease_id"] != lease.lease_id
    ):
        raise _reject(correlation_id, "target_not_claimed")
    require_target_context(target, lease, correlation_id)


def require_target_context(target, lease, correlation_id):
    expected = AuthorityBoundTargetService._context_fingerprint(
        lease.scope,
        lease,
        target["operation_kind"],
        target["target_ref"],
        target["idempotency_key"],
    )
    if target["context_fingerprint"] != expected:
        raise _reject(correlation_id, "target_context_mismatch")
    if (
        target["host_id"],
        target["profile_id"],
        target["binding_generation"],
        target["verified_identity_ref"],
        target["verified_identity_revision"],
        target["authority_epoch"],
        target["fence_counter"],
    ) != (
        lease.scope.host_id,
        lease.scope.profile_id,
        lease.scope.binding_generation,
        lease.scope.verified_identity_ref,
        lease.scope.verified_identity_revision,
        lease.fence.authority_epoch,
        lease.fence.fence_counter,
    ):
        raise _reject(correlation_id, "target_authority_mismatch")


def require_execution_context(
    execution, attempt, target, lease, owner_id, correlation_id
):
    if attempt is None:
        raise _reject(correlation_id, "attempt_not_found")
    require_target_context(target, lease, correlation_id)
    expected = (
        target["target_id"],
        target["job_id"],
        target["idempotency_key"],
        lease.scope.host_id,
        lease.scope.profile_id,
        lease.scope.binding_generation,
        lease.scope.verified_identity_ref,
        lease.scope.verified_identity_revision,
        lease.fence.authority_epoch,
        lease.fence.fence_counter,
        owner_id,
        lease.lease_id,
    )
    for row in (execution, attempt):
        actual = (
            row["target_id"],
            row["job_id"],
            row["idempotency_key"],
            row["host_id"],
            row["profile_id"],
            row["binding_generation"],
            row["verified_identity_ref"],
            row["verified_identity_revision"],
            row["authority_epoch"],
            row["fence_counter"],
            row["owner_id"],
            row["lease_id"],
        )
        if actual != expected:
            raise _reject(correlation_id, "execution_authority_mismatch")
    if execution["attempt_count"] != 1 or attempt["attempt_number"] != 1:
        raise _reject(correlation_id, "attempt_identity_invalid")


def ownership_values(target, lease, owner_id):
    return (
        target["idempotency_key"],
        lease.scope.host_id,
        lease.scope.profile_id,
        lease.scope.binding_generation,
        lease.scope.verified_identity_ref,
        lease.scope.verified_identity_revision,
        lease.fence.authority_epoch,
        lease.fence.fence_counter,
        owner_id,
        lease.lease_id,
    )


def validate_reference(value, label, correlation_id):
    if not isinstance(value, str) or not value.strip():
        raise _reject(correlation_id, label + "_invalid")
