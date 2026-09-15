#!/usr/bin/env python3
import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from services.binding_authority import AuthorityRejected
from services.canary_arming import CanaryArmingService
from services.canary_envelope import CanaryEnvelope
from services.canary_transport import (
    ContractProbeTransport,
    NullCanaryTransport,
    RecordingCanaryTransport,
)
from services.shadow_dispatch_transport import RecordingShadowTransport

from tests.security.shadow_dispatch_support import ShadowDispatchFixture


class CanaryArmingContractTests(ShadowDispatchFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.canary = CanaryArmingService(
            self.portable_path, self.operational, self.coordinator
        )

    def _matched_shadow(self, key="target-key"):
        _execution, intent = self.prepared(key)
        return self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-" + key,
            RecordingShadowTransport(),
        )

    def test_matched_shadow_arms_deterministic_sanitized_envelope(self):
        shadow = self._matched_shadow()

        result = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            NullCanaryTransport(),
        )

        envelope = json.loads(result["envelope_json"])
        self.assertEqual(result["state"], "armed")
        self.assertEqual(
            set(envelope),
            {
                "attempt_id", "authority_epoch", "binding_generation",
                "canary_idempotency_key", "contract_version", "destination_ref",
                "execution_id", "fence_counter", "host_id", "intent_id",
                "job_id", "lease_id", "operation_kind", "owner_id",
                "pre_send_identity", "profile_id", "shadow_evaluation_id",
                "target_id", "verified_identity_ref",
                "verified_identity_revision",
            },
        )
        self.assertEqual(
            result["envelope_fingerprint"],
            hashlib.sha256(result["envelope_json"].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(CanaryEnvelope(**envelope).canonical_json, result["envelope_json"])
        lowered = result["envelope_json"].lower()
        for forbidden in ("cookie", "token", "secret", "payload", "password"):
            self.assertNotIn(forbidden, lowered)

    def test_envelope_identities_are_deterministic_and_distinct(self):
        shadow = self._matched_shadow()
        first = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            NullCanaryTransport(),
        )
        second = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            NullCanaryTransport(),
        )

        self.assertEqual(first["canary_idempotency_key"], second["canary_idempotency_key"])
        self.assertEqual(first["pre_send_identity"], second["pre_send_identity"])
        self.assertNotEqual(first["canary_idempotency_key"], first["pre_send_identity"])
        self.assertEqual(second["outcome"], "idempotent_replay")

    def test_recording_and_probe_transports_are_inert_and_one_shot(self):
        for transport in (RecordingCanaryTransport(), ContractProbeTransport()):
            with self.subTest(kind=transport.kind):
                shadow = self._matched_shadow("target-" + transport.kind)
                first = self.canary.arm(
                    shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
                )
                replay = self.canary.arm(
                    shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
                )
                self.assertEqual(len(transport.observations), 1)
                self.assertEqual(first["state"], "armed")
                self.assertEqual(replay["outcome"], "idempotent_replay")

    def test_unknown_is_terminal_for_arming_and_never_retries_transport(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        first = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_canary_candidates SET state='unknown' "
                "WHERE canary_candidate_id=?",
                (first["canary_candidate_id"],),
            )

        replay = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )

        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(len(transport.observations), 1)

    def test_transport_kind_or_contract_mismatch_fails_closed(self):
        shadow = self._matched_shadow()
        self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            NullCanaryTransport(),
        )

        for transport in (
            RecordingCanaryTransport(),
            NullCanaryTransport(contract_version="is3b1.v2"),
        ):
            with self.subTest(kind=transport.kind), self.assertRaises(AuthorityRejected):
                self.canary.arm(
                    shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
                )
            if hasattr(transport, "observations"):
                self.assertEqual(transport.observations, [])

    def test_contract_mismatch_on_first_arm_has_no_side_effect_or_row(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport(contract_version="is3b1.v2")

        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "canary_transport_contract_mismatch",
        )
        self.assertEqual(transport.observations, [])
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)


if __name__ == "__main__":
    unittest.main()
