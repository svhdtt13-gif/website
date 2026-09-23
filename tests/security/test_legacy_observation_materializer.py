from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import (
    LegacyAuthorityStore,
    LegacyAuthorityStoreError,
    LegacyAuthorityStoreFactory,
    ReceiptConflictError,
    TransitionError,
    legacy_store_path,
)
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    FenceState,
    Handoff,
    Receipt,
    ReceiptState,
)
from services.legacy_observation_materializer import (
    ObservationEvidence,
    ObservationMaterializer,
    ObservationMaterializerConflict,
    ObservationMaterializerError,
)
from services.legacy_observation_trust import observation_attestation


class TestKeyProvider:
    def key_for(self, _identity: str) -> bytes:
        return b"test-observation-key"


def handoff() -> Handoff:
    return Handoff("handoff-1", "is3b1.v1", "is3b2.v1", "send-1", "idem-1", "sha256:envelope-1", '{"operation_kind":"group_on","target_ref":"client:one"}', "group_on", "client:one", 7, "identity:one", 3, "epoch-1", 11, "exporter:one", "attestation:one")


def artifact() -> AuthorizationArtifact:
    return AuthorizationArtifact("handoff-1", "send-1", "idem-1", "run-1", "fence:run-1:11", "epoch-1", 11, "legacy-source:one", "producer:one", "is3b1.v1", "is3b2.v1", "is3b2.v1", "exporter:one", "group_on", "client:one", "running", 41, "sha256:envelope-1", "sha256:artifact-1")


def evidence(**changes: str | int) -> ObservationEvidence:
    values: dict[str, str | int] = {
        "boundary_id": "boundary-42", "generation_floor": 42, "snapshot_generation_id": 43,
        "snapshot_id": "snapshot-43", "captured_at": "2026-09-22T00:00:00+00:00",
        "materializer_run_id": "materializer-run-1", "source_hash": "sha256:source-1",
        "source_identity_ref": "legacy-source:one", "canary_run_id": "run-1",
        "fence_identity": "fence:run-1:11", "fence_counter": 11,
        "target_ref": "client:one", "requested_state": "running",
        "observed_state": "running", "attestation_fingerprint": "sha256:attestation-1",
    }
    values.update(changes)
    unsigned = ObservationEvidence(**values)
    return replace(
        unsigned,
        attestation_fingerprint=observation_attestation(
            unsigned, "materializer:one", TestKeyProvider()
        ),
    )


class LegacyObservationMaterializerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = legacy_store_path(Path(self.temporary.name))
        self.factory = LegacyAuthorityStoreFactory("materializer:one", TestKeyProvider())
        self.store = self.factory.create(self.path)
        self.store.add_authorization(handoff(), artifact())
        self.store.request_fence("send-1")
        self.store.transition_fence("send-1", FenceState.ACQUIRED)
        accepted = self.store.accept_receipt("send-1", "sha256:envelope-1")
        dispatching = self.store.begin_dispatch(accepted)
        terminal = self.store.terminal_receipt(dispatching, ReceiptState.APPLIED)
        self.receipt = self.store.append_observation_boundary(terminal, "boundary-42")
        self.materializer = ObservationMaterializer(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def test_ack_is_durable_idempotent_and_evidence_is_strict(self) -> None:
        ack = self.materializer.record_ack(self.receipt, evidence())
        self.assertEqual(self.materializer.record_ack(self.receipt, evidence()), ack)
        self.assertEqual(self.materializer.require_evidence(self.receipt), ack)
        self.store.close_fence("send-1")
        self.assertEqual(self.store.get_fence("send-1")["state"], FenceState.CLOSED.value)

    def test_boundary_rejects_forged_pre_operation_floor(self) -> None:
        forged = replace(self.receipt, pre_operation_observation_generation_floor=0)
        with self.assertRaises(ReceiptConflictError):
            self.store.append_observation_boundary(forged, "boundary-43")

    def test_boundary_rejects_forged_authority_epoch(self) -> None:
        forged = replace(self.receipt, authority_epoch="epoch-forged")
        with self.assertRaises(ReceiptConflictError):
            self.store.append_observation_boundary(forged, "boundary-43")

    def test_ack_rejects_forged_pre_operation_floor(self) -> None:
        forged = replace(self.receipt, pre_operation_observation_generation_floor=0)
        with self.assertRaises(ObservationMaterializerError):
            self.materializer.record_ack(forged, evidence())

    def test_closure_rejects_tampered_applied_state(self) -> None:
        self.materializer.record_ack(self.receipt, evidence())
        self.store.connection.execute("DROP TRIGGER receipt_lineage_immutable")
        self.store.connection.execute("DROP TRIGGER receipt_boundary_immutable_update")
        self.store.connection.execute(
            "UPDATE receipts SET state=? WHERE pre_send_identity=?",
            (ReceiptState.NOT_APPLIED_PROVEN.value, self.receipt.pre_send_identity),
        )
        with self.assertRaises(TransitionError):
            self.store.close_fence("send-1")

    def test_closure_rejects_tampered_generation_head(self) -> None:
        self.materializer.record_ack(self.receipt, evidence())
        self.store.connection.execute(
            "UPDATE observation_generation_sequence SET last_generation_id=0,last_record_digest='' WHERE key='global'"
        )
        with self.assertRaises(ObservationMaterializerError):
            self.store.close_fence("send-1")

    def test_conflicting_replay_and_provenance_mismatch_fail_closed(self) -> None:
        self.materializer.record_ack(self.receipt, evidence())
        with self.assertRaises(ObservationMaterializerConflict):
            self.materializer.record_ack(self.receipt, evidence(snapshot_generation_id=44))
        mismatched = Receipt(self.receipt.pre_send_identity, self.receipt.envelope_fingerprint, self.receipt.canary_run_id, self.receipt.fence_identity, self.receipt.authority_epoch, self.receipt.fence_counter, self.receipt.state, self.receipt.pre_operation_observation_generation_floor, self.receipt.post_dispatch_observation_boundary_id, self.receipt.post_dispatch_observation_generation_floor, self.receipt.source_identity_ref, "client:two", self.receipt.requested_state)
        with self.assertRaises(ObservationMaterializerError):
            self.materializer.require_evidence(mismatched)

    def test_generation_is_monotonic_and_survives_restart(self) -> None:
        self.materializer.record_ack(self.receipt, evidence())
        self.store.close()
        self.store = self.factory.open(self.path)
        self.materializer = ObservationMaterializer(self.store)
        second_receipt = replace(self.receipt, pre_send_identity="send-2")
        with self.assertRaises(ObservationMaterializerError):
            self.materializer.record_ack(second_receipt, evidence(snapshot_generation_id=43))

    def test_authority_generation_is_store_owned_not_snapshot_selected(self) -> None:
        self.materializer.record_ack(self.receipt, evidence(snapshot_generation_id=999))
        authority_generation_id = self.store.connection.execute(
            "SELECT generation_id FROM observation_generation_ledger"
        ).fetchone()[0]
        stored_snapshot_generation_id = self.store.connection.execute(
            "SELECT snapshot_generation_id FROM observation_materializer_acks"
        ).fetchone()[0]
        self.assertEqual(authority_generation_id, 43)
        self.assertEqual(stored_snapshot_generation_id, 999)

    def test_stale_snapshot_generation_is_rejected(self) -> None:
        for snapshot_generation_id in (41, 42):
            with self.subTest(snapshot_generation_id=snapshot_generation_id), self.assertRaises(ObservationMaterializerError):
                evidence(snapshot_generation_id=snapshot_generation_id)

    def test_ack_is_append_only_and_authority_is_same_store(self) -> None:
        self.materializer.record_ack(self.receipt, evidence())
        self.assertIsNotNone(self.store.connection.execute("SELECT 1 FROM observation_boundaries").fetchone())
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("DELETE FROM observation_materializer_acks WHERE receipt_identity='send-1'")

    def test_tampered_generation_head_fails_closed(self) -> None:
        self.materializer.record_ack(self.receipt, evidence())
        self.store.connection.execute(
            "UPDATE observation_generation_sequence SET last_record_digest='tampered' WHERE key='global'"
        )
        with self.assertRaises(ObservationMaterializerError):
            self.materializer.require_evidence(self.receipt)

    def test_missing_trusted_detached_provenance_fails_closed(self) -> None:
        untrusted = LegacyAuthorityStore.create(legacy_store_path(Path(self.temporary.name) / "untrusted"))
        with self.assertRaises(LegacyAuthorityStoreError):
            untrusted._verify_observation_attestation(evidence())
        untrusted.close()

    def test_untrusted_receipt_or_fence_cannot_ack(self) -> None:
        not_applied = Receipt("send-2", "sha256:envelope-2", "run-2", "fence:run-2:12", "epoch-2", 12, ReceiptState.UNKNOWN, 10)
        with self.assertRaises(ObservationMaterializerError):
            self.materializer.record_ack(not_applied, evidence())


if __name__ == "__main__":
    unittest.main()
