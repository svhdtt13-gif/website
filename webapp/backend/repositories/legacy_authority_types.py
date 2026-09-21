from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

SOURCE_CONTRACT_VERSION: Final = "is3b1.v1"
TRANSPORT_CONTRACT_VERSION: Final = "is3b2.v1"
OPERATION_KIND: Final = "group_on"
REQUESTED_STATE: Final = "running"

_SECRET_MARKERS: Final = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
    "http://",
    "https://",
)


class LegacyValidationError(ValueError):
    """Raised when untrusted LEGACY authority input violates the contract."""


class FenceState(StrEnum):
    REQUESTED = "requested"
    ACQUIRED = "acquired"
    RECEIPT_ACCEPTED = "receipt_accepted"
    MUTATION = "mutation"
    RECEIPT_TERMINAL = "receipt_terminal"
    OBSERVATION_PENDING = "observation_pending"
    CLOSED = "closed"
    ABANDONED = "abandoned"


class ReceiptState(StrEnum):
    ACCEPTED = "accepted"
    DISPATCHING = "dispatching"
    APPLIED = "applied"
    NOT_APPLIED_PROVEN = "not_applied_proven"
    UNKNOWN = "unknown"


def _required(value: str, field: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise LegacyValidationError(f"{field} must be a non-empty canonical value")


def _logical_reference(value: str, field: str) -> None:
    _required(value, field)
    lowered = value.casefold()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise LegacyValidationError(f"{field} must be a non-secret logical reference")


def _target_reference(value: str) -> None:
    _logical_reference(value, "target_ref")
    lowered = value.casefold()
    if lowered.startswith("fixed:") or "/fixed/" in lowered:
        raise LegacyValidationError("target_ref must identify one non-fixed target")


def _canonical_envelope(value: str) -> None:
    _required(value, "canonical_envelope_json")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as error:
        raise LegacyValidationError("canonical_envelope_json must be valid JSON") from error
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    if canonical != value:
        raise LegacyValidationError("canonical_envelope_json must be canonical JSON")
    lowered = value.casefold()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise LegacyValidationError("canonical_envelope_json contains secret material")


@dataclass(frozen=True, slots=True)
class Handoff:
    handoff_id: str
    source_envelope_contract_version: str
    transport_contract_version: str
    pre_send_identity: str
    canary_idempotency_key: str
    envelope_fingerprint: str
    canonical_envelope_json: str
    operation_kind: str
    target_ref: str
    binding_generation: int
    verified_identity_ref: str
    verified_identity_revision: int
    authority_epoch: str
    fence_counter: int
    exporter_identity: str
    exporter_attestation: str

    def __post_init__(self) -> None:
        for field, value in (
            ("handoff_id", self.handoff_id),
            ("pre_send_identity", self.pre_send_identity),
            ("canary_idempotency_key", self.canary_idempotency_key),
            ("envelope_fingerprint", self.envelope_fingerprint),
            ("authority_epoch", self.authority_epoch),
        ):
            _required(value, field)
        for field, value in (
            ("verified_identity_ref", self.verified_identity_ref),
            ("exporter_identity", self.exporter_identity),
            ("exporter_attestation", self.exporter_attestation),
        ):
            _logical_reference(value, field)
        if self.source_envelope_contract_version != SOURCE_CONTRACT_VERSION:
            raise LegacyValidationError("source envelope contract must be is3b1.v1")
        if self.transport_contract_version != TRANSPORT_CONTRACT_VERSION:
            raise LegacyValidationError("transport contract must be is3b2.v1")
        if self.operation_kind != OPERATION_KIND:
            raise LegacyValidationError("operation_kind must be group_on")
        _target_reference(self.target_ref)
        _canonical_envelope(self.canonical_envelope_json)
        if self.binding_generation < 1 or self.verified_identity_revision < 1:
            raise LegacyValidationError("identity generations must be positive")
        if self.fence_counter < 1:
            raise LegacyValidationError("fence_counter must be positive")

    def projection(self) -> tuple[str | int, ...]:
        return (
            self.handoff_id,
            self.source_envelope_contract_version,
            self.transport_contract_version,
            self.pre_send_identity,
            self.canary_idempotency_key,
            self.envelope_fingerprint,
            self.canonical_envelope_json,
            self.operation_kind,
            self.target_ref,
            self.binding_generation,
            self.verified_identity_ref,
            self.verified_identity_revision,
            self.authority_epoch,
            self.fence_counter,
            self.exporter_identity,
            self.exporter_attestation,
        )


@dataclass(frozen=True, slots=True)
class AuthorizationArtifact:
    handoff_id: str
    pre_send_identity: str
    canary_idempotency_key: str
    canary_run_id: str
    fence_identity: str
    authority_epoch: str
    fence_counter: int
    source_identity_ref: str
    artifact_producer_identity: str
    source_envelope_contract_version: str
    transport_contract_version: str
    contract_version: str
    envelope_exporter_identity: str
    operation_kind: str
    target_ref: str
    requested_state: str
    pre_operation_observation_generation_floor: int
    envelope_fingerprint: str
    authorization_artifact_fingerprint: str

    def __post_init__(self) -> None:
        for field, value in (
            ("handoff_id", self.handoff_id),
            ("pre_send_identity", self.pre_send_identity),
            ("canary_idempotency_key", self.canary_idempotency_key),
            ("canary_run_id", self.canary_run_id),
            ("fence_identity", self.fence_identity),
            ("authority_epoch", self.authority_epoch),
            ("envelope_fingerprint", self.envelope_fingerprint),
            ("authorization_artifact_fingerprint", self.authorization_artifact_fingerprint),
        ):
            _required(value, field)
        for field, value in (
            ("source_identity_ref", self.source_identity_ref),
            ("artifact_producer_identity", self.artifact_producer_identity),
            ("envelope_exporter_identity", self.envelope_exporter_identity),
        ):
            _logical_reference(value, field)
        versions = (
            self.source_envelope_contract_version == SOURCE_CONTRACT_VERSION
            and self.transport_contract_version == TRANSPORT_CONTRACT_VERSION
            and self.contract_version == TRANSPORT_CONTRACT_VERSION
        )
        if not versions:
            raise LegacyValidationError("authorization artifact contract versions are invalid")
        if self.operation_kind != OPERATION_KIND or self.requested_state != REQUESTED_STATE:
            raise LegacyValidationError("authorization artifact permits only group_on to running")
        _target_reference(self.target_ref)
        if self.fence_counter < 1 or self.pre_operation_observation_generation_floor < 0:
            raise LegacyValidationError("artifact counters must be non-negative and fenced")

    def require_handoff(self, handoff: Handoff) -> None:
        bindings = (
            self.handoff_id,
            self.pre_send_identity,
            self.canary_idempotency_key,
            self.authority_epoch,
            self.fence_counter,
            self.envelope_exporter_identity,
            self.source_envelope_contract_version,
            self.transport_contract_version,
            self.operation_kind,
            self.target_ref,
            self.envelope_fingerprint,
        )
        expected = (
            handoff.handoff_id,
            handoff.pre_send_identity,
            handoff.canary_idempotency_key,
            handoff.authority_epoch,
            handoff.fence_counter,
            handoff.exporter_identity,
            handoff.source_envelope_contract_version,
            handoff.transport_contract_version,
            handoff.operation_kind,
            handoff.target_ref,
            handoff.envelope_fingerprint,
        )
        if bindings != expected:
            raise LegacyValidationError("authorization artifact does not bind to handoff")

    def projection(self) -> tuple[str | int, ...]:
        return (
            self.handoff_id,
            self.pre_send_identity,
            self.canary_idempotency_key,
            self.canary_run_id,
            self.fence_identity,
            self.authority_epoch,
            self.fence_counter,
            self.source_identity_ref,
            self.artifact_producer_identity,
            self.source_envelope_contract_version,
            self.transport_contract_version,
            self.contract_version,
            self.envelope_exporter_identity,
            self.operation_kind,
            self.target_ref,
            self.requested_state,
            self.pre_operation_observation_generation_floor,
            self.envelope_fingerprint,
            self.authorization_artifact_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class Receipt:
    pre_send_identity: str
    envelope_fingerprint: str
    canary_run_id: str
    fence_identity: str
    authority_epoch: str
    fence_counter: int
    state: ReceiptState
    pre_operation_observation_generation_floor: int
    post_dispatch_observation_boundary_id: str | None = None
    post_dispatch_observation_generation_floor: int | None = None


@dataclass(frozen=True, slots=True)
class ReceiptDecision:
    receipt: Receipt
    mutation_allowed: bool
