#!/usr/bin/env python3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from unittest.mock import patch

from repositories.operational_sqlite import (
    OperationalSQLiteRepository,
    backup_directory,
)
from repositories.portable_store import PortableDomainStore
from services.binding_authority import (
    AuthorityConfig,
    AuthorityRejected,
    BindingAuthorityCoordinator,
)
from services.canary_arming import CanaryArmingService
from services.canary_transport import RecordingCanaryTransport
from services.shadow_dispatch_transport import (
    NullShadowTransport,
    RecordingShadowTransport,
)

from tests.security.shadow_dispatch_support import ShadowDispatchFixture


class CanaryArmingAuthorityTests(ShadowDispatchFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.canary = CanaryArmingService(
            self.portable_path, self.operational, self.coordinator
        )

    def _shadow(self, matched=True):
        _execution, intent = self.prepared()
        transport = RecordingShadowTransport() if matched else NullShadowTransport()
        return self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-request", transport
        )

    def test_only_matched_shadow_is_eligible(self):
        shadow = self._shadow(matched=False)
        transport = RecordingCanaryTransport()

        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(rejected.exception.evidence.reason_class, "canary_shadow_not_matched")
        self.assertEqual(transport.observations, [])
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)

    def test_authority_drift_rejects_before_side_effect(self):
        shadow = self._shadow()
        portable = PortableDomainStore.open(self.portable_path)
        try:
            with portable.transaction():
                portable.connection.execute(
                    "UPDATE host_profile_bindings SET state='RETIRED' "
                    "WHERE binding_id='binding-a'"
                )
        finally:
            portable.close()
        transport = RecordingCanaryTransport()

        with self.assertRaises(AuthorityRejected):
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.observations, [])
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)

    def test_stale_lease_rejects_before_side_effect(self):
        shadow = self._shadow()
        self.clock.current += timedelta(seconds=31)
        transport = RecordingCanaryTransport()

        with self.assertRaises(AuthorityRejected):
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.observations, [])
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)

    def test_lineage_conflict_rejects_before_side_effect_and_row(self):
        shadow = self._shadow()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_shadow_evaluations SET owner_id='agent-b' "
                "WHERE shadow_evaluation_id=?",
                (shadow["shadow_evaluation_id"],),
            )
        transport = RecordingCanaryTransport()

        with self.assertRaises(AuthorityRejected):
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.observations, [])
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)

    def test_authority_rejection_precedes_replay_transport_conflict(self):
        shadow = self._shadow()
        self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            RecordingCanaryTransport(),
        )
        self.clock.current += timedelta(seconds=31)
        transport = RecordingCanaryTransport(contract_version="is3b1.v2")

        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(transport.observations, [])

    def test_ten_independent_writers_create_one_row_and_one_observation(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()

        def arm_once(_index):
            operational = OperationalSQLiteRepository.open(self.runtime)
            try:
                coordinator = BindingAuthorityCoordinator(
                    self.portable_path, operational, self.clock,
                    AuthorityConfig(lease_ttl=timedelta(seconds=30)),
                )
                return CanaryArmingService(
                    self.portable_path, operational, coordinator
                ).arm(
                    shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
                )
            finally:
                operational.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(arm_once, range(10)))

        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 1)
        self.assertEqual(len(transport.observations), 1)
        self.assertEqual(
            {result["outcome"] for result in results},
            {"armed", "idempotent_replay"},
        )

    def _retire_binding(self):
        portable = PortableDomainStore.open(self.portable_path)
        try:
            with portable.transaction():
                portable.connection.execute(
                    "UPDATE host_profile_bindings SET state='RETIRED' "
                    "WHERE binding_id='binding-a'"
                )
        finally:
            portable.close()

    def _upstream_snapshot(self, shadow):
        intent = dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_dispatch_intents WHERE intent_id=?",
                (shadow["intent_id"],),
            ).fetchone()
        )
        stored = dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_shadow_evaluations "
                "WHERE shadow_evaluation_id=?",
                (shadow["shadow_evaluation_id"],),
            ).fetchone()
        )
        return intent, stored

    def test_late_guard_drift_before_pre_observe_rejects_without_observation(self):
        shadow = self._shadow()
        intent_before, stored_before = self._upstream_snapshot(shadow)
        transport = RecordingCanaryTransport()
        original = self.canary.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self._retire_binding()
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.canary.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected):
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(transport.observations, [])
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)
        intent_after, stored_after = self._upstream_snapshot(shadow)
        self.assertEqual(intent_after, intent_before)
        self.assertEqual(stored_after, stored_before)

    def test_late_guard_drift_before_persist_rejects_without_row(self):
        shadow = self._shadow()
        intent_before, stored_before = self._upstream_snapshot(shadow)
        transport = RecordingCanaryTransport()
        original = self.canary.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 3:
                self._retire_binding()
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.canary.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected):
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertGreaterEqual(len(calls), 3)
        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)
        intent_after, stored_after = self._upstream_snapshot(shadow)
        self.assertEqual(intent_after, intent_before)
        self.assertEqual(stored_after, stored_before)

    def test_late_guard_lease_expiry_rejects_without_row(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        original = self.canary.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self.clock.current += timedelta(seconds=31)
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.canary.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected):
            self.canary.arm(
                shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_canary_candidates"
        )[0][0], 0)

    def _armed_candidate(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        return shadow, armed

    def _unknown_candidate(self, ref="evidence-order-1"):
        shadow, armed = self._armed_candidate()
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            ref, "ambiguous_boundary", armed["pre_send_identity"],
        )
        return shadow, armed, unknown

    def _candidate_row(self, candidate_id):
        return dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_canary_candidates "
                "WHERE canary_candidate_id=?",
                (candidate_id,),
            ).fetchone()
        )

    def test_unknown_replay_after_lease_expiry_rejects_authority_first(self):
        _shadow, armed, _unknown = self._unknown_candidate()
        self.clock.current += timedelta(seconds=31)
        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-order-1", "ambiguous_boundary",
                armed["pre_send_identity"],
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(
            self._candidate_row(armed["canary_candidate_id"])["state"], "unknown"
        )

    def test_unknown_replay_after_portable_drift_rejects_authority_first(self):
        _shadow, armed, _unknown = self._unknown_candidate()
        self._retire_binding()
        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-order-1", "ambiguous_boundary",
                armed["pre_send_identity"],
            )
        self.assertNotEqual(
            rejected.exception.evidence.reason_class, "canary_ambiguity_conflict"
        )
        self.assertEqual(
            self._candidate_row(armed["canary_candidate_id"])["state"], "unknown"
        )

    def test_stale_authority_beats_ambiguity_conflict(self):
        _shadow, armed, _unknown = self._unknown_candidate()
        self.clock.current += timedelta(seconds=31)
        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-conflicting", "ambiguous_boundary",
                armed["pre_send_identity"],
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )

    def test_reconciled_replay_after_stale_authority_rejects(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-order-2", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        evidence = {
            "evidence_ref": "evidence-order-3",
            "result": "succeeded",
            "source": "read_only",
            "canary_idempotency_key": unknown["canary_idempotency_key"],
            "pre_send_identity": unknown["pre_send_identity"],
            "envelope_fingerprint": unknown["envelope_fingerprint"],
        }
        self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a", evidence
        )
        self.clock.current += timedelta(seconds=31)
        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", evidence
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(
            self._candidate_row(armed["canary_candidate_id"])["state"], "reconciled"
        )

    def test_stale_authority_beats_reconciliation_conflict(self):
        _shadow, armed, unknown = self._unknown_candidate(ref="evidence-order-4")
        self.clock.current += timedelta(seconds=31)
        conflicting = {
            "evidence_ref": "evidence-other",
            "result": "failed",
            "source": "read_only",
            "canary_idempotency_key": unknown["canary_idempotency_key"],
            "pre_send_identity": unknown["pre_send_identity"],
            "envelope_fingerprint": unknown["envelope_fingerprint"],
        }
        with self.assertRaises(AuthorityRejected) as rejected:
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", conflicting
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(
            self._candidate_row(armed["canary_candidate_id"])["state"], "unknown"
        )

    def test_drift_before_unknown_update_leaves_no_mutation(self):
        shadow = self._shadow()
        intent_before, stored_before = self._upstream_snapshot(shadow)
        attempts_before = self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_attempts"
        )[0][0]
        jobs_before = self.operational.rows(
            "SELECT COUNT(*) FROM jobs"
        )[0][0]
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        row_before = self._candidate_row(armed["canary_candidate_id"])
        original = self.canary.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self._retire_binding()
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.canary.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected):
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-late-1", "ambiguous_boundary",
                armed["pre_send_identity"],
            )

        self.assertGreaterEqual(len(calls), 2)
        row_after = self._candidate_row(armed["canary_candidate_id"])
        self.assertEqual(row_after["state"], "armed")
        self.assertEqual(row_after["ambiguity_evidence_ref"], None)
        self.assertEqual(row_after["unknown_at"], None)
        self.assertEqual(row_after, row_before)
        intent_after, stored_after = self._upstream_snapshot(shadow)
        self.assertEqual(intent_after, intent_before)
        self.assertEqual(stored_after, stored_before)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_attempts"
            )[0][0],
            attempts_before,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], jobs_before
        )

    def test_drift_before_reconcile_update_leaves_no_mutation(self):
        shadow = self._shadow()
        intent_before, stored_before = self._upstream_snapshot(shadow)
        attempts_before = self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_attempts"
        )[0][0]
        jobs_before = self.operational.rows(
            "SELECT COUNT(*) FROM jobs"
        )[0][0]
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-late-2", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        evidence = {
            "evidence_ref": "evidence-late-3",
            "result": "succeeded",
            "source": "read_only",
            "canary_idempotency_key": unknown["canary_idempotency_key"],
            "pre_send_identity": unknown["pre_send_identity"],
            "envelope_fingerprint": unknown["envelope_fingerprint"],
        }
        row_before = self._candidate_row(armed["canary_candidate_id"])
        original = self.canary.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self._retire_binding()
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.canary.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected):
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", evidence
            )

        self.assertGreaterEqual(len(calls), 2)
        row_after = self._candidate_row(armed["canary_candidate_id"])
        self.assertEqual(row_after["state"], "unknown")
        self.assertEqual(row_after["reconciliation_evidence_ref"], None)
        self.assertEqual(row_after["reconciled_at"], None)
        self.assertEqual(row_after, row_before)
        intent_after, stored_after = self._upstream_snapshot(shadow)
        self.assertEqual(intent_after, intent_before)
        self.assertEqual(stored_after, stored_before)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_attempts"
            )[0][0],
            attempts_before,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], jobs_before
        )

    def test_stale_authority_before_unknown_transition_rejects(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        self.clock.current += timedelta(seconds=31)
        with self.assertRaises(AuthorityRejected):
            self.canary.record_ambiguous(
                armed["canary_candidate_id"], self.lease, "agent-a",
                "evidence-stale-1", "ambiguous_boundary",
                armed["pre_send_identity"],
            )
        row = self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0][0]
        self.assertEqual(row, "armed")

    def test_stale_authority_before_reconciliation_rejects(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-stale-2", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        evidence = {
            "evidence_ref": "evidence-stale-3",
            "result": "succeeded",
            "source": "read_only",
            "canary_idempotency_key": unknown["canary_idempotency_key"],
            "pre_send_identity": unknown["pre_send_identity"],
            "envelope_fingerprint": unknown["envelope_fingerprint"],
        }
        self.clock.current += timedelta(seconds=31)
        with self.assertRaises(AuthorityRejected):
            self.canary.reconcile_unknown(
                armed["canary_candidate_id"], self.lease, "agent-a", evidence
            )
        row = self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0][0]
        self.assertEqual(row, "unknown")

    def test_restart_and_restore_never_revive_unknown(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-revive-1", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        self.coordinator.establish_fresh_authority()
        row = self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?",
            (armed["canary_candidate_id"],),
        )[0][0]
        self.assertEqual(row, "unknown")
        self.assertEqual(unknown["state"], "unknown")

    def test_canary_transitions_create_no_network_side_effect(self):
        shadow = self._shadow()
        transport = RecordingCanaryTransport()
        before = {
            str(path.relative_to(self.runtime))
            for path in self.runtime.rglob("*")
            if path.is_file()
        }
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a", transport
        )
        unknown = self.canary.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-net-1", "ambiguous_boundary",
            armed["pre_send_identity"],
        )
        evidence = {
            "evidence_ref": "evidence-net-2",
            "result": "succeeded",
            "source": "read_only",
            "canary_idempotency_key": unknown["canary_idempotency_key"],
            "pre_send_identity": unknown["pre_send_identity"],
            "envelope_fingerprint": unknown["envelope_fingerprint"],
        }
        self.canary.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a", evidence
        )
        after = {
            str(path.relative_to(self.runtime))
            for path in self.runtime.rglob("*")
            if path.is_file()
        }
        new_files = {
            path for path in (after - before)
            if not path.endswith((".sqlite3", ".sqlite3-wal", ".sqlite3-shm",
                                   ".manifest.json"))
        }
        self.assertEqual(new_files, set())
        self.assertEqual(len(transport.observations), 1)

    def test_restart_and_restore_quarantine_armed_candidates(self):
        shadow = self._shadow()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            RecordingCanaryTransport(),
        )
        self.coordinator.establish_fresh_authority()
        self.assertEqual(self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?", (armed["canary_candidate_id"],)
        )[0][0], "quarantined")

        self.tearDown()
        self.setUp()
        shadow = self._shadow()
        armed = self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            RecordingCanaryTransport(),
        )
        backup, _manifest = self.operational.backup_to(
            backup_directory(self.runtime) / "canary-backup.sqlite3"
        )
        self.operational.close()
        self.operational = OperationalSQLiteRepository.restore_from(self.runtime, backup)
        self.assertEqual(self.operational.rows(
            "SELECT state FROM authority_bound_canary_candidates "
            "WHERE canary_candidate_id=?", (armed["canary_candidate_id"],)
        )[0][0], "quarantined")


if __name__ == "__main__":
    unittest.main()
