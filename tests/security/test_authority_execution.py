#!/usr/bin/env python3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from services.binding_authority_types import AUTHORITY_ELIGIBILITY

START = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self):
        self.current = START

    def now(self):
        return self.current


class AuthorityExecutionTests(unittest.TestCase):
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
        self.lease = self.coordinator.acquire(self.scope, "agent-a", "lease-key")

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def _record_and_claim(self):
        target = self.targets.record_target(
            self.scope, self.lease, "refresh", "profile-a", "operator-a", "target-key"
        )
        return self.targets.claim_target(target["target_id"], self.lease, "agent-a")

    def _create(self):
        target = self._record_and_claim()
        return self.executions.create_execution(
            target["target_id"], self.lease, "agent-a"
        )

    def test_execution_and_attempt_are_durable_and_idempotent(self):
        first = self._create()
        replay = self.executions.create_execution(
            first["target_id"], self.lease, "agent-a"
        )

        self.assertEqual(first["status"], "claimed")
        self.assertEqual(first["attempt_status"], "claimed")
        self.assertEqual(first["attempt_number"], 1)
        self.assertEqual(first["authority_eligibility"], AUTHORITY_ELIGIBILITY)
        self.assertEqual(first["execution_id"], replay["execution_id"])
        self.assertEqual(first["attempt_id"], replay["attempt_id"])
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM authority_bound_executions")[0][0],
            1,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM authority_bound_attempts")[0][0],
            1,
        )

    def test_validation_requires_the_full_ownership_tuple(self):
        execution = self._create()
        self.assertEqual(
            self.executions.validate_execution(
                execution["execution_id"], self.lease, "agent-a"
            ),
            AUTHORITY_ELIGIBILITY,
        )
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_executions SET fence_counter=fence_counter + 1 "
                "WHERE execution_id=?",
                (execution["execution_id"],),
            )
        with self.assertRaises(AuthorityRejected):
            self.executions.validate_execution(
                execution["execution_id"], self.lease, "agent-a"
            )

    def test_lease_loss_and_binding_drift_fail_closed(self):
        execution = self._create()
        self.coordinator.release(self.lease, "agent-a", "test_release")
        with self.assertRaises(AuthorityRejected):
            self.executions.validate_execution(
                execution["execution_id"], self.lease, "agent-a"
            )

        self.operational.close()
        self.operational = OperationalSQLiteRepository.open(self.runtime)
        self.coordinator = BindingAuthorityCoordinator(
            self.portable_path,
            self.operational,
            self.clock,
            AuthorityConfig(lease_ttl=timedelta(seconds=30)),
        )
        self.targets = AuthorityBoundTargetService(
            self.portable_path, self.operational, self.coordinator
        )
        self.executions = AuthorityExecutionService(
            self.portable_path, self.operational, self.coordinator
        )

        portable = PortableDomainStore.open(self.portable_path)
        portable.record_verified_identity("profile-a", "identity-b", START.isoformat())
        portable.close()
        with self.assertRaises(AuthorityRejected):
            self.executions.validate_execution(
                execution["execution_id"], self.lease, "agent-a"
            )

    def test_restart_and_restore_quarantine_execution_and_attempt(self):
        execution = self._create()
        backup, _manifest = self.operational.backup_to(
            backup_directory(self.runtime) / "authority-execution.sqlite3"
        )
        self.coordinator.establish_fresh_authority()
        self.assertEqual(
            tuple(
                self.operational.rows(
                    "SELECT status, quarantine_reason FROM authority_bound_executions "
                    "WHERE execution_id=?",
                    (execution["execution_id"],),
                )[0]
            ),
            ("quarantined", "coordinator_restart"),
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT status FROM authority_bound_attempts WHERE attempt_id=?",
                (execution["attempt_id"],),
            )[0][0],
            "quarantined",
        )

        self.operational.close()
        self.operational = OperationalSQLiteRepository.open(self.runtime)
        self.operational.close()
        self.operational = OperationalSQLiteRepository.restore_from(self.runtime, backup)
        self.assertEqual(
            self.operational.rows(
                "SELECT status, quarantine_reason FROM authority_bound_executions "
                "WHERE execution_id=?",
                (execution["execution_id"],),
            )[0][1],
            "operational_restore",
        )

    def test_unknown_execution_cannot_create_a_blind_retry(self):
        execution = self._create()
        with self.operational.transaction():
            self.operational.connection.execute(
                "UPDATE authority_bound_executions SET status='unknown' "
                "WHERE execution_id=?",
                (execution["execution_id"],),
            )
            self.operational.connection.execute(
                "UPDATE authority_bound_attempts SET status='unknown' "
                "WHERE attempt_id=?",
                (execution["attempt_id"],),
            )
        replay = self.executions.create_execution(
            execution["target_id"], self.lease, "agent-a"
        )
        self.assertEqual(replay["execution_id"], execution["execution_id"])
        self.assertEqual(replay["attempt_id"], execution["attempt_id"])
        self.assertEqual(replay["status"], "unknown")
        self.assertEqual(replay["attempt_status"], "unknown")
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM authority_bound_attempts")[0][0],
            1,
        )

    def test_execution_foundation_does_not_enqueue_or_dispatch(self):
        self.operational.insert_job(
            "legacy-job", "legacy", "{}", "legacy-owner", "legacy-key"
        )
        execution = self._create()
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 1
        )
        self.assertEqual(
            self.targets.attempt_dispatch(execution["target_id"])["dispatch_count"], 0
        )
        self.assertFalse(
            self.targets.attempt_dispatch(execution["target_id"])["dispatched"]
        )


if __name__ == "__main__":
    unittest.main()
