from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from repositories.legacy_authority_types import FenceState, Receipt, ReceiptState


class ObservationMaterializerError(ValueError):
    """Raised when observation evidence cannot be trusted or replayed."""


class ObservationMaterializerConflict(ObservationMaterializerError):
    """Raised when an immutable observation is replayed with different data."""


@dataclass(frozen=True, slots=True)
class ObservationEvidence:
    boundary_id: str
    generation_floor: int
    snapshot_generation_id: int
    snapshot_id: str
    captured_at: str
    materializer_run_id: str
    source_hash: str
    source_identity_ref: str
    canary_run_id: str
    fence_identity: str
    fence_counter: int
    target_ref: str
    requested_state: str
    observed_state: str
    attestation_fingerprint: str

    def __post_init__(self) -> None:
        for name in (
            "boundary_id", "snapshot_id", "materializer_run_id", "source_hash",
            "source_identity_ref", "canary_run_id", "fence_identity", "target_ref",
            "requested_state", "observed_state", "attestation_fingerprint",
        ):
            _reference(getattr(self, name), name)
        if type(self.generation_floor) is not int or type(self.snapshot_generation_id) is not int:
            raise ObservationMaterializerError("generation values must be integers")
        if self.generation_floor < 0 or self.snapshot_generation_id <= self.generation_floor:
            raise ObservationMaterializerError("snapshot generation must be later than generation floor")
        if type(self.fence_counter) is not int or self.fence_counter < 1:
            raise ObservationMaterializerError("fence_counter must be positive")
        try:
            datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ObservationMaterializerError("captured_at must be an ISO timestamp") from error

    def projection(self) -> tuple[str | int, ...]:
        return (
            self.boundary_id, self.generation_floor, self.snapshot_generation_id, self.snapshot_id,
            self.captured_at, self.materializer_run_id, self.source_hash,
            self.source_identity_ref, self.canary_run_id, self.fence_identity,
            self.fence_counter, self.target_ref, self.requested_state,
            self.observed_state, self.attestation_fingerprint,
        )


@dataclass(frozen=True, slots=True)
class MaterializerAck:
    receipt_identity: str
    evidence: ObservationEvidence


class _ObservationHost(Protocol):
    connection: sqlite3.Connection

    def _immediate(self) -> AbstractContextManager[None]: ...

    def _require_live(self) -> None: ...

    def _receipt_row(self, identity: str) -> sqlite3.Row | None: ...

    def get_fence(self, identity: str) -> sqlite3.Row | None: ...

    def _verify_generation_locked(self) -> None: ...

    def _verify_observation_attestation(self, evidence: ObservationEvidence) -> str: ...


