from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, fields, replace
from typing import Final, Protocol

from repositories.legacy_authority_types import AuthorizationArtifact, Handoff

from services.canary_envelope import CANARY_CONTRACT_VERSION, CanaryEnvelope

ACK_CONTRACT_VERSION: Final = "lcr2b.ack.v1"
_HANDOFF_ID_DOMAIN: Final = "lcr2b.handoff-id.v1"
_HANDOFF_ATTESTATION_DOMAIN: Final = "lcr2b.handoff-attestation.v1"
_ARTIFACT_FINGERPRINT_DOMAIN: Final = "lcr2b.authorization-artifact.v1"
_ACK_ATTESTATION_DOMAIN: Final = "lcr2b.authorization-ack.v1"
_TRANSPORT_CONTRACT_VERSION: Final = "is3b2.v1"
_ENVELOPE_FIELDS: Final = tuple(field.name for field in fields(CanaryEnvelope))


class HmacKeyProvider(Protocol):
    def key_for(self, identity: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class HandoffTrustError(ValueError):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class AuthenticatedAuthorizationAck:
    handoff_id: str
    pre_send_identity: str
    envelope_fingerprint: str
    authorization_artifact_fingerprint: str
    contract_version: str
    coordinator_identity: str
    coordinator_attestation: str


def _canonical(payload) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _key(key_provider: HmacKeyProvider, identity: str) -> bytes:
    try:
        key = key_provider.key_for(identity)
    except KeyError as error:
        raise HandoffTrustError("trusted identity key is unavailable") from error
    if type(key) is not bytes or not key:
        raise HandoffTrustError("trusted identity key is invalid")
    return key


def _attestation(domain: str, payload, identity: str, key_provider: HmacKeyProvider) -> str:
    message = _canonical({"domain": domain, "payload": payload}).encode("utf-8")
    digest = hmac.new(_key(key_provider, identity), message, hashlib.sha256).hexdigest()
    return "hmac-sha256:" + digest


def deterministic_handoff_id(pre_send_identity: str) -> str:
    material = _canonical(
        {
            "domain": _HANDOFF_ID_DOMAIN,
            "pre_send_identity": pre_send_identity,
            "transport_contract_version": _TRANSPORT_CONTRACT_VERSION,
        }
    )
    return "handoff-v1-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _handoff_payload(handoff: Handoff) -> dict[str, str | int]:
    return {
        "authority_epoch": handoff.authority_epoch,
        "binding_generation": handoff.binding_generation,
        "canary_idempotency_key": handoff.canary_idempotency_key,
        "canonical_envelope_json": handoff.canonical_envelope_json,
        "envelope_fingerprint": handoff.envelope_fingerprint,
        "exporter_identity": handoff.exporter_identity,
        "fence_counter": handoff.fence_counter,
        "handoff_id": handoff.handoff_id,
        "operation_kind": handoff.operation_kind,
        "pre_send_identity": handoff.pre_send_identity,
        "source_envelope_contract_version": handoff.source_envelope_contract_version,
        "target_ref": handoff.target_ref,
        "transport_contract_version": handoff.transport_contract_version,
        "verified_identity_ref": handoff.verified_identity_ref,
        "verified_identity_revision": handoff.verified_identity_revision,
    }


def attest_handoff(handoff: Handoff, key_provider: HmacKeyProvider) -> Handoff:
    attestation = _attestation(
        _HANDOFF_ATTESTATION_DOMAIN,
        _handoff_payload(handoff),
        handoff.exporter_identity,
        key_provider,
    )
    return replace(handoff, exporter_attestation=attestation)


def authorization_artifact_fingerprint(artifact: AuthorizationArtifact) -> str:
    names = tuple(field.name for field in fields(AuthorizationArtifact))
    payload = {
        name: getattr(artifact, name)
        for name in names
        if name != "authorization_artifact_fingerprint"
    }
    canonical = _canonical(
        {"domain": _ARTIFACT_FINGERPRINT_DOMAIN, "payload": payload}
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def sign_authorization_ack(
    artifact: AuthorizationArtifact,
    coordinator_identity: str,
    key_provider: HmacKeyProvider,
) -> AuthenticatedAuthorizationAck:
    ack = AuthenticatedAuthorizationAck(
        handoff_id=artifact.handoff_id,
        pre_send_identity=artifact.pre_send_identity,
        envelope_fingerprint=artifact.envelope_fingerprint,
        authorization_artifact_fingerprint=artifact.authorization_artifact_fingerprint,
        contract_version=ACK_CONTRACT_VERSION,
        coordinator_identity=coordinator_identity,
        coordinator_attestation="pending",
    )
    payload = _ack_payload(ack)
    return replace(
        ack,
        coordinator_attestation=_attestation(
            _ACK_ATTESTATION_DOMAIN, payload, coordinator_identity, key_provider
        ),
    )


def _ack_payload(
    ack: AuthenticatedAuthorizationAck,
) -> dict[str, str]:
    return {
        field.name: getattr(ack, field.name)
        for field in fields(AuthenticatedAuthorizationAck)
        if field.name != "coordinator_attestation"
    }


class TrustedAckVerifier:
    def __init__(self, coordinator_identity: str, key_provider: HmacKeyProvider) -> None:
        self._coordinator_identity = coordinator_identity
        self._key_provider = key_provider

    def verify(self, ack: AuthenticatedAuthorizationAck) -> None:
        if type(ack) is not AuthenticatedAuthorizationAck:
            raise HandoffTrustError("authorization ACK type is invalid")
        if ack.coordinator_identity != self._coordinator_identity:
            raise HandoffTrustError("authorization ACK coordinator is untrusted")
        if ack.contract_version != ACK_CONTRACT_VERSION:
            raise HandoffTrustError("authorization ACK contract is invalid")
        expected = _attestation(
            _ACK_ATTESTATION_DOMAIN,
            _ack_payload(ack),
            ack.coordinator_identity,
            self._key_provider,
        )
        if not hmac.compare_digest(ack.coordinator_attestation, expected):
            raise HandoffTrustError("authorization ACK attestation is invalid")


class TrustedHandoffVerifier:
    def __init__(self, exporter_identity: str, key_provider: HmacKeyProvider) -> None:
        self._exporter_identity = exporter_identity
        self._key_provider = key_provider

    def verify(self, handoff: Handoff) -> CanaryEnvelope:
        if type(handoff) is not Handoff:
            raise HandoffTrustError("handoff type is invalid")
        if handoff.exporter_identity != self._exporter_identity:
            raise HandoffTrustError("handoff exporter is untrusted")
        expected = attest_handoff(
            replace(handoff, exporter_attestation="pending"), self._key_provider
        ).exporter_attestation
        if not hmac.compare_digest(handoff.exporter_attestation, expected):
            raise HandoffTrustError("handoff attestation is invalid")
        if handoff.handoff_id != deterministic_handoff_id(handoff.pre_send_identity):
            raise HandoffTrustError("handoff identity is invalid")
        envelope = _parse_envelope(handoff.canonical_envelope_json)
        if hashlib.sha256(envelope.canonical_json.encode("utf-8")).hexdigest() != handoff.envelope_fingerprint:
            raise HandoffTrustError("handoff envelope fingerprint is invalid")
        expected_projection = (
            envelope.contract_version,
            envelope.pre_send_identity,
            envelope.canary_idempotency_key,
            envelope.operation_kind,
            envelope.destination_ref,
            envelope.binding_generation,
            envelope.verified_identity_ref,
            envelope.verified_identity_revision,
            envelope.authority_epoch,
            envelope.fence_counter,
        )
        actual_projection = (
            handoff.source_envelope_contract_version,
            handoff.pre_send_identity,
            handoff.canary_idempotency_key,
            handoff.operation_kind,
            handoff.target_ref,
            handoff.binding_generation,
            handoff.verified_identity_ref,
            handoff.verified_identity_revision,
            handoff.authority_epoch,
            handoff.fence_counter,
        )
        if actual_projection != expected_projection:
            raise HandoffTrustError("handoff envelope projection is invalid")
        return envelope


def _parse_envelope(canonical_json: str) -> CanaryEnvelope:
    try:
        parsed = json.loads(canonical_json)
    except json.JSONDecodeError as error:
        raise HandoffTrustError("handoff envelope JSON is invalid") from error
    if type(parsed) is not dict or set(parsed) != set(_ENVELOPE_FIELDS):
        raise HandoffTrustError("handoff envelope shape is invalid")
    if _canonical(parsed) != canonical_json:
        raise HandoffTrustError("handoff envelope JSON is not canonical")
    string_fields = _ENVELOPE_FIELDS[:10] + _ENVELOPE_FIELDS[11:12] + _ENVELOPE_FIELDS[13:14] + _ENVELOPE_FIELDS[15:]
    if any(type(parsed[name]) is not str for name in string_fields):
        raise HandoffTrustError("handoff envelope string field is invalid")
    for name in ("binding_generation", "verified_identity_revision", "fence_counter"):
        if type(parsed[name]) is not int:
            raise HandoffTrustError("handoff envelope counter field is invalid")
    envelope = CanaryEnvelope(
        shadow_evaluation_id=parsed["shadow_evaluation_id"],
        intent_id=parsed["intent_id"],
        execution_id=parsed["execution_id"],
        attempt_id=parsed["attempt_id"],
        target_id=parsed["target_id"],
        job_id=parsed["job_id"],
        operation_kind=parsed["operation_kind"],
        destination_ref=parsed["destination_ref"],
        host_id=parsed["host_id"],
        profile_id=parsed["profile_id"],
        binding_generation=parsed["binding_generation"],
        verified_identity_ref=parsed["verified_identity_ref"],
        verified_identity_revision=parsed["verified_identity_revision"],
        authority_epoch=parsed["authority_epoch"],
        fence_counter=parsed["fence_counter"],
        owner_id=parsed["owner_id"],
        lease_id=parsed["lease_id"],
        contract_version=parsed["contract_version"],
        canary_idempotency_key=parsed["canary_idempotency_key"],
        pre_send_identity=parsed["pre_send_identity"],
    )
    if envelope.contract_version != CANARY_CONTRACT_VERSION:
        raise HandoffTrustError("handoff envelope contract is invalid")
    return envelope
