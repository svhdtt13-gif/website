from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_types import Receipt, ReceiptState
from services.legacy_observation_materializer import (
    ObservationEvidence,
    ObservationMaterializer,
    ObservationMaterializerConflict,
    ObservationMaterializerError,
)


def applied_receipt() -> Receipt:
    return Receipt(
        pre_send_identity="send-1",
        envelope_fingerprint="sha256:envelope-1",
        canary_run_id="run-1",
        fence_identity="fence:run-1:11",
        authority_epoch="epoch-1",
        fence_counter=11,
        state=ReceiptState.APPLIED,
        pre_operation_observation_generation_floor=41,
        post_dispatch_observation_boundary_id="boundary-42",
        post_dispatch_observation_generation_floor=42,
        source_identity_ref="legacy-source:one",
        target_ref="client:one",
        requested_state="running",
    )


def evidence(**changes: object) -> ObservationEvidence:
    values: dict[str, object] = {
        "boundary_id": "boundary-42",
        "generation_floor": 42,
        "generation_id": 43,
        "snapshot_id": "snapshot-43",
        "captured_at": "2026-09-22T00:00:00+00:00",
        "materializer_run_id": "materializer-run-1",
        "source_hash": "sha256:source-1",
        "source_identity_ref": "legacy-source:one",
        "canary_run_id": "run-1",
        "fence_identity": "fence:run-1:11",
        "fence_counter": 11,
        "target_ref": "client:one",
        "requested_state": "running",
        "observed_state": "running",
    }
    values.update(changes)
    return ObservationEvidence(**values)


class LegacyObservationMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(Path(self.temporary.name) / "ack.sqlite3")
        self.connection.row_factory = sqlite3.Row
        self.materializer = ObservationMaterializer(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def test_ack_is_durable_idempotent_and_evidence_is_strict(self) -> None:
        receipt = applied_receipt()
        ack = self.materializer.record_ack(receipt, evidence())
        self.assertEqual(self.materializer.record_ack(receipt, evidence()), ack)
        self.assertEqual(
            self.materializer.require_evidence(receipt),
            ack,
        )

    def test_conflicting_replay_and_provenance_mismatch_fail_closed(self) -> None:
        receipt = applied_receipt()
        self.materializer.record_ack(receipt, evidence())
        with self.assertRaises(ObservationMaterializerConflict):
            self.materializer.record_ack(receipt, evidence(generation_id=44))
        with self.assertRaises(ObservationMaterializerError):
            mismatched = Receipt(
                receipt.pre_send_identity,
                receipt.envelope_fingerprint,
                receipt.canary_run_id,
                receipt.fence_identity,
                receipt.authority_epoch,
                receipt.fence_counter,
                receipt.state,
                receipt.pre_operation_observation_generation_floor,
                receipt.post_dispatch_observation_boundary_id,
                receipt.post_dispatch_observation_generation_floor,
                receipt.source_identity_ref,
                "client:two",
                receipt.requested_state,
            )
            self.materializer.require_evidence(mismatched)

    def test_generation_must_be_strictly_later_than_floor(self) -> None:
        with self.assertRaises(ObservationMaterializerError):
            evidence(generation_id=42)

    def test_non_applied_receipt_cannot_ack(self) -> None:
        receipt = applied_receipt()
        not_applied = Receipt(
            receipt.pre_send_identity,
            receipt.envelope_fingerprint,
            receipt.canary_run_id,
            receipt.fence_identity,
            receipt.authority_epoch,
            receipt.fence_counter,
            ReceiptState.UNKNOWN,
            receipt.pre_operation_observation_generation_floor,
        )
        with self.assertRaises(ObservationMaterializerError):
            self.materializer.record_ack(not_applied, evidence())

    def test_ack_is_append_only_across_restart(self) -> None:
        receipt = applied_receipt()
        self.materializer.record_ack(receipt, evidence())
        self.connection.close()
        reopened = sqlite3.connect(Path(self.temporary.name) / "ack.sqlite3")
        reopened.row_factory = sqlite3.Row
        materializer = ObservationMaterializer(reopened)
        self.assertEqual(
            materializer.require_evidence(receipt).evidence.generation_id,
            43,
        )
        with self.assertRaises(sqlite3.DatabaseError):
            reopened.execute(
                "DELETE FROM observation_materializer_acks WHERE receipt_identity='send-1'"
            )
        reopened.close()
        self.connection = sqlite3.connect(Path(self.temporary.name) / "ack.sqlite3")
        self.connection.row_factory = sqlite3.Row

    def test_default_sqlite_row_factory_is_supported(self) -> None:
        self.connection.close()
        connection = sqlite3.connect(Path(self.temporary.name) / "plain.sqlite3")
        materializer = ObservationMaterializer(connection)
        receipt = applied_receipt()
        materializer.record_ack(receipt, evidence())
        self.assertEqual(materializer.require_evidence(receipt).evidence.snapshot_id, "snapshot-43")
        connection.close()

    def test_authority_store_connection_is_rejected(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        with self.assertRaises(ObservationMaterializerError):
            ObservationMaterializer(connection)
        connection.close()


if __name__ == "__main__":
    unittest.main()
