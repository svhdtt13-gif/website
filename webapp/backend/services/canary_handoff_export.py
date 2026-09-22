from __future__ import annotations

from repositories.legacy_authority_types import (
    SOURCE_CONTRACT_VERSION,
    TRANSPORT_CONTRACT_VERSION,
    Handoff,
)

from services.canary_envelope import CanaryEnvelope
from services.legacy_handoff_trust import (
    HandoffTrustError,
    HmacKeyProvider,
    attest_handoff,
    deterministic_handoff_id,
)


class CanaryHandoffExporter:
    def __init__(self, exporter_identity: str, key_provider: HmacKeyProvider) -> None:
        if not exporter_identity or exporter_identity != exporter_identity.strip():
            raise HandoffTrustError("exporter identity is invalid")
        self._exporter_identity = exporter_identity
        self._key_provider = key_provider

    def export(self, envelope: CanaryEnvelope) -> Handoff:
        if type(envelope) is not CanaryEnvelope:
            raise HandoffTrustError("canary envelope type is invalid")
        if envelope.contract_version != SOURCE_CONTRACT_VERSION:
            raise HandoffTrustError("canary envelope contract is invalid")
        handoff = Handoff(
            handoff_id=deterministic_handoff_id(envelope.pre_send_identity),
            source_envelope_contract_version=envelope.contract_version,
            transport_contract_version=TRANSPORT_CONTRACT_VERSION,
            pre_send_identity=envelope.pre_send_identity,
            canary_idempotency_key=envelope.canary_idempotency_key,
            envelope_fingerprint=envelope.fingerprint,
            canonical_envelope_json=envelope.canonical_json,
            operation_kind=envelope.operation_kind,
            target_ref=envelope.destination_ref,
            binding_generation=envelope.binding_generation,
            verified_identity_ref=envelope.verified_identity_ref,
            verified_identity_revision=envelope.verified_identity_revision,
            authority_epoch=envelope.authority_epoch,
            fence_counter=envelope.fence_counter,
            exporter_identity=self._exporter_identity,
            exporter_attestation="pending",
        )
        return attest_handoff(handoff, self._key_provider)
