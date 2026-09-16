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

    def _evidence(self, candidate, ref="evidence-1", result="succeeded"):
        return {
            "evidence_ref": ref,
            "result": result,
            "source": "read_only",
            "canary_idempotency_key": candidate["canary_idempotency_key"],
            "pre_send_identity": candidate["pre_send_identity"],
            "envelope_fingerprint": candidate["envelope_fingerprint"],
        }

    def test_unknown_is_terminal_for_arming_and_never_retries_transport(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        first = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            first["canary_candidate_id"], self.lease, "agent-a",
            "evidence-unknown-1", "ambiguous_boundary",
            first["pre_send_identity"],
        )
        self.assertEqual(unknown["state"], "unknown")

        replay = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )

        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(len(transport.observations), 1)

    def test_record_ambiguous_armed_to_unknown(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        attempts_before = self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_attempts"
        )[0][0]
        jobs_before = self.operational.rows(
            "SELECT COUNT(*) FROM jobs"
        )[0][0]

        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-1", "ambiguous_boundary",
            armed["pre_send_identity"],
        )

        self.assertEqual(unknown["state"], "unknown")
        self.assertEqual(unknown["outcome"], "ambiguous_recorded")
        self.assertEqual(unknown["ambiguity_evidence_ref"], "evidence-amb-1")
        self.assertIsNotNone(unknown["unknown_at"])
        self.assertEqual(len(transport.observations), 1)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_attempts"
            )[0][0],
            attempts_before,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0],
            jobs_before,
        )

    def test_record_ambiguous_replay_is_idempotent(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        first = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-2", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        second = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-2", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        self.assertEqual(second["state"], "unknown")
        self.assertEqual(second["outcome"], "idempotent_replay")
        self.assertEqual(first["unknown_at"], second["unknown_at"])
        self.assertEqual(len(transport.observations), 1)

    def test_record_ambiguous_conflicting_evidence_rejects(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-3", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        with self.assertRaises(AuthorityRejected):
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-other", "ambiguous_boundary",
                armed["pre_send_identity"],
            )
        row = self.operational.rows(
            "SELECT state, ambiguity_evidence_ref FROM "
            "authority_bound_canary_candidates WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0]
        self.assertEqual(tuple(row), ("unknown", "evidence-amb-3"))

    def test_record_ambiguous_rejects_non_armed_states(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-4", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        reconciled = self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self._evidence(unknown),
        )
        self.assertEqual(reconciled["state"], "reconciled")
        with self.assertRaises(AuthorityRejected):
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-amb-5", "ambiguous_boundary",
                armed["pre_send_identity"],
            )

    def test_reconcile_unknown_to_reconciled(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-6", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        attempts_before = self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_attempts"
        )[0][0]
        jobs_before = self.operational.rows(
            "SELECT COUNT(*) FROM jobs"
        )[0][0]

        reconciled = self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self._evidence(unknown),
        )

        self.assertEqual(reconciled["state"], "reconciled")
        self.assertEqual(reconciled["outcome"], "reconciled")
        self.assertEqual(reconciled["reconciliation_evidence_ref"], "evidence-1")
        self.assertEqual(reconciled["reconciliation_result"], "succeeded")
        self.assertIsNotNone(reconciled["reconciled_at"])
        self.assertEqual(len(transport.observations), 1)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_attempts"
            )[0][0],
            attempts_before,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0],
            jobs_before,
        )

    def test_reconcile_replay_is_idempotent(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-7", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        evidence = self._evidence(unknown)
        first = self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a", evidence
        )
        second = self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a", evidence
        )
        self.assertEqual(second["state"], "reconciled")
        self.assertEqual(second["outcome"], "idempotent_replay")
        self.assertEqual(first["reconciled_at"], second["reconciled_at"])
        self.assertEqual(len(transport.observations), 1)

    def test_reconcile_conflicting_or_untrusted_evidence_rejects(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-8", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        good = self._evidence(unknown)
        tampered = dict(good, pre_send_identity="pre-send-wrong-identity")
        with self.assertRaises(AuthorityRejected):
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", tampered
            )
        untrusted = dict(good, source="remote_write", evidence_ref="evidence-x")
        with self.assertRaises(AuthorityRejected):
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", untrusted
            )
        bad_result = dict(good, result="maybe", evidence_ref="evidence-y")
        with self.assertRaises(AuthorityRejected):
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", bad_result
            )
        row = self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0][0]
        self.assertEqual(row, "unknown")
        self.assertEqual(len(transport.observations), 1)
        self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a", good
        )
        flipped = dict(good, result="failed")
        with self.assertRaises(AuthorityRejected):
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", flipped
            )
        row = self.operational.rows(
            "SELECT state, reconciliation_result FROM "
            "authority_bound_canary_candidates WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0]
        self.assertEqual(tuple(row), ("reconciled", "succeeded"))

    def test_reconciled_never_returns_to_armed(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-9", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self._evidence(unknown),
        )
        replay = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(replay["state"], "reconciled")
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(len(transport.observations), 1)

    def test_sensitive_evidence_is_rejected(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        with self.assertRaises(AuthorityRejected):
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "bearer-token-abc", "ambiguous_boundary",
                armed["pre_send_identity"],
            )
        row = self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0][0]
        self.assertEqual(row, "armed")

    def test_reconciliation_preserves_upstream_and_counts(self):
        shadow = self._matched_shadow()
        transport = RecordingCanaryTransport()
        intent_before = dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_dispatch_intents WHERE intent_id=?",
                (shadow["intent_id"],),
            ).fetchone()
        )
        shadow_before = dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_shadow_evaluations "
                "WHERE shadow_evaluation_id=?",
                (shadow["shadow_evaluation_id"],),
            ).fetchone()
        )
        attempts_before = self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_attempts"
        )[0][0]
        jobs_before = self.operational.rows(
            "SELECT COUNT(*) FROM jobs"
        )[0][0]
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-amb-10", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self._evidence(unknown),
        )
        intent_after = dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_dispatch_intents WHERE intent_id=?",
                (shadow["intent_id"],),
            ).fetchone()
        )
        shadow_after = dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_shadow_evaluations "
                "WHERE shadow_evaluation_id=?",
                (shadow["shadow_evaluation_id"],),
            ).fetchone()
        )
        self.assertEqual(intent_after, intent_before)
        self.assertEqual(shadow_after, shadow_before)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_attempts"
            )[0][0],
            attempts_before,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0],
            jobs_before,
        )

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
