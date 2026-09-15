#!/usr/bin/env python3
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from services.binding_authority import AuthorityRejected
from services.shadow_dispatch_envelope import ShadowEnvelope, build_shadow_envelope
from services.shadow_dispatch_transport import (
    NullShadowTransport,
    RecordingShadowTransport,
)
from shadow_dispatch_support import ShadowDispatchFixture


class ShadowDispatchLifecycleTests(ShadowDispatchFixture, unittest.TestCase):
    def test_prepared_intent_evaluates_deterministic_sanitized_envelope(self):
        execution, intent = self.prepared()
        result = self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request-1", NullShadowTransport()
        )

        self.assertEqual(result["status"], "evaluated")
        envelope = json.loads(result["envelope_json"])
        self.assertEqual(
            set(envelope),
            {"execution_id", "idempotency_key", "job_id", "operation_kind", "target_ref"},
        )
        self.assertEqual(envelope["execution_id"], execution["execution_id"])
        self.assertNotIn("payload", result["envelope_json"])
        self.assertEqual(
            result["envelope_fingerprint"],
            ShadowEnvelope(**envelope).fingerprint,
        )
        for field in (
            "owner_id", "lease_id", "authority_epoch", "fence_counter",
            "verified_identity_ref", "request_idempotency_key",
        ):
            self.assertNotIn(field, result)
        self.assertEqual(self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 0)
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM authority_bound_attempts")[0][0],
            1,
        )

    def test_recording_transport_matches_and_replays_without_second_record(self):
        _execution, intent = self.prepared()
        transport = RecordingShadowTransport()
        first = self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request-1", transport
        )
        replay = self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request-1", transport
        )

        self.assertEqual(first["status"], "matched")
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(first["shadow_evaluation_id"], replay["shadow_evaluation_id"])
        self.assertEqual(len(transport.records), 1)

    def test_recording_mismatch_is_durable_and_blocks_source_intent(self):
        _execution, intent = self.prepared()
        result = self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request-1",
            RecordingShadowTransport("wrong-fingerprint"),
        )

        self.assertEqual(result["status"], "mismatched")
        self.assertEqual(len(result["transport_fingerprint"]), 64)
        self.assertNotEqual(result["transport_fingerprint"], "wrong-fingerprint")
        self.assertEqual(
            self.operational.rows(
                "SELECT status, blocked_reason FROM authority_bound_dispatch_intents "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0:2],
            ("blocked", "transport_fingerprint_mismatch"),
        )

    def test_non_prepared_states_are_never_actionable(self):
        for source_status in ("blocked", "unknown", "reconciled", "quarantined"):
            with self.subTest(source_status=source_status):
                execution, intent = self.prepared("key-" + source_status)
                with self.operational.transaction():
                    self.operational.connection.execute(
                        "UPDATE authority_bound_dispatch_intents SET status=? WHERE intent_id=?",
                        (source_status, intent["intent_id"]),
                    )
                transport = RecordingShadowTransport()
                result = self.shadow.evaluate(
                    intent["intent_id"], self.lease, "agent-a", "request-" + source_status,
                    transport,
                )
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["reason_class"], "intent_not_prepared")
                self.assertEqual(transport.records, [])
                self.assertEqual(result["execution_id"], execution["execution_id"])

    def test_conflicting_request_and_transport_context_rejects(self):
        _execution, intent = self.prepared()
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request-1", NullShadowTransport()
        )
        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "shadow-request-2", NullShadowTransport()
            )
        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "shadow-request-1", RecordingShadowTransport()
            )

    def test_wrong_owner_cannot_replay_or_quarantine_existing_evaluation(self):
        _execution, intent = self.prepared()
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request-1", NullShadowTransport()
        )

        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-b", "shadow-request-1",
                NullShadowTransport(),
            )
        self.assertEqual(self.shadow_row(intent["intent_id"])["outcome"], "evaluated")

    def test_request_key_is_sensitive_safe_and_global_conflicts_reject_before_transport(self):
        _execution, intent = self.prepared()
        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "access-token",
                NullShadowTransport(),
            )

        _other_execution, other_intent = self.prepared("other-target")
        first_transport = RecordingShadowTransport()
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "global-key", first_transport
        )
        second_transport = RecordingShadowTransport()
        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                other_intent["intent_id"], self.lease, "agent-a", "global-key",
                second_transport,
            )
        self.assertEqual(second_transport.records, [])

    def test_envelope_builder_is_order_independent_and_sensitive_values_reject(self):
        _execution, intent = self.prepared()
        target = self.operational.rows(
            "SELECT * FROM authority_bound_targets WHERE target_id=?",
            (intent["target_id"],),
        )[0]
        execution = self.operational.rows(
            "SELECT * FROM authority_bound_executions WHERE execution_id=?",
            (intent["execution_id"],),
        )[0]
        first = build_shadow_envelope(execution, target, "corr-test")
        second = ShadowEnvelope(
            first.execution_id,
            first.idempotency_key,
            first.job_id,
            first.operation_kind,
            first.target_ref,
        )
        self.assertEqual(first.canonical_json, second.canonical_json)
        for sensitive_ref in (
            "secret",
            "access-token",
            "session",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature",
            "raw-payload",
        ):
            with self.subTest(sensitive_ref=sensitive_ref), self.assertRaises(
                AuthorityRejected
            ):
                build_shadow_envelope(
                    execution,
                    dict(target, target_ref=sensitive_ref),
                    "corr-test",
                )


if __name__ == "__main__":
    unittest.main()
