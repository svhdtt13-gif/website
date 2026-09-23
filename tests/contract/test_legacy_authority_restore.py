from __future__ import annotations

import shutil
import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from typing import assert_never

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import (
    LegacyAuthorityStore,
    LegacyAuthorityStoreFactory,
    StoreQuarantinedError,
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
)
from services.legacy_observation_trust import observation_attestation


class TestKeyProvider:
    def key_for(self, _identity: str) -> bytes:
        return b"test-observation-key"


def valid_handoff() -> Handoff:
    return Handoff(
        handoff_id="handoff-1",
        source_envelope_contract_version="is3b1.v1",
        transport_contract_version="is3b2.v1",
        pre_send_identity="send-1",
        canary_idempotency_key="idem-1",
        envelope_fingerprint="sha256:envelope-1",
        canonical_envelope_json='{"operation_kind":"group_on","target_ref":"client:one"}',
        operation_kind="group_on",
        target_ref="client:one",
        binding_generation=7,
        verified_identity_ref="identity:one",
        verified_identity_revision=3,
        authority_epoch="epoch-1",
        fence_counter=11,
        exporter_identity="exporter:one",
        exporter_attestation="attestation:one",
    )


def closeable_observation(store: LegacyAuthorityStore, receipt: Receipt) -> None:
    unsigned = ObservationEvidence(
        "boundary-42", 42, 43, "snapshot-43", "2026-09-22T00:00:00+00:00",
        "materializer-run-1", "sha256:source-1", receipt.source_identity_ref,
        receipt.canary_run_id, receipt.fence_identity, receipt.fence_counter,
        receipt.target_ref, receipt.requested_state, receipt.requested_state,
        "pending",
    )
    signed = replace(unsigned, attestation_fingerprint=observation_attestation(unsigned, "materializer:one", TestKeyProvider()))
    ObservationMaterializer(store).record_ack(
        receipt,
        signed,
    )


def valid_artifact() -> AuthorizationArtifact:
    return AuthorizationArtifact(
        handoff_id="handoff-1",
        pre_send_identity="send-1",
        canary_idempotency_key="idem-1",
        canary_run_id="run-1",
        fence_identity="fence:run-1:11",
        authority_epoch="epoch-1",
        fence_counter=11,
        source_identity_ref="legacy-source:one",
        artifact_producer_identity="producer:one",
        source_envelope_contract_version="is3b1.v1",
        transport_contract_version="is3b2.v1",
        contract_version="is3b2.v1",
        envelope_exporter_identity="exporter:one",
        operation_kind="group_on",
        target_ref="client:one",
        requested_state="running",
        pre_operation_observation_generation_floor=41,
        envelope_fingerprint="sha256:envelope-1",
        authorization_artifact_fingerprint="sha256:artifact-1",
    )


