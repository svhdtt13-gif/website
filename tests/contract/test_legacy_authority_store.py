from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import (
    LegacyAuthorityStore,
    ReceiptConflictError,
    TransitionError,
    legacy_store_path,
)
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    FenceState,
    Handoff,
    ReceiptState,
)


class InjectedFailure(RuntimeError):
    """Test-only failure used to prove transaction rollback on any throwable."""


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


class LegacyAuthorityStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = legacy_store_path(Path(self.temporary.name))
        self.store = LegacyAuthorityStore.create(self.path)
        self.store.add_authorization(valid_handoff(), valid_artifact())

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def acquire_fence(self) -> None:
        self.store.request_fence("send-1")
        self.store.transition_fence("send-1", FenceState.ACQUIRED)

    def test_schema_metadata_identity_and_attach_denial(self) -> None:
        metadata = self.store.schema_metadata()

        self.assertEqual(metadata["store_kind"], "legacy_canary_authority")
        self.assertEqual(metadata["schema_version"], "3")
        self.assertEqual(len(metadata["schema_checksum"]), 64)
        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("ATTACH DATABASE ':memory:' AS forbidden")

    def test_fence_lifecycle_and_abandoned_resolve_clear_history(self) -> None:
        self.store.request_fence("send-1")
        for state in (
            FenceState.ACQUIRED,
            FenceState.RECEIPT_ACCEPTED,
            FenceState.MUTATION,
            FenceState.RECEIPT_TERMINAL,
            FenceState.OBSERVATION_PENDING,
            FenceState.ABANDONED,
        ):
            self.store.transition_fence("send-1", state)

        self.store.resolve_clear("send-1")

        self.assertIsNone(self.store.get_fence("send-1"))
        self.assertEqual(
            self.store.fence_history("send-1"),
            [
                "requested",
                "acquired",
                "receipt_accepted",
                "mutation",
                "receipt_terminal",
                "observation_pending",
                "abandoned",
                "resolve_clear",
            ],
        )

    def test_invalid_fence_transition_fails_closed(self) -> None:
        self.store.request_fence("send-1")

        with self.assertRaises(TransitionError):
            self.store.transition_fence("send-1", FenceState.MUTATION)

    def test_receipt_insert_wins_replay_denies_and_conflict_is_permanent(self) -> None:
        self.acquire_fence()
        winner = self.store.accept_receipt("send-1", "sha256:envelope-1")
        replay = self.store.accept_receipt("send-1", "sha256:envelope-1")

        self.assertTrue(winner.mutation_allowed)
        self.assertFalse(replay.mutation_allowed)
        self.assertEqual(replay.receipt.state, ReceiptState.ACCEPTED)
        with self.assertRaises(ReceiptConflictError):
            self.store.accept_receipt("send-1", "sha256:different")
        self.assertEqual(
            self.store.get_receipt("send-1").envelope_fingerprint,
            "sha256:envelope-1",
        )

    def test_terminal_cas_requires_expected_state_and_applied_observation(self) -> None:
        self.acquire_fence()
        winner = self.store.accept_receipt("send-1", "sha256:envelope-1")
        dispatching = self.store.begin_dispatch(winner)

        applied = self.store.terminal_receipt(dispatching, ReceiptState.APPLIED)
        self.assertEqual(applied.state, ReceiptState.APPLIED)
        self.assertIsNone(applied.post_dispatch_observation_boundary_id)
        self.assertIsNone(applied.post_dispatch_observation_generation_floor)
        applied = self.store.append_observation_boundary(applied, "boundary-42", 42)
        self.assertEqual(applied.post_dispatch_observation_boundary_id, "boundary-42")
        self.assertEqual(applied.post_dispatch_observation_generation_floor, 42)
        self.assertEqual(self.store.append_observation_boundary(applied, "boundary-42", 42), applied)
        with self.assertRaises(ReceiptConflictError):
            self.store.append_observation_boundary(applied, "boundary-43", 43)
        with self.assertRaises(TransitionError):
            self.store.terminal_receipt(dispatching, ReceiptState.UNKNOWN)

    def test_dispatching_cannot_become_not_applied_proven(self) -> None:
        self.acquire_fence()
        dispatching = self.store.begin_dispatch(self.store.accept_receipt("send-1", "sha256:envelope-1"))

        with self.assertRaises(TransitionError):
            self.store.terminal_receipt(dispatching, ReceiptState.NOT_APPLIED_PROVEN)

    def test_receipt_requires_acquired_fence(self) -> None:
        with self.assertRaises(TransitionError):
            self.store.accept_receipt("send-1", "sha256:envelope-1")

    def test_authorization_replay_is_idempotent_and_fingerprint_conflict_is_fatal(self) -> None:
        self.store.add_authorization(valid_handoff(), valid_artifact())
        conflicting = valid_artifact()
        conflicting = AuthorizationArtifact(
            **{
                **asdict(conflicting),
                "authorization_artifact_fingerprint": "sha256:artifact-2",
            }
        )

        with self.assertRaises(ReceiptConflictError):
            self.store.add_authorization(valid_handoff(), conflicting)

    def test_authorization_replay_compares_complete_projection_without_trusting_fingerprints(self) -> None:
        handoff = valid_handoff()
        artifact = valid_artifact()
        before_handoff = tuple(self.store.connection.execute("SELECT * FROM handoffs").fetchone())
        before_artifact = tuple(self.store.connection.execute("SELECT * FROM authorization_artifacts").fetchone())
        cases = (
            (
                handoff,
                replace(artifact, canary_run_id="run-2"),
            ),
            (
                replace(handoff, target_ref="client:two", canonical_envelope_json='{"operation_kind":"group_on","target_ref":"client:two"}'),
                replace(artifact, target_ref="client:two"),
            ),
            (replace(handoff, binding_generation=8), artifact),
            (replace(handoff, verified_identity_ref="identity:two"), artifact),
            (replace(handoff, exporter_attestation="attestation:two"), artifact),
            (handoff, replace(artifact, fence_identity="fence:run-1:12")),
            (handoff, replace(artifact, source_identity_ref="legacy-source:two")),
            (handoff, replace(artifact, artifact_producer_identity="producer:two")),
            (handoff, replace(artifact, pre_operation_observation_generation_floor=42)),
        )
        for changed_handoff, changed_artifact in cases:
            with self.subTest(changed_handoff=changed_handoff, changed_artifact=changed_artifact), self.assertRaises(ReceiptConflictError):
                self.store.add_authorization(changed_handoff, changed_artifact)
            self.assertEqual(tuple(self.store.connection.execute("SELECT * FROM handoffs").fetchone()), before_handoff)
            self.assertEqual(tuple(self.store.connection.execute("SELECT * FROM authorization_artifacts").fetchone()), before_artifact)

    def test_observation_pending_and_close_are_fenced(self) -> None:
        self.acquire_fence()
        winner = self.store.accept_receipt("send-1", "sha256:envelope-1")
        dispatching = self.store.begin_dispatch(winner)
        terminal = self.store.terminal_receipt(dispatching, ReceiptState.UNKNOWN)

        self.store.mark_observation_pending("send-1")
        self.store.close_fence("send-1")

        self.assertEqual(self.store.get_fence("send-1")["state"], FenceState.CLOSED.value)
        self.assertEqual(terminal.state, ReceiptState.UNKNOWN)

    def test_immediate_rolls_back_any_exception_and_releases_lock(self) -> None:
        with self.assertRaises(InjectedFailure), self.store._immediate():
            self.store.connection.execute("INSERT INTO schema_meta(key, value) VALUES ('transient', 'discard')")
            raise InjectedFailure

        self.assertFalse(self.store.connection.in_transaction)
        self.assertNotIn("transient", self.store.schema_metadata())
        self.store.request_fence("send-1")

    def test_fence_identity_is_immutable(self) -> None:
        self.acquire_fence()

        with self.assertRaises(sqlite3.DatabaseError):
            self.store.connection.execute("UPDATE fences SET fence_identity='fence:tampered' WHERE pre_send_identity='send-1'")

if __name__ == "__main__":
    unittest.main()
