from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from services.authority_bound_targets import AuthorityBoundTargetService


@dataclass(frozen=True, slots=True)
class ShadowEnvelope:
    execution_id: str
    idempotency_key: str
    job_id: str
    operation_kind: str
    target_ref: str

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            {
                "execution_id": self.execution_id,
                "idempotency_key": self.idempotency_key,
                "job_id": self.job_id,
                "operation_kind": self.operation_kind,
                "target_ref": self.target_ref,
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def build_shadow_envelope(execution, target, correlation_id: str) -> ShadowEnvelope:
    for value, label in (
        (execution["execution_id"], "execution_id"),
        (target["idempotency_key"], "idempotency_key"),
        (target["job_id"], "job_id"),
        (target["operation_kind"], "operation_kind"),
        (target["target_ref"], "target_ref"),
    ):
        AuthorityBoundTargetService._validate_reference(value, label, correlation_id)
    return ShadowEnvelope(
        execution_id=execution["execution_id"],
        idempotency_key=target["idempotency_key"],
        job_id=target["job_id"],
        operation_kind=target["operation_kind"],
        target_ref=target["target_ref"],
    )


def snapshot_fingerprint(snapshot) -> str:
    fields = {
        key: snapshot.get(key)
        for key in (
            "binding_id",
            "host_id",
            "profile_id",
            "binding_generation",
            "state",
            "verified_identity_ref",
            "verified_identity_event_ref",
            "verified_identity_revision",
            "status",
        )
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
