#!/usr/bin/env python3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import OperationalSQLiteRepository
from repositories.portable_store import PortableDomainStore
from services.authority_bound_targets import AuthorityBoundTargetService
from services.authority_execution import AuthorityExecutionService
from services.binding_authority import (
    AuthorityConfig,
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


class DispatchPreparationReviewBlockerTests(unittest.TestCase):
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
        claimed = self.targets.claim_target(target["target_id"], self.lease, "agent-a")
        return self.executions.create_execution(
            claimed["target_id"], self.lease, "agent-a"
        )

    def test_unknown_remains_terminal_when_binding_drifts_during_terminal_write(self):
        execution = self._execution()
        prepared = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        original = self.preparation._guard_intent

        def guard_and_drift(*args):
            row = original(*args)
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity(
                "profile-a", "identity-b", START.isoformat()
            )
            portable.close()
            return row

        with patch.object(self.preparation, "_guard_intent", side_effect=guard_and_drift):
            result = self.preparation.record_unknown(
                prepared["intent_id"],
                self.lease,
                "agent-a",
                "ack_ambiguous",
                "evidence-1",
            )

        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["outcome"], "unknown_recorded")
        self.assertEqual(
            self.operational.rows(
                "SELECT status FROM authority_bound_dispatch_intents WHERE intent_id=?",
                (prepared["intent_id"],),
            )[0][0],
            "unknown",
        )

    def test_reconciled_remains_terminal_when_binding_drifts_during_terminal_write(self):
        execution = self._execution()
        prepared = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        self.preparation.record_unknown(
            prepared["intent_id"],
            self.lease,
            "agent-a",
            "ack_ambiguous",
            "evidence-1",
        )
        original = self.preparation._guard_intent

        def guard_and_drift(*args):
            row = original(*args)
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity(
                "profile-a", "identity-b", START.isoformat()
            )
            portable.close()
            return row

        with patch.object(self.preparation, "_guard_intent", side_effect=guard_and_drift):
            result = self.preparation.reconcile_unknown(
                prepared["intent_id"],
                self.lease,
                "agent-a",
                "succeeded",
                "reconcile-1",
            )

        self.assertEqual(result["status"], "reconciled")
        self.assertEqual(result["outcome"], "reconciled")
        self.assertEqual(
            self.operational.rows(
                "SELECT status FROM authority_bound_dispatch_intents WHERE intent_id=?",
                (prepared["intent_id"],),
            )[0][0],
            "reconciled",
        )

    def test_concurrent_prepare_has_one_logical_winner(self):
        execution = self._execution()
        start = Barrier(2)

        def prepare():
            operational = OperationalSQLiteRepository.open(self.runtime)
            try:
                coordinator = BindingAuthorityCoordinator(
                    self.portable_path,
                    operational,
                    FixedClock(),
                    AuthorityConfig(lease_ttl=timedelta(seconds=30)),
                )
                preparation = DispatchPreparationService(
                    self.portable_path, operational, coordinator
                )
                start.wait()
                return preparation.prepare_intent(
                    execution["execution_id"], self.lease, "agent-a"
                )
            finally:
                operational.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            future_a = executor.submit(prepare)
            future_b = executor.submit(prepare)
            results = [future_a.result(), future_b.result()]

        self.assertCountEqual(
            [result["outcome"] for result in results],
            ["prepared", "idempotent_replay"],
        )
        self.assertEqual(
            {result["intent_id"] for result in results},
            {results[0]["intent_id"]},
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_dispatch_intents"
            )[0][0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
