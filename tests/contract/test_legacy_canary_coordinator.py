from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import LegacyAuthorityStore, legacy_store_path
from repositories.legacy_authority_types import AuthorizationLookup
from services.canary_envelope import CanaryEnvelope
from services.canary_handoff_export import CanaryHandoffExporter
from services.legacy_canary_coordinator import (
    DeterministicTestAuthorizationContextProvider,
    LegacyAuthorizationContext,
    LegacyCanaryCoordinator,
    LegacyCoordinatorTrust,
    ProductionAuthorizationContextProvider,
)
from services.legacy_handoff_trust import (
    HandoffTrustError,
    TrustedAckVerifier,
    TrustedHandoffVerifier,
)


@dataclass(frozen=True, slots=True)
class CoordinatorTestKeyProvider:
    keys: tuple[tuple[str, bytes], ...]

    def key_for(self, identity: str) -> bytes:
        for candidate, key in self.keys:
            if candidate == identity:
                return key
        raise KeyError(identity)


def valid_envelope() -> CanaryEnvelope:
    return CanaryEnvelope(
        shadow_evaluation_id="shadow-1",
        intent_id="intent-1",
        execution_id="execution-1",
        attempt_id="attempt-1",
        target_id="target-1",
        job_id="job-1",
        operation_kind="group_on",
        destination_ref="client:one",
        host_id="host-1",
        profile_id="profile-1",
        binding_generation=7,
        verified_identity_ref="identity:one",
        verified_identity_revision=3,
        authority_epoch="epoch-1",
        fence_counter=11,
        owner_id="owner-1",
        lease_id="lease-1",
        contract_version="is3b1.v1",
        canary_idempotency_key="idem-1",
        pre_send_identity="send-1",
    )


def valid_context() -> LegacyAuthorizationContext:
    return LegacyAuthorizationContext(
        canary_run_id="run-1",
        fence_identity="fence:run-1:11",
        source_identity_ref="legacy-source:one",
        artifact_producer_identity="coordinator:legacy",
        pre_operation_observation_generation_floor=41,
        authority_epoch="epoch-1",
        fence_counter=11,
    )


class LegacyCanaryCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = LegacyAuthorityStore.create(
            legacy_store_path(Path(self.temporary.name))
        )
        self.keys = CoordinatorTestKeyProvider(
            (
                ("exporter:canary", b"test-exporter-key"),
                ("coordinator:legacy", b"test-coordinator-key"),
            )
        )
        self.exporter = CanaryHandoffExporter("exporter:canary", self.keys)
        self.handoff_verifier = TrustedHandoffVerifier("exporter:canary", self.keys)
        self.ack_verifier = TrustedAckVerifier("coordinator:legacy", self.keys)

    def tearDown(self) -> None:
        self.store.close()
        self.temporary.cleanup()

    def coordinator(self, context_provider) -> LegacyCanaryCoordinator:
        return LegacyCanaryCoordinator(
            self.store,
            context_provider,
            LegacyCoordinatorTrust(
                self.handoff_verifier,
                "coordinator:legacy",
                self.keys,
            ),
        )

    def test_authorize_persists_bound_artifact_and_authenticated_ack(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        coordinator = self.coordinator(
            DeterministicTestAuthorizationContextProvider(valid_context())
        )

        ack = coordinator.authorize(handoff)

        stored = self.store.lookup_authorization_by_pre_send_identity("send-1")
        self.assertIsNotNone(stored)
        stored.artifact.require_handoff(stored.handoff)
        self.assertEqual(ack.handoff_id, handoff.handoff_id)
        self.assertEqual(
            ack.authorization_artifact_fingerprint,
            stored.artifact.authorization_artifact_fingerprint,
        )
        self.ack_verifier.verify(ack)

    def test_exact_durable_replay_recovers_ack_without_context_provider(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        first = self.coordinator(
            DeterministicTestAuthorizationContextProvider(valid_context())
        ).authorize(handoff)

        recovered = self.coordinator(ProductionAuthorizationContextProvider()).authorize(
            handoff
        )

        self.assertEqual(recovered, first)
        self.ack_verifier.verify(recovered)
        self.assertEqual(
            self.coordinator(ProductionAuthorizationContextProvider()).recover_ack(
                AuthorizationLookup("send-1")
            ),
            first,
        )

    def test_ack_tampering_is_rejected(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        ack = self.coordinator(
            DeterministicTestAuthorizationContextProvider(valid_context())
        ).authorize(handoff)

        with self.assertRaises(HandoffTrustError):
            self.ack_verifier.verify(
                replace(ack, authorization_artifact_fingerprint="sha256:changed")
            )

    def test_context_binding_mismatch_fails_without_partial_rows(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        mismatched = replace(valid_context(), fence_counter=12)

        with self.assertRaises(HandoffTrustError):
            self.coordinator(
                DeterministicTestAuthorizationContextProvider(mismatched)
            ).authorize(handoff)

        self.assertIsNone(
            self.store.lookup_authorization_by_pre_send_identity("send-1")
        )

    def test_same_identity_forged_handoff_is_rejected_before_lookup(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        coordinator = self.coordinator(
            DeterministicTestAuthorizationContextProvider(valid_context())
        )
        coordinator.authorize(handoff)

        with self.assertRaises(HandoffTrustError):
            coordinator.authorize(replace(handoff, exporter_attestation="changed"))

    def test_production_context_provider_fails_closed(self) -> None:
        handoff = self.exporter.export(valid_envelope())

        with self.assertRaises(HandoffTrustError):
            self.coordinator(ProductionAuthorizationContextProvider()).authorize(handoff)


if __name__ == "__main__":
    unittest.main()
