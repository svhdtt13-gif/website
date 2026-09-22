from __future__ import annotations

import hashlib
import json
import sys
import unittest
from dataclasses import dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from services.canary_envelope import CanaryEnvelope
from services.canary_handoff_export import CanaryHandoffExporter
from services.legacy_handoff_trust import (
    HandoffTrustError,
    TrustedHandoffVerifier,
    attest_handoff,
    deterministic_handoff_id,
)


@dataclass(frozen=True, slots=True)
class HandoffTestKeyProvider:
    identity: str
    key: bytes

    def key_for(self, identity: str) -> bytes:
        if identity != self.identity:
            raise KeyError(identity)
        return self.key


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


class CanaryHandoffTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.keys = HandoffTestKeyProvider("exporter:canary", b"test-exporter-key")
        self.exporter = CanaryHandoffExporter("exporter:canary", self.keys)
        self.verifier = TrustedHandoffVerifier("exporter:canary", self.keys)

    def test_export_is_exact_authenticated_sixteen_field_projection(self) -> None:
        envelope = valid_envelope()

        handoff = self.exporter.export(envelope)

        self.assertEqual(len(handoff.projection()), 16)
        self.assertEqual(handoff.canonical_envelope_json, envelope.canonical_json)
        self.assertEqual(handoff.envelope_fingerprint, envelope.fingerprint)
        self.assertEqual(handoff.target_ref, envelope.destination_ref)
        self.assertEqual(handoff.handoff_id, deterministic_handoff_id("send-1"))
        self.assertNotIn("run", handoff.handoff_id)
        self.assertEqual(self.verifier.verify(handoff), envelope)

    def test_handoff_identity_is_deterministic_and_version_domain_separated(self) -> None:
        first = self.exporter.export(valid_envelope())
        second = self.exporter.export(valid_envelope())

        self.assertEqual(first, second)
        self.assertNotEqual(first.handoff_id, first.pre_send_identity)

    def test_verifier_rejects_wrong_identity_attestation_and_projection(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        cases = (
            replace(handoff, exporter_identity="exporter:other"),
            replace(handoff, exporter_attestation="hmac-sha256:wrong"),
            replace(handoff, binding_generation=8),
            replace(handoff, handoff_id="handoff-wrong"),
        )

        for changed in cases:
            with self.subTest(changed=changed), self.assertRaises(HandoffTrustError):
                self.verifier.verify(changed)

    def test_verifier_rejects_non_exact_twenty_key_envelope(self) -> None:
        handoff = self.exporter.export(valid_envelope())
        widened_payload = json.loads(handoff.canonical_envelope_json)
        widened_payload["canary_run_id"] = "forbidden"
        widened = json.dumps(
            widened_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        )
        changed = replace(
            handoff,
            canonical_envelope_json=widened,
            envelope_fingerprint=hashlib.sha256(widened.encode("utf-8")).hexdigest(),
            exporter_attestation="pending",
        )
        changed = attest_handoff(changed, self.keys)

        with self.assertRaises(HandoffTrustError):
            self.verifier.verify(changed)


if __name__ == "__main__":
    unittest.main()