class LegacyAuthorityRestoreTests(unittest.TestCase):
    def _applied_receipt(self, store: LegacyAuthorityStore) -> Receipt:
        store.request_fence("send-1")
        store.transition_fence("send-1", FenceState.ACQUIRED)
        accepted = store.accept_receipt("send-1", "sha256:envelope-1")
        dispatching = store.begin_dispatch(accepted)
        return store.terminal_receipt(dispatching, ReceiptState.APPLIED)

    def test_fence_identity_cannot_be_reused_after_resolve_clear(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))
            store.add_authorization(valid_handoff(), valid_artifact())
            store.request_fence("send-1")
            store.transition_fence("send-1", FenceState.ACQUIRED)
            accepted = store.accept_receipt("send-1", "sha256:envelope-1")
            store.terminal_receipt(accepted.receipt, ReceiptState.UNKNOWN)
            store.mark_observation_pending("send-1")
            store.transition_fence("send-1", FenceState.ABANDONED)
            store.resolve_clear("send-1")

            second_handoff = replace(
                valid_handoff(),
                handoff_id="handoff-2",
                pre_send_identity="send-2",
                canary_idempotency_key="idem-2",
                envelope_fingerprint="sha256:envelope-2",
                canonical_envelope_json='{"operation_kind":"group_on","target_ref":"client:one"}',
                authority_epoch="epoch-2",
                fence_counter=12,
                exporter_attestation="attestation:two",
            )
            second_artifact = replace(
                valid_artifact(),
                handoff_id="handoff-2",
                pre_send_identity="send-2",
                canary_idempotency_key="idem-2",
                canary_run_id="run-2",
                authority_epoch="epoch-2",
                fence_counter=12,
                envelope_fingerprint="sha256:envelope-2",
                authorization_artifact_fingerprint="sha256:artifact-2",
            )

            with self.assertRaises(sqlite3.IntegrityError):
                store.add_authorization(second_handoff, second_artifact)
            self.assertIsNone(store.connection.execute("SELECT 1 FROM handoffs WHERE handoff_id='handoff-2'").fetchone())
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM authorization_artifacts").fetchone()[0], 1)
            store.close()

    def test_applied_receipt_requires_observation_boundary_before_close(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))
            store.add_authorization(valid_handoff(), valid_artifact())
            applied = self._applied_receipt(store)
            store.mark_observation_pending("send-1")

            with self.assertRaises(TransitionError):
                store.close_fence("send-1")
            self.assertEqual(store.get_fence("send-1")["state"], FenceState.OBSERVATION_PENDING.value)

            store.append_observation_boundary(applied, "boundary-42")
            with self.assertRaises(TransitionError):
                store.transition_fence("send-1", FenceState.CLOSED)
            with self.assertRaises(TransitionError):
                store.close_fence("send-1")
            store.close()

    def test_closed_applied_receipt_rejects_late_observation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStoreFactory("materializer:one", TestKeyProvider()).create(
                legacy_store_path(Path(temporary))
            )
            store.add_authorization(valid_handoff(), valid_artifact())
            applied = self._applied_receipt(store)
            bounded = store.append_observation_boundary(applied, "boundary-42")
            closeable_observation(store, bounded)
            store.mark_observation_pending("send-1")
            store.close_fence("send-1")

            with self.assertRaises(TransitionError):
                store.append_observation_boundary(applied, "boundary-43")
            store.close()

    def test_abandoned_applied_receipt_rejects_late_observation_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))
            store.add_authorization(valid_handoff(), valid_artifact())
            applied = self._applied_receipt(store)
            store.mark_observation_pending("send-1")
            store.transition_fence("send-1", FenceState.ABANDONED)

            with self.assertRaises(TransitionError):
                store.append_observation_boundary(applied, "boundary-43")
            receipt = store.get_receipt("send-1")
            self.assertIsNone(receipt.post_dispatch_observation_boundary_id)
            self.assertIsNone(receipt.post_dispatch_observation_generation_floor)
            store.close()

    def test_reopen_never_restores_mutation_permission(self) -> None:
        for terminal_state in (None, ReceiptState.APPLIED, ReceiptState.NOT_APPLIED_PROVEN, ReceiptState.UNKNOWN):
            with self.subTest(terminal_state=terminal_state), tempfile.TemporaryDirectory() as temporary:
                path = legacy_store_path(Path(temporary))
                store = LegacyAuthorityStore.create(path)
                store.add_authorization(valid_handoff(), valid_artifact())
                store.request_fence("send-1")
                store.transition_fence("send-1", FenceState.ACQUIRED)
                decision = store.accept_receipt("send-1", "sha256:envelope-1")
                match terminal_state:
                    case None:
                        pass
                    case ReceiptState.APPLIED | ReceiptState.UNKNOWN:
                        decision_receipt = store.begin_dispatch(decision)
                        store.terminal_receipt(decision_receipt, terminal_state)
                    case ReceiptState.NOT_APPLIED_PROVEN:
                        store.terminal_receipt(decision.receipt, terminal_state)
                    case unreachable:
                        assert_never(unreachable)
                store.close()

                reopened = LegacyAuthorityStore.open(path)
                replay = reopened.accept_receipt("send-1", "sha256:envelope-1")
                self.assertFalse(replay.mutation_allowed)
                expected_state = ReceiptState.ACCEPTED if terminal_state is None else terminal_state
                self.assertEqual(replay.receipt.state, expected_state)
                reopened.close()

    def test_restored_store_is_persistently_quarantined_and_read_only_for_authority(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source_path = legacy_store_path(Path(temporary) / "source")
            source = LegacyAuthorityStore.create(source_path)
            source.add_authorization(valid_handoff(), valid_artifact())
            source.request_fence("send-1")
            source.transition_fence("send-1", FenceState.ACQUIRED)
            source.close()

            restored_path = legacy_store_path(Path(temporary) / "restored")
            restored_path.parent.mkdir(parents=True)
            shutil.copy2(source_path, restored_path)
            restored = LegacyAuthorityStore.open_restored(restored_path, "restore:one")

            self.assertEqual(restored.schema_metadata()["restore_state"], "quarantined")
            with self.assertRaises(StoreQuarantinedError):
                restored.accept_receipt("send-1", "sha256:envelope-1")
            with self.assertRaises(StoreQuarantinedError):
                restored.transition_fence("send-1", FenceState.RECEIPT_ACCEPTED)
            restored.close()

            reopened = LegacyAuthorityStore.open(restored_path)
            with self.assertRaises(StoreQuarantinedError):
                reopened.request_fence("send-1")
            reopened.close()
            with self.assertRaises(StoreQuarantinedError):
                LegacyAuthorityStore.open_restored(restored_path, "restore:two")


if __name__ == "__main__":
    unittest.main()
