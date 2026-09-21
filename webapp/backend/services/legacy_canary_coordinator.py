from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from repositories.legacy_authority_store import LegacyAuthorityStore
from repositories.legacy_authority_store_types import ReceiptConflictError
from repositories.legacy_authority_types import (
    REQUESTED_STATE,
    TRANSPORT_CONTRACT_VERSION,
    AuthorizationArtifact,
    AuthorizationLookup,
    EnvelopeAuthorizationLookup,
    Handoff,
    HandoffAuthorizationLookup,
    StoredAuthorization,
)

from services.canary_envelope import CanaryEnvelope
from services.legacy_handoff_trust import (
    AuthenticatedAuthorizationAck,
    HandoffTrustError,
    HmacKeyProvider,
    TrustedHandoffVerifier,
    authorization_artifact_fingerprint,
    sign_authorization_ack,
)


@dataclass(frozen=True, slots=True)
class LegacyAuthorizationContext:
    canary_run_id: str
    fence_identity: str
    source_identity_ref: str
    artifact_producer_identity: str
    pre_operation_observation_generation_floor: int
    authority_epoch: str
    fence_counter: int


class AuthorizationContextProvider(Protocol):
    def context_for(
        self, handoff: Handoff, envelope: CanaryEnvelope
    ) -> LegacyAuthorizationContext: ...


class ProductionAuthorizationContextProvider:
    def context_for(
        self, handoff: Handoff, envelope: CanaryEnvelope
    ) -> LegacyAuthorizationContext:
        del handoff, envelope
        raise HandoffTrustError("production authorization context is unavailable")


@dataclass(frozen=True, slots=True)
class DeterministicTestAuthorizationContextProvider:
    context: LegacyAuthorizationContext

    def context_for(
        self, handoff: Handoff, envelope: CanaryEnvelope
    ) -> LegacyAuthorizationContext:
        del handoff, envelope
        return self.context


@dataclass(frozen=True, slots=True)
class LegacyCoordinatorTrust:
    handoff_verifier: TrustedHandoffVerifier
    coordinator_identity: str
    key_provider: HmacKeyProvider


class LegacyCanaryCoordinator:
    def __init__(
        self,
        store: LegacyAuthorityStore,
        context_provider: AuthorizationContextProvider,
        trust: LegacyCoordinatorTrust,
    ) -> None:
        self._store = store
        self._context_provider = context_provider
        self._trust = trust

    def authorize(self, handoff: Handoff) -> AuthenticatedAuthorizationAck:
        if type(handoff) is not Handoff:
            raise HandoffTrustError("handoff type is invalid")
        envelope = self._trust.handoff_verifier.verify(handoff)
        stored = self._store.lookup_authorization(
            AuthorizationLookup(handoff.pre_send_identity)
        )
        if stored is not None:
            if stored.handoff != handoff:
                raise ReceiptConflictError("durable handoff identity conflict")
            return self._ack_from_stored(stored)
        for conflict_lookup in (
            HandoffAuthorizationLookup(handoff.handoff_id),
            EnvelopeAuthorizationLookup(handoff.envelope_fingerprint),
        ):
            if self._store.lookup_authorization(conflict_lookup) is not None:
                raise ReceiptConflictError("durable handoff fingerprint conflict")
        context = self._context_provider.context_for(handoff, envelope)
        self._require_context_binding(handoff, context)
        artifact = AuthorizationArtifact(
            handoff_id=handoff.handoff_id,
            pre_send_identity=handoff.pre_send_identity,
            canary_idempotency_key=handoff.canary_idempotency_key,
            canary_run_id=context.canary_run_id,
            fence_identity=context.fence_identity,
            authority_epoch=context.authority_epoch,
            fence_counter=context.fence_counter,
            source_identity_ref=context.source_identity_ref,
            artifact_producer_identity=context.artifact_producer_identity,
            source_envelope_contract_version=handoff.source_envelope_contract_version,
            transport_contract_version=handoff.transport_contract_version,
            contract_version=TRANSPORT_CONTRACT_VERSION,
            envelope_exporter_identity=handoff.exporter_identity,
            operation_kind=handoff.operation_kind,
            target_ref=handoff.target_ref,
            requested_state=REQUESTED_STATE,
            pre_operation_observation_generation_floor=(
                context.pre_operation_observation_generation_floor
            ),
            envelope_fingerprint=handoff.envelope_fingerprint,
            authorization_artifact_fingerprint="pending",
        )
        artifact = replace(
            artifact,
            authorization_artifact_fingerprint=authorization_artifact_fingerprint(
                artifact
            ),
        )
        self._store.add_authorization(handoff, artifact)
        durable = self._store.lookup_authorization(
            AuthorizationLookup(handoff.pre_send_identity)
        )
        if durable is None:
            raise HandoffTrustError("durable authorization is unavailable")
        return self._ack_from_stored(durable)

    def recover_ack(
        self, lookup: AuthorizationLookup
    ) -> AuthenticatedAuthorizationAck | None:
        stored = self._store.lookup_authorization(lookup)
        if stored is None:
            return None
        self._trust.handoff_verifier.verify(stored.handoff)
        return self._ack_from_stored(stored)

    def _ack_from_stored(
        self, stored: StoredAuthorization
    ) -> AuthenticatedAuthorizationAck:
        stored.artifact.require_handoff(stored.handoff)
        expected = authorization_artifact_fingerprint(stored.artifact)
        if stored.artifact.authorization_artifact_fingerprint != expected:
            raise ReceiptConflictError("durable authorization artifact conflict")
        if (
            stored.artifact.artifact_producer_identity
            != self._trust.coordinator_identity
        ):
            raise ReceiptConflictError("durable authorization producer conflict")
        return sign_authorization_ack(
            stored.artifact,
            self._trust.coordinator_identity,
            self._trust.key_provider,
        )

    def _require_context_binding(
        self, handoff: Handoff, context: LegacyAuthorizationContext
    ) -> None:
        if (
            context.authority_epoch != handoff.authority_epoch
            or context.fence_counter != handoff.fence_counter
            or context.artifact_producer_identity
            != self._trust.coordinator_identity
        ):
            raise HandoffTrustError("authorization context binding is invalid")