def _reference(value: str, field: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise ObservationMaterializerError(f"{field} must be a canonical reference")
    if any(marker in value.casefold() for marker in ("authorization", "cookie", "credential", "password", "secret", "session", "token", "http://", "https://")):
        raise ObservationMaterializerError(f"{field} must not contain secret material")


def _digest(previous: str, authority_generation_id: int, evidence: ObservationEvidence) -> str:
    payload = json.dumps(
        (previous, authority_generation_id, evidence.projection()),
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _evidence_from_ack_row(row: sqlite3.Row) -> ObservationEvidence:
    return ObservationEvidence(
        row[1], row[2], row[4], row[5], row[6], row[7], row[8], row[9],
        row[10], row[11], row[12], row[13], row[14], row[15], row[16],
    )


class ObservationPersistenceMixin:
    def _record_observation_ack(
        self: _ObservationHost,
        receipt: Receipt,
        evidence: ObservationEvidence,
    ) -> MaterializerAck:
        self._require_live()
        _validate_binding(receipt, evidence)
        with self._immediate():
            trusted_attestation_fingerprint = self._verify_observation_attestation(evidence)
            if evidence.attestation_fingerprint != trusted_attestation_fingerprint:
                raise ObservationMaterializerError("detached observation attestation is not trusted")
            stored = self._receipt_row(receipt.pre_send_identity)
            fence = self.get_fence(receipt.pre_send_identity)
            if stored is None or fence is None or stored["state"] != ReceiptState.APPLIED.value or fence["state"] not in (FenceState.RECEIPT_TERMINAL.value, FenceState.OBSERVATION_PENDING.value):
                raise ObservationMaterializerError("stored receipt is not eligible for observation")
            if (
                stored["envelope_fingerprint"], stored["canary_run_id"], stored["fence_identity"],
                stored["authority_epoch"], stored["fence_counter"], stored["source_identity_ref"],
                stored["target_ref"], stored["post_dispatch_observation_boundary_id"],
                stored["post_dispatch_observation_generation_floor"],
                stored["pre_operation_observation_generation_floor"],
            ) != (
                receipt.envelope_fingerprint, receipt.canary_run_id, receipt.fence_identity,
                receipt.authority_epoch, receipt.fence_counter, receipt.source_identity_ref,
                receipt.target_ref, evidence.boundary_id, evidence.generation_floor,
                receipt.pre_operation_observation_generation_floor,
            ):
                raise ObservationMaterializerError("stored receipt lineage does not match observation")
            self._verify_generation_locked()
            existing = self.connection.execute(
                "SELECT * FROM observation_materializer_acks WHERE receipt_identity=?",
                (receipt.pre_send_identity,),
            ).fetchone()
            sequence = self.connection.execute(
                "SELECT last_generation_id,last_record_digest FROM observation_generation_sequence WHERE key='global'"
            ).fetchone()
            if sequence is None:
                raise ObservationMaterializerError("observation generation sequence is missing")
            authority_generation_id = max(
                int(sequence[0]),
                evidence.generation_floor,
            ) + 1
            values = (
                receipt.pre_send_identity,
                evidence.boundary_id,
                evidence.generation_floor,
                authority_generation_id,
                *evidence.projection()[2:],
            )
            if existing is not None:
                if tuple(existing[:3]) + tuple(existing[4:]) != values[:3] + values[4:]:
                    raise ObservationMaterializerConflict("observation ACK conflict")
                return MaterializerAck(receipt.pre_send_identity, evidence)
            previous = str(sequence[1])
            record_digest = _digest(previous, authority_generation_id, evidence)
            self.connection.execute(
                "INSERT INTO observation_generation_ledger VALUES (?,?,?,?,?)",
                (authority_generation_id, evidence.generation_floor, record_digest, previous, evidence.attestation_fingerprint),
            )
            self.connection.execute(
                "INSERT INTO observation_materializer_acks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            self.connection.execute(
                "UPDATE observation_generation_sequence SET last_generation_id=?,last_record_digest=? WHERE key='global'",
                (authority_generation_id, record_digest),
            )
        return MaterializerAck(receipt.pre_send_identity, evidence)

    def require_observation_evidence(
        self: _ObservationHost,
        receipt: Receipt,
    ) -> MaterializerAck:
        self._require_live()
        with self._immediate():
            self._verify_generation_locked()
            stored = self._receipt_row(receipt.pre_send_identity)
            fence = self.get_fence(receipt.pre_send_identity)
            if stored is None or fence is None or stored["state"] != ReceiptState.APPLIED.value:
                raise ObservationMaterializerError("stored receipt is missing")
            if fence["state"] != (receipt.fence_state.value if receipt.fence_state is not None else ""):
                raise ObservationMaterializerError("current fence state does not match receipt")
            row = self.connection.execute(
                "SELECT * FROM observation_materializer_acks WHERE receipt_identity=?",
                (receipt.pre_send_identity,),
            ).fetchone()
            if row is None:
                raise ObservationMaterializerError("post-dispatch evidence is missing")
            evidence = _evidence_from_ack_row(row)
            if evidence.attestation_fingerprint != self._verify_observation_attestation(evidence):
                raise ObservationMaterializerError("detached observation attestation is not trusted")
            if (
                stored["envelope_fingerprint"], stored["canary_run_id"], stored["fence_identity"],
                stored["authority_epoch"], stored["fence_counter"], stored["source_identity_ref"],
                stored["target_ref"], stored["post_dispatch_observation_boundary_id"],
                stored["post_dispatch_observation_generation_floor"],
                stored["pre_operation_observation_generation_floor"],
            ) != (
                receipt.envelope_fingerprint, receipt.canary_run_id, receipt.fence_identity,
                receipt.authority_epoch, receipt.fence_counter, receipt.source_identity_ref,
                receipt.target_ref, evidence.boundary_id, evidence.generation_floor,
                receipt.pre_operation_observation_generation_floor,
            ):
                raise ObservationMaterializerError("stored receipt lineage does not match observation")
            _validate_binding(receipt, evidence, allow_closed=True)
        return MaterializerAck(receipt.pre_send_identity, evidence)

    def _verify_generation_locked(self: _ObservationHost) -> None:
        rows = self.connection.execute(
            "SELECT generation_id,generation_floor,record_digest,previous_digest,attestation_fingerprint FROM observation_generation_ledger ORDER BY generation_id"
        ).fetchall()
        previous = ""
        last_id = 0
        for row in rows:
            if row[0] <= last_id or row[3] != previous:
                raise ObservationMaterializerError("observation generation ledger tampered")
            ack = self.connection.execute(
                "SELECT * FROM observation_materializer_acks WHERE generation_id=?",
                (row[0],),
            ).fetchone()
            if ack is None or _digest(previous, row[0], _evidence_from_ack_row(ack)) != row[2]:
                raise ObservationMaterializerError("observation generation ledger tampered")
            previous = row[2]
            last_id = row[0]
        head = self.connection.execute(
            "SELECT last_generation_id,last_record_digest FROM observation_generation_sequence WHERE key='global'"
        ).fetchone()
        if head is None or (rows and tuple(head) != (last_id, previous)) or (not rows and tuple(head) != (0, "")):
            raise ObservationMaterializerError("observation generation head tampered")


def _validate_binding(receipt: Receipt, evidence: ObservationEvidence, allow_closed: bool = False) -> None:
    if receipt.state is not ReceiptState.APPLIED:
        raise ObservationMaterializerError("materializer ACK requires an applied receipt")
    if receipt.post_dispatch_observation_boundary_id != evidence.boundary_id or receipt.post_dispatch_observation_generation_floor != evidence.generation_floor:
        raise ObservationMaterializerError("observation boundary does not bind to receipt")
    allowed_fence_states = (FenceState.RECEIPT_TERMINAL, FenceState.OBSERVATION_PENDING, FenceState.CLOSED) if allow_closed else (FenceState.RECEIPT_TERMINAL, FenceState.OBSERVATION_PENDING)
    if (receipt.source_identity_ref != evidence.source_identity_ref or receipt.target_ref != evidence.target_ref or receipt.requested_state != evidence.requested_state or receipt.requested_state != "running" or evidence.observed_state != evidence.requested_state or receipt.canary_run_id != evidence.canary_run_id or receipt.fence_identity != evidence.fence_identity or receipt.fence_counter != evidence.fence_counter or receipt.fence_state not in allowed_fence_states):
        raise ObservationMaterializerError("observation does not bind to receipt lineage")
