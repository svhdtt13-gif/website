#!/usr/bin/env python3
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import (
    OperationalSQLiteRepository,
    backup_directory,
)
from services.binding_authority import (
    AuthorityConfig,
    AuthorityRejected,
    BindingAuthorityCoordinator,
)
from services.shadow_dispatch import DryRunShadowDispatcher
from services.shadow_dispatch_transport import (
    NullShadowTransport,
    RecordingShadowTransport,
)
from shadow_dispatch_support import ShadowDispatchFixture


class ShadowDispatchAuthorityTests(ShadowDispatchFixture, unittest.TestCase):
    def test_released_lease_rejects_before_shadow_persistence(self):
        _execution, intent = self.prepared()
        self.coordinator.release(self.lease, "agent-a", "agent_shutdown")
        transport = RecordingShadowTransport()

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "release-request", transport
            )

        self.assertEqual(rejected.exception.evidence.reason_class, "lease_not_current")
        self.assertEqual(transport.records, [])
        self.assertEqual(
            self.operational.rows(
                "SELECT status, blocked_reason FROM authority_bound_dispatch_intents "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0:2],
            ("prepared", None),
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_released_lease_cannot_replay_existing_shadow_evaluation(self):
        _execution, intent = self.prepared()
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "replay-release",
            RecordingShadowTransport(),
        )
        self.coordinator.release(self.lease, "agent-a", "replay_release")

        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "replay-release",
                RecordingShadowTransport(),
            )
        self.assertEqual(self.shadow_row(intent["intent_id"])["outcome"], "matched")

    def test_expired_lease_cannot_replay_existing_shadow_evaluation(self):
        _execution, intent = self.prepared()
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "replay-expiry",
            RecordingShadowTransport(),
        )
        self.clock.current += timedelta(seconds=31)

        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "replay-expiry",
                RecordingShadowTransport(),
            )
        self.assertEqual(self.shadow_row(intent["intent_id"])["outcome"], "matched")

    def test_expired_lease_rejects_before_shadow_persistence(self):
        _execution, intent = self.prepared()
        self.clock.current += timedelta(seconds=31)
        transport = RecordingShadowTransport()

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "expiry-request", transport
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(transport.records, [])
        self.assertEqual(
            self.operational.rows(
                "SELECT state FROM fenced_leases WHERE lease_id=?", (self.lease.lease_id,)
            )[0][0],
            "ACQUIRED",
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_identity_drift_rejects_before_shadow_persistence(self):
        _execution, intent = self.prepared()
        portable = self._open_portable()
        portable.record_verified_identity("profile-a", "identity-b", self.clock.now().isoformat())
        portable.close()
        transport = RecordingShadowTransport()

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "identity-request", transport
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "verified_identity_mismatch",
        )
        self.assertEqual(transport.records, [])
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_portable_drift_rejects_blocked_shadow_replay_before_return(self):
        _execution, intent = self.prepared()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_dispatch_intents SET status='blocked' "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "drift-replay",
            NullShadowTransport(),
        )

        portable = self._open_portable()
        try:
            with portable.transaction():
                portable.connection.execute(
                    "UPDATE host_profile_bindings SET state='RETIRED' "
                    "WHERE binding_id=?",
                    ("binding-a",),
                )
        finally:
            portable.close()

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "drift-replay",
                NullShadowTransport(),
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "binding_not_authoritative",
        )
        self.assertEqual(self.shadow_row(intent["intent_id"])["outcome"], "blocked")

    def test_stale_authority_rejects_non_prepared_before_shadow_persistence(self):
        _execution, intent = self.prepared()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_dispatch_intents SET status='blocked' "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )
        self.clock.current += timedelta(seconds=31)

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "stale-nonprepared",
                NullShadowTransport(),
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_authority_rejection_precedes_global_conflict_classification(self):
        _execution, first = self.prepared("first-conflict")
        self.shadow.evaluate(
            first["intent_id"], self.lease, "agent-a", "authority-wins",
            NullShadowTransport(),
        )
        _execution, second = self.prepared("second-conflict")
        self.clock.current += timedelta(seconds=31)

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                second["intent_id"], self.lease, "agent-a", "authority-wins",
                NullShadowTransport(),
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (second["intent_id"],),
            )[0][0],
            0,
        )

    def test_drift_before_second_guard_rejects_without_block_or_persistence(self):
        _execution, intent = self.prepared()
        original = self.shadow._guard_current

        def drift_then_guard(*args):
            portable = self._open_portable()
            try:
                with portable.transaction():
                    portable.connection.execute(
                        "UPDATE host_profile_bindings SET state='RETIRED' "
                        "WHERE binding_id=?",
                        ("binding-a",),
                    )
            finally:
                portable.close()
            return original(*args)

        transport = RecordingShadowTransport()
        with (
            patch.object(self.shadow, "_guard_current", side_effect=drift_then_guard),
            self.assertRaises(AuthorityRejected) as rejected,
        ):
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "second-guard-drift",
                transport,
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "binding_not_authoritative",
        )
        self.assertEqual(transport.records, [])
        self.assertEqual(
            self.operational.rows(
                "SELECT status, blocked_reason FROM authority_bound_dispatch_intents "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0:2],
            ("prepared", None),
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_binding_state_drift_rejects_before_shadow_persistence(self):
        _execution, intent = self.prepared()
        portable = self._open_portable()
        try:
            with portable.transaction():
                portable.connection.execute(
                    "UPDATE host_profile_bindings SET state='RETIRED' WHERE binding_id=?",
                    ("binding-a",),
                )
        finally:
            portable.close()
        transport = RecordingShadowTransport()

        with self.assertRaises(AuthorityRejected) as rejected:
            self.shadow.evaluate(
                intent["intent_id"], self.lease, "agent-a", "binding-request", transport
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "binding_not_authoritative",
        )
        self.assertEqual(transport.records, [])
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_epoch_and_fence_mismatch_reject_before_shadow_persistence(self):
        for column in ("authority_epoch", "fence_counter"):
            with self.subTest(column=column):
                _execution, intent = self.prepared("key-" + column)
                replacement = "different-epoch" if column == "authority_epoch" else 99
                with self.operational.transaction():
                    self.operational.connection.execute(
                        f"UPDATE authority_bound_dispatch_intents SET {column}=? "
                        "WHERE intent_id=?",
                        (replacement, intent["intent_id"]),
                    )
                with self.assertRaises(AuthorityRejected):
                    self.shadow.evaluate(
                        intent["intent_id"], self.lease, "agent-a", "mismatch-" + column,
                        RecordingShadowTransport(),
                    )
                self.assertEqual(
                    self.operational.rows(
                        "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                        "WHERE intent_id=?",
                        (intent["intent_id"],),
                    )[0][0],
                    0,
                )

    def test_replacement_lease_cannot_reuse_old_prepared_intent(self):
        _execution, intent = self.prepared()
        self.coordinator.release(self.lease, "agent-a", "replacement")
        replacement = self.coordinator.acquire(self.scope, "agent-b", "replacement-lease")

        with self.assertRaises(AuthorityRejected):
            self.shadow.evaluate(
                intent["intent_id"], replacement, "agent-b", "replacement-request",
                RecordingShadowTransport(),
            )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations "
                "WHERE intent_id=?",
                (intent["intent_id"],),
            )[0][0],
            0,
        )

    def test_restart_quarantines_existing_shadow_evaluation(self):
        _execution, intent = self.prepared()
        first = self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "restart-request",
            RecordingShadowTransport(),
        )

        self.coordinator.establish_fresh_authority()
        row = self.shadow_row(intent["intent_id"])

        self.assertEqual(first["status"], "matched")
        self.assertEqual(row["outcome"], "quarantined")
        self.assertEqual(row["quarantine_reason"], "coordinator_restart")
        self.assertIsNotNone(row["quarantined_at"])

    def test_restore_quarantines_existing_shadow_evaluation(self):
        _execution, intent = self.prepared()
        self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "restore-request",
            RecordingShadowTransport(),
        )
        backup, _manifest = self.operational.backup_to(
            backup_directory(self.runtime) / "shadow-backup.sqlite3"
        )
        self.operational.close()
        self.operational = self.operational.__class__.restore_from(self.runtime, backup)

        row = self.shadow_row(intent["intent_id"])
        self.assertEqual(row["outcome"], "quarantined")
        self.assertEqual(row["quarantine_reason"], "operational_restore")

    def test_concurrent_same_request_has_one_durable_evaluation(self):
        _execution, intent = self.prepared()
        results = []

        def evaluate():
            operational = OperationalSQLiteRepository.open(self.runtime)
            try:
                coordinator = BindingAuthorityCoordinator(
                    self.portable_path,
                    operational,
                    self.clock,
                    AuthorityConfig(lease_ttl=timedelta(seconds=30)),
                )
                dispatcher = DryRunShadowDispatcher(
                    self.portable_path, operational, coordinator
                )
                results.append(
                    dispatcher.evaluate(
                        intent["intent_id"], self.lease, "agent-a", "same-request",
                        RecordingShadowTransport(),
                    )
                )
            finally:
                operational.close()

        first = self._thread(evaluate)
        second = self._thread(evaluate)
        first.start()
        second.start()
        first.join()
        second.join()

        self.assertEqual(len(results), 2)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_shadow_evaluations"
            )[0][0],
            1,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM authority_bound_attempts")[0][0],
            1,
        )
        self.assertEqual(
            {result["outcome"] for result in results},
            {"matched", "idempotent_replay"},
        )

    def _open_portable(self):
        from repositories.portable_store import PortableDomainStore

        return PortableDomainStore.open(self.portable_path)

    @staticmethod
    def _thread(function):
        from threading import Thread

        return Thread(target=function)


if __name__ == "__main__":
    unittest.main()
