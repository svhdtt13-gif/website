#!/usr/bin/env python3
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from tests.security.canary_send_support import CanarySendFixture


class CanaryHandoffBoundaryTests(CanarySendFixture, unittest.TestCase):
    def test_trusted_handoff_uses_logical_target_and_preserves_four_field_body(self):
        armed = self.armed_candidate()
        stub = self.stub()

        result = self.sender.send(
            armed["canary_candidate_id"],
            self.lease,
            "agent-a",
            self.transport(stub),
        )

        body = json.loads(stub.hits[0]["body"])
        self.assertEqual(
            set(body),
            {
                "pre_send_identity",
                "canary_idempotency_key",
                "envelope_fingerprint",
                "contract_version",
            },
        )
        self.assertEqual(result["state"], "succeeded")
        stored = self.legacy_stores[0].lookup_authorization_by_pre_send_identity(
            armed["pre_send_identity"]
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.handoff.target_ref, "profile-a")
        self.assertNotEqual(stored.handoff.target_ref, stub.url)

    def test_authorization_runs_with_closed_p4_transaction(self):
        armed = self.armed_candidate()
        stub = self.stub()
        states = []
        authorize = self.sender.legacy_coordinator.authorize

        def inspect_transaction(handoff):
            states.append(self.operational.connection.in_transaction)
            return authorize(handoff)

        with patch.object(
            self.sender.legacy_coordinator,
            "authorize",
            side_effect=inspect_transaction,
        ):
            self.sender.send(
                armed["canary_candidate_id"],
                self.lease,
                "agent-a",
                self.transport(stub),
            )

        self.assertEqual(states, [False])
        self.assertFalse(self.operational.connection.in_transaction)
        self.assertEqual(stub.hit_count(), 1)

    def test_exact_send_replay_allocates_no_second_context_or_artifact(self):
        armed = self.armed_candidate()
        stub = self.stub()
        self.sender.send(
            armed["canary_candidate_id"],
            self.lease,
            "agent-a",
            self.transport(stub),
        )

        replay_transport = self.transport(stub)
        replay = self.sender.send(
            armed["canary_candidate_id"],
            self.lease,
            "agent-a",
            replay_transport,
        )

        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(self.context_provider.allocations, 1)
        self.assertEqual(self.legacy_artifact_count(), 1)
        self.assertEqual(replay_transport.transmissions, 0)

    def test_hmac_keys_are_absent_from_both_sqlite_files(self):
        armed = self.armed_candidate()
        stub = self.stub()

        self.sender.send(
            armed["canary_candidate_id"],
            self.lease,
            "agent-a",
            self.transport(stub),
        )

        operational_bytes = self.operational.path.read_bytes()
        legacy_bytes = self.legacy_stores[0].path.read_bytes()
        for _identity, key in self.handoff_keys.keys:
            self.assertNotIn(key, operational_bytes)
            self.assertNotIn(key, legacy_bytes)


if __name__ == "__main__":
    unittest.main()
