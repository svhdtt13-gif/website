#!/usr/bin/env python3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import (
    OperationalSQLiteRepository,
    backup_directory,
)
from repositories.portable_store import PortableDomainStore
from services.authority_bound_targets import AuthorityBoundTargetService
from services.authority_execution import AuthorityExecutionService
from services.binding_authority import (
    AuthorityConfig,
    AuthorityRejected,
    BindingAuthorityCoordinator,
    BindingScope,
)
from services.dispatch_preparation import DispatchPreparationService

START = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self):
        self.current = START

    def now(self):
        return self.current


class DispatchPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.portable_path = self.runtime / "portable.sqlite3"
        portable = PortableDomainStore.create(self.portable_path)
        portable.add_host("host-a", "Host A", "origin-a", START.isoformat())
        portable.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", START.isoformat())
        revision = portable.record_verified_identity(
            "profile-a", "identity-a", START.isoformat()
        )
        portable.bind_profile(
            "binding-a", "host-a", "profile-a", "account-a", 1, "ACTIVE", START.isoformat()
        )
        portable.close()
        self.operational = OperationalSQLiteRepository.create(self.runtime)
        self.clock = FixedClock()
        self.coordinator = BindingAuthorityCoordinator(
            self.portable_path,
            self.operational,
            self.clock,
            AuthorityConfig(lease_ttl=timedelta(seconds=30)),
        )
        self.scope = BindingScope("host-a", "profile-a", 1, "identity-a", revision)
        self.targets = AuthorityBoundTargetService(
            self.portable_path, self.operational, self.coordinator
        )
        self.executions = AuthorityExecutionService(
            self.portable_path, self.operational, self.coordinator
        )
        self.preparation = DispatchPreparationService(
            self.portable_path, self.operational, self.coordinator
        )
        self.lease = self.coordinator.acquire(self.scope, "agent-a", "lease-key")

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def _execution(self):
        target = self.targets.record_target(
            self.scope, self.lease, "refresh", "profile-a", "operator-a", "target-key"
        )
        claimed = self.targets.claim_target(
            target["target_id"], self.lease, "agent-a"
        )
        return self.executions.create_execution(
            claimed["target_id"], self.lease, "agent-a"
        )

    def test_prepare_intent_persists_provenance_and_replays_idempotently(self):
        execution = self._execution()
        first = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        replay = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )

        self.assertEqual(first["status"], "prepared")
        self.assertEqual(first["intent_id"], replay["intent_id"])
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(first["execution_id"], execution["execution_id"])
        self.assertEqual(first["attempt_id"], execution["attempt_id"])
        self.assertEqual(first["authority_epoch"], self.lease.fence.authority_epoch)
        self.assertEqual(first["fence_counter"], self.lease.fence.fence_counter)
        self.assertNotIn("payload", first)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_dispatch_intents"
            )[0][0],
            1,
        )
        self.assertEqual(self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 0)

    def test_existing_intent_replay_after_lease_loss_fails_closed(self):
        execution = self._execution()
        self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        self.coordinator.release(self.lease, "agent-a", "test_release")
        with self.assertRaises(AuthorityRejected):
            self.preparation.prepare_intent(
                execution["execution_id"], self.lease, "agent-a"
            )

    def test_existing_intent_replay_after_binding_drift_fails_closed(self):
        execution = self._execution()
        self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        portable = PortableDomainStore.open(self.portable_path)
        portable.record_verified_identity("profile-a", "identity-b", START.isoformat())
        portable.close()
        with self.assertRaises(AuthorityRejected):
            self.preparation.prepare_intent(
                execution["execution_id"], self.lease, "agent-a"
            )

    def test_revalidation_drift_blocks_new_intent_without_eligibility(self):
        execution = self._execution()
        original = self.coordinator._revalidate_snapshot
        calls = 0

        def drift(scope, snapshot, correlation_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                portable = PortableDomainStore.open(self.portable_path)
                portable.record_verified_identity(
                    "profile-a", "identity-b", START.isoformat()
                )
                portable.close()
            original(scope, snapshot, correlation_id)

        with patch.object(self.coordinator, "_revalidate_snapshot", side_effect=drift):
            result = self.preparation.prepare_intent(
                execution["execution_id"], self.lease, "agent-a"
            )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["authority_eligibility"], None)
        self.assertEqual(result["blocked_reason"], "verified_identity_mismatch")

    def test_unknown_reconciles_without_creating_a_new_intent(self):
        execution = self._execution()
        prepared = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        unknown = self.preparation.record_unknown(
            prepared["intent_id"], self.lease, "agent-a", "ack_ambiguous", "evidence-1"
        )
        replay = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        reconciled = self.preparation.reconcile_unknown(
            unknown["intent_id"], self.lease, "agent-a", "succeeded", "reconcile-1"
        )

        self.assertEqual(unknown["status"], "unknown")
        self.assertEqual(replay["intent_id"], prepared["intent_id"])
        self.assertEqual(replay["status"], "unknown")
        self.assertEqual(reconciled["status"], "reconciled")
        self.assertEqual(reconciled["reconciliation_result"], "succeeded")
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_dispatch_intents"
            )[0][0],
            1,
        )

    def test_sensitive_reconciliation_evidence_is_rejected(self):
        execution = self._execution()
        prepared = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        with self.assertRaises(AuthorityRejected):
            self.preparation.record_unknown(
                prepared["intent_id"], self.lease, "agent-a", "ack_ambiguous", "session-token"
            )

    def test_restart_and_restore_quarantine_live_intent(self):
        execution = self._execution()
        prepared = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        backup, _manifest = self.operational.backup_to(
            backup_directory(self.runtime) / "dispatch-intent.sqlite3"
        )
        self.coordinator.establish_fresh_authority()
        self.assertEqual(
            tuple(self.operational.rows(
                "SELECT status, quarantine_reason FROM authority_bound_dispatch_intents "
                "WHERE intent_id=?",
                (prepared["intent_id"],),
            )[0]),
            ("quarantined", "coordinator_restart"),
        )

        self.operational.close()
        self.operational = OperationalSQLiteRepository.open(self.runtime)
        self.operational.close()
        self.operational = OperationalSQLiteRepository.restore_from(self.runtime, backup)
        self.assertEqual(
            tuple(self.operational.rows(
                "SELECT status, quarantine_reason FROM authority_bound_dispatch_intents "
                "WHERE intent_id=?",
                (prepared["intent_id"],),
            )[0]),
            ("quarantined", "operational_restore"),
        )


if __name__ == "__main__":
    unittest.main()
