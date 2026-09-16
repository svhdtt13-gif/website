from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final

from services.authority_bound_targets import AuthorityBoundTargetService

CANARY_CONTRACT_VERSION: Final = "is3b1.v1"


def _canonical(values) -> str:
    return json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _digest(values) -> str:
    return hashlib.sha256(_canonical(values).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CanaryEnvelope:
    shadow_evaluation_id: str
    intent_id: str
    execution_id: str
    attempt_id: str
    target_id: str
    job_id: str
    operation_kind: str
    destination_ref: str
    host_id: str
    profile_id: str
    binding_generation: int
    verified_identity_ref: str
    verified_identity_revision: int
    authority_epoch: str
    fence_counter: int
    owner_id: str
    lease_id: str
    contract_version: str
    canary_idempotency_key: str
    pre_send_identity: str

    @property
    def canonical_json(self) -> str:
        return _canonical(
            {field: getattr(self, field) for field in self.__dataclass_fields__}
        )

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


def build_canary_envelope(shadow, intent, execution, attempt, target, correlation_id):
    references = {
        "shadow_evaluation_id": shadow["shadow_evaluation_id"],
        "intent_id": intent["intent_id"],
        "execution_id": execution["execution_id"],
        "attempt_id": attempt["attempt_id"],
        "target_id": target["target_id"],
        "job_id": target["job_id"],
        "operation_kind": target["operation_kind"],
        "destination_ref": target["target_ref"],
        "host_id": intent["host_id"],
        "profile_id": intent["profile_id"],
        "verified_identity_ref": intent["verified_identity_ref"],
        "authority_epoch": intent["authority_epoch"],
        "owner_id": intent["owner_id"],
        "lease_id": intent["lease_id"],
        "contract_version": CANARY_CONTRACT_VERSION,
    }
    for label, value in references.items():
        AuthorityBoundTargetService._validate_reference(value, label, correlation_id)
    identity_material = {
        **references,
        "binding_generation": intent["binding_generation"],
        "verified_identity_revision": intent["verified_identity_revision"],
        "fence_counter": intent["fence_counter"],
        "shadow_envelope_fingerprint": shadow["envelope_fingerprint"],
    }
    canary_key = "canary-key-" + _digest(identity_material)
    pre_send_identity = "pre-send-" + _digest(
        {**identity_material, "canary_idempotency_key": canary_key}
    )
    return CanaryEnvelope(
        **references,
        binding_generation=intent["binding_generation"],
        verified_identity_revision=intent["verified_identity_revision"],
        fence_counter=intent["fence_counter"],
        canary_idempotency_key=canary_key,
        pre_send_identity=pre_send_identity,
    )
