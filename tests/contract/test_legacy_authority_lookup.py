from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import LegacyAuthorityStore, legacy_store_path
from repositories.legacy_authority_store_types import (
    ReceiptConflictError,
    StoreQuarantinedError,
)
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    AuthorizationLookup,
    Handoff,
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
        artifact_producer_identity="coordinator:one",
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


class LegacyAuthorityLookupTests(unittest.TestCase):
    def test_lookup_reconstructs_complete_typed_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))
            handoff = valid_handoff()
            artifact = valid_artifact()
            store.add_authorization(handoff, artifact)

            stored = store.lookup_authorization(AuthorizationLookup("send-1"))

            self.assertIsNotNone(stored)
            self.assertEqual(stored.handoff, handoff)
            self.assertEqual(stored.artifact, artifact)
            store.close()

    def test_lookup_rejects_quarantined_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = legacy_store_path(Path(temporary))
            store = LegacyAuthorityStore.create(path)
            store.add_authorization(valid_handoff(), valid_artifact())
            store.close()
            restored = LegacyAuthorityStore.open_restored(path, "restore:one")

            with self.assertRaises(StoreQuarantinedError):
                restored.lookup_authorization(AuthorizationLookup("send-1"))
            restored.close()

    def test_envelope_fingerprint_conflict_is_unique_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = LegacyAuthorityStore.create(legacy_store_path(Path(temporary)))
            store.add_authorization(valid_handoff(), valid_artifact())
            fingerprint_indexes = {
                tuple(
                    row[2]
                    for row in store.connection.execute(
                        f"PRAGMA index_info('{index[1]}')"
                    )
                )
                for index in store.connection.execute("PRAGMA index_list('handoffs')")
                if index[2] == 1
            }
            second_handoff = replace(
                valid_handoff(),
                handoff_id="handoff-2",
                pre_send_identity="send-2",
                canary_idempotency_key="idem-2",
                authority_epoch="epoch-2",
                fence_counter=12,
            )
            second_artifact = replace(
                valid_artifact(),
                handoff_id="handoff-2",
                pre_send_identity="send-2",
                canary_idempotency_key="idem-2",
                canary_run_id="run-2",
                fence_identity="fence:run-2:12",
                authority_epoch="epoch-2",
                fence_counter=12,
                authorization_artifact_fingerprint="sha256:artifact-2",
            )

            with self.assertRaises(ReceiptConflictError):
                store.add_authorization(second_handoff, second_artifact)

            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM authorization_artifacts").fetchone()[0], 1)
            self.assertIn(("envelope_fingerprint",), fingerprint_indexes)
            store.close()


if __name__ == "__main__":
    unittest.main()
