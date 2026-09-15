#!/usr/bin/env python3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

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
