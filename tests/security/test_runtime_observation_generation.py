from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_types import (
    FenceState,
    Receipt,
    ReceiptState,
)
from services.runtime_observation_generation import (
    DurableObservationBoundaryProvider,
    ObservationGenerationContractError,
    TrustedObservationBoundary,
    next_observation_generation,
)


def applied_receipt() -> Receipt:
    return Receipt(
        "send-1", "sha256:envelope", "run-1", "fence-1", "epoch-1", 1,
        ReceiptState.APPLIED, 41, "boundary-42", 42, "source-1", "target-1",
        "running", FenceState.OBSERVATION_PENDING,
    )


class ReceiptStore:
    def current_observation_receipt(self) -> Receipt:
        return applied_receipt()


class RuntimeObservationGenerationTests(unittest.TestCase):
    def test_generation_uses_shared_causal_floor(self) -> None:
        boundary = TrustedObservationBoundary(
            "boundary-42", "send-1", 42, "run-1", "fence-1", "source-1", "target-1",
            "epoch-1", 1, "running",
        )
        self.assertEqual(next_observation_generation(None, None), 1)
        self.assertEqual(next_observation_generation(40, None), 41)
        self.assertEqual(next_observation_generation(41, boundary), 43)

    def test_boundary_provider_loads_durable_receipt_lineage(self) -> None:
        boundary = DurableObservationBoundaryProvider(ReceiptStore()).current_boundary()
        self.assertEqual(boundary.boundary_id, "boundary-42")
        self.assertEqual(boundary.generation_floor, 42)
        self.assertEqual(boundary.target_ref, "target-1")

    def test_boundary_without_applied_receipt_fails_closed(self) -> None:
        receipt = Receipt(
            "send-1", "sha256:envelope", "run-1", "fence-1", "epoch-1", 1,
            ReceiptState.ACCEPTED, 41, None, None, "source-1", "target-1",
            "running", FenceState.RECEIPT_ACCEPTED,
        )

        with self.assertRaises(ObservationGenerationContractError):
            TrustedObservationBoundary.from_receipt(receipt)


if __name__ == "__main__":
    unittest.main()
