from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import LegacyAuthorityStore, legacy_store_path
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    FenceState,
    Handoff,
    ReceiptState,
)


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


def second_authorization() -> tuple[Handoff, AuthorizationArtifact]:
    return (
        replace(
            valid_handoff(),
            handoff_id="handoff-2",
            pre_send_identity="send-2",
            canary_idempotency_key="idem-2",
            envelope_fingerprint="sha256:envelope-2",
            authority_epoch="epoch-2",
            fence_counter=12,
            exporter_attestation="attestation:two",
        ),
        replace(
            valid_artifact(),
            handoff_id="handoff-2",
            pre_send_identity="send-2",
            canary_idempotency_key="idem-2",
            canary_run_id="run-1",
            fence_identity="fence:run-2:12",
            authority_epoch="epoch-2",
            fence_counter=12,
            envelope_fingerprint="sha256:envelope-2",
            authorization_artifact_fingerprint="sha256:artifact-2",
        ),
    )


class LegacyAuthorityOwnershipTests(unittest.TestCase):
    def test_handoffs_schema_excludes_run_identity_and_artifacts_retain_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))

            handoff_columns = {
                row[1] for row in store.connection.execute("PRAGMA table_info(handoffs)")
            }
            artifact_columns = {
                row[1] for row in store.connection.execute("PRAGMA table_info(authorization_artifacts)")
            }

            self.assertNotIn("canary_run_id", handoff_columns)
            self.assertIn("canary_run_id", artifact_columns)
            store.close()

    def test_duplicate_canary_run_id_rejects_without_partial_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))
            store.add_authorization(valid_handoff(), valid_artifact())
            second_handoff, second_artifact = second_authorization()

            with self.assertRaises(sqlite3.IntegrityError):
                store.add_authorization(second_handoff, second_artifact)
            self.assertIsNone(store.connection.execute("SELECT 1 FROM handoffs WHERE handoff_id='handoff-2'").fetchone())
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM authorization_artifacts").fetchone()[0], 1)
            store.close()

    def test_canary_run_id_reuse_after_abandoned_resolve_clear_rejects(self) -> None:
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
            second_handoff, second_artifact = second_authorization()

            with self.assertRaises(sqlite3.IntegrityError):
                store.add_authorization(second_handoff, second_artifact)
            self.assertIsNone(store.connection.execute("SELECT 1 FROM handoffs WHERE handoff_id='handoff-2'").fetchone())
            store.close()


if __name__ == "__main__":
    unittest.main()
