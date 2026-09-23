"""Compatibility facade for same-store observation persistence."""
from __future__ import annotations

from repositories.legacy_authority_observation import (
    MaterializerAck,
    ObservationEvidence,
    ObservationMaterializerConflict,
    ObservationMaterializerError,
)
from repositories.legacy_authority_store import LegacyAuthorityStore
from repositories.legacy_authority_types import Receipt

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
