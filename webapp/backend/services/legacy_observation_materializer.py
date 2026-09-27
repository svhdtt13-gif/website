"""Compatibility facade for same-store observation persistence."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace

from repositories.legacy_authority_observation import (
    MaterializerAck,
    ObservationEvidence,
    ObservationMaterializerConflict,
    ObservationMaterializerError,
)
from repositories.legacy_authority_store import LegacyAuthorityStore
from repositories.legacy_authority_types import Receipt
from repositories.sqlite import VerifiedTargetObservation


@dataclass(frozen=True, slots=True)
class VerifiedRuntimeSnapshot:
    """Runtime snapshot facts accepted from the verified sole-reader path."""

    snapshot_generation_id: int
    snapshot_id: str
    captured_at: str
    source_hash: str
    target_observation: VerifiedTargetObservation
    materializer_run_id: str

    @property
    def observed_state(self) -> str:
        return self.target_observation.observed_state

__all__ = (
    "MaterializerAck",
    "ObservationEvidence",
    "ObservationMaterializer",
    "ObservationMaterializerConflict",
    "ObservationMaterializerError",
)


class ObservationMaterializer:
    def __init__(
        self,
        store: LegacyAuthorityStore,
    ) -> None:
        self.store = store

    def record_ack(
        self,
        receipt: Receipt,
        evidence: ObservationEvidence,
    ) -> MaterializerAck:
        return self.store._record_observation_ack(receipt, evidence)

    def require_evidence(self, receipt: Receipt) -> MaterializerAck:
        ack = self.store.require_observation_evidence(receipt)
        return ack

    def record_verified_runtime_snapshot(
        self,
        authority_lookup_key: str,
        snapshot: VerifiedRuntimeSnapshot,
    ) -> MaterializerAck:
        """Re-read authority lineage and persist internally constructed evidence."""
        receipt, evidence = self.prepare_verified_runtime_snapshot(
            authority_lookup_key, snapshot
        )
        return self.record_ack(receipt, evidence)

    def prepare_verified_runtime_snapshot(
        self,
        authority_lookup_key: str,
        snapshot: VerifiedRuntimeSnapshot,
    ) -> tuple[Receipt, ObservationEvidence]:
        """Re-read authority lineage and internally construct signed evidence."""
        receipt = self.store.get_receipt(authority_lookup_key)
        if snapshot.target_observation.target_ref != receipt.target_ref:
            raise ObservationMaterializerError("runtime target observation does not match receipt")
        if snapshot.observed_state != receipt.requested_state:
            raise ObservationMaterializerError("runtime target observation does not prove requested state")
        boundary_id = receipt.post_dispatch_observation_boundary_id
        generation_floor = receipt.post_dispatch_observation_generation_floor
        if boundary_id is None or generation_floor is None:
            raise ObservationMaterializerError("durable observation boundary is unavailable")
        unsigned = ObservationEvidence(
            boundary_id=boundary_id,
            generation_floor=generation_floor,
            snapshot_generation_id=snapshot.snapshot_generation_id,
            snapshot_id=snapshot.snapshot_id,
            captured_at=snapshot.captured_at,
            materializer_run_id=snapshot.materializer_run_id or uuid.uuid4().hex,
            source_hash=snapshot.source_hash,
            source_identity_ref=receipt.source_identity_ref,
            canary_run_id=receipt.canary_run_id,
            fence_identity=receipt.fence_identity,
            fence_counter=receipt.fence_counter,
            target_ref=receipt.target_ref,
            requested_state=receipt.requested_state,
            observed_state=snapshot.observed_state,
            attestation_fingerprint="pending",
        )
        evidence = replace(
            unsigned,
            attestation_fingerprint=self.store._attest_observation(unsigned),
        )
        return receipt, evidence
