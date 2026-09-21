#!/usr/bin/env python3
import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store_types import ReceiptConflictError
from services.binding_authority import AuthorityRejected
from services.canary_envelope import CanaryEnvelope
from services.canary_handoff_export import CanaryHandoffExporter
from services.legacy_handoff_trust import (
    HandoffTrustError,
    authorization_artifact_fingerprint,
    sign_authorization_ack,
)

from tests.security.canary_handoff_support import HandoffHmacKeyProvider
from tests.security.canary_send_support import (
    TEST_COORDINATOR_IDENTITY,
    TEST_EXPORTER_IDENTITY,
    CanarySendFixture,
)


class CanaryHandoffFaultTests(CanarySendFixture, unittest.TestCase):
    def test_missing_ack_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)

        with patch.object(
            self.sender.legacy_coordinator, "authorize", return_value=None
        ), self.assertRaises(HandoffTrustError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_forged_ack_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        authorize = self.sender.legacy_coordinator.authorize

        def forged(handoff):
            return replace(
                authorize(handoff), coordinator_attestation="hmac-sha256:forged"
            )

        with patch.object(
            self.sender.legacy_coordinator, "authorize", side_effect=forged
        ), self.assertRaises(HandoffTrustError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_authenticated_ack_for_wrong_handoff_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        authorize = self.sender.legacy_coordinator.authorize

        def wrong_binding(handoff):
            authorize(handoff)
            stored = self.legacy_stores[0].lookup_authorization_by_pre_send_identity(
                handoff.pre_send_identity
            )
            altered = replace(
                stored.artifact,
                handoff_id="handoff-v1-wrong-binding",
                authorization_artifact_fingerprint="pending",
            )
            altered = replace(
                altered,
                authorization_artifact_fingerprint=(
                    authorization_artifact_fingerprint(altered)
                ),
            )
            return sign_authorization_ack(
                altered, TEST_COORDINATOR_IDENTITY, self.handoff_keys
            )

        with patch.object(
            self.sender.legacy_coordinator, "authorize", side_effect=wrong_binding
        ), self.assertRaises(HandoffTrustError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_wrong_exporter_identity_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        keys = HandoffHmacKeyProvider(
            (("exporter:wrong", b"wrong-exporter-identity-key"),)
        )
        self.sender.handoff_exporter = CanaryHandoffExporter(
            "exporter:wrong", keys
        )

        with self.assertRaises(HandoffTrustError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_wrong_exporter_key_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        keys = HandoffHmacKeyProvider(
            ((TEST_EXPORTER_IDENTITY, b"wrong-exporter-key"),)
        )
        self.sender.handoff_exporter = CanaryHandoffExporter(
            TEST_EXPORTER_IDENTITY, keys
        )

        with self.assertRaises(HandoffTrustError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_tampered_envelope_json_and_fingerprint_send_zero(self):
        armed = self.armed_candidate()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_canary_candidates "
                "SET envelope_json='{}', envelope_fingerprint='changed' "
                "WHERE canary_candidate_id=?",
                (armed["canary_candidate_id"],),
            )
        stub = self.stub()
        transport = self.transport(stub)

        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_tampered_operation_and_target_send_zero(self):
        armed = self.armed_candidate()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_canary_candidates "
                "SET operation_kind='group_off', destination_ref='profile-b' "
                "WHERE canary_candidate_id=?",
                (armed["canary_candidate_id"],),
            )
        stub = self.stub()
        transport = self.transport(stub)

        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_tampered_binding_epoch_and_fence_send_zero(self):
        armed = self.armed_candidate()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_canary_candidates SET "
                "binding_generation=99, verified_identity_ref='identity:wrong', "
                "verified_identity_revision=99, authority_epoch='epoch-wrong', "
                "fence_counter=99 WHERE canary_candidate_id=?",
                (armed["canary_candidate_id"],),
            )
        stub = self.stub()
        transport = self.transport(stub)

        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_conflicting_durable_replay_sends_zero(self):
        armed = self.armed_candidate()
        envelope = CanaryEnvelope(**json.loads(armed["envelope_json"]))
        conflicting = replace(envelope, destination_ref="profile-conflict")
        self.sender.legacy_coordinator.authorize(
            self.handoff_exporter.export(conflicting)
        )
        stub = self.stub()
        transport = self.transport(stub)

        with self.assertRaises(ReceiptConflictError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(self.legacy_artifact_count(), 1)
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_ack_lost_after_durable_commit_never_posts_or_reauthorizes(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        authorize = self.sender.legacy_coordinator.authorize

        def commit_then_lose(handoff):
            authorize(handoff)
            raise HandoffTrustError("authorization ACK lost")

        with patch.object(
            self.sender.legacy_coordinator,
            "authorize",
            side_effect=commit_then_lose,
        ), self.assertRaises(HandoffTrustError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(self.legacy_artifact_count(), 1)
        self.assertEqual(self.context_provider.allocations, 1)
        replay_transport = self.transport(stub)
        replay = self.sender.send(
            armed["canary_candidate_id"],
            self.lease,
            "agent-a",
            replay_transport,
        )
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(self.context_provider.allocations, 1)
        self.assertEqual(replay_transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_crash_after_ack_before_post_never_blind_retries(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)

        with patch(
            "services.canary_send._send_body",
            side_effect=RuntimeError("simulated crash after ACK"),
        ), self.assertRaises(RuntimeError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(self.legacy_artifact_count(), 1)
        self.assertEqual(self.context_provider.allocations, 1)
        replay_transport = self.transport(stub)
        replay = self.sender.send(
            armed["canary_candidate_id"],
            self.lease,
            "agent-a",
            replay_transport,
        )
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(self.context_provider.allocations, 1)
        self.assertEqual(replay_transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)


if __name__ == "__main__":
    unittest.main()
