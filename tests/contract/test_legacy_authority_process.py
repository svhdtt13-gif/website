from __future__ import annotations

import multiprocessing
import sys
import tempfile
import unittest
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
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
        canary_run_id="run-1",
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


def _race_accept_worker(path: str, barrier: Barrier, queue: Queue) -> None:
    store = LegacyAuthorityStore.open(Path(path))
    barrier.wait()
    decision = store.accept_receipt("send-1", "sha256:envelope-1")
    queue.put(decision.mutation_allowed)
    store.close()


class LegacyAuthorityProcessTests(unittest.TestCase):
    def test_two_processes_have_exactly_one_receipt_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = legacy_store_path(Path(temporary))
            store = LegacyAuthorityStore.create(path)
            store.add_authorization(valid_handoff(), valid_artifact())
            store.request_fence("send-1")
            store.transition_fence("send-1", FenceState.ACQUIRED)
            store.close()

            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(2)
            queue = context.Queue()
            processes = [context.Process(target=_race_accept_worker, args=(str(path), barrier, queue)) for _ in range(2)]
            for process in processes:
                process.start()
            results = [queue.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(timeout=20)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)

            self.assertEqual(sorted(results), [False, True])
            winner_store = LegacyAuthorityStore.open(path)
            self.assertEqual(winner_store.get_receipt("send-1").state, ReceiptState.ACCEPTED)
            winner_store.close()


if __name__ == "__main__":
    unittest.main()
