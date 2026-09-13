#!/usr/bin/env python3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier, Thread
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import (
    OperationalSQLiteRepository,
    backup_directory,
)
from repositories.portable_store import PortableDomainStore
from services.authority_bound_targets import AuthorityBoundTargetService
from services.binding_authority import (
    AuthorityConfig,
    AuthorityRejected,
    BindingAuthorityCoordinator,
    BindingScope,
)

START = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self):
        self.current = START

    def now(self):
        return self.current


class AuthorityBoundTargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.portable_path = self.runtime / "portable.sqlite3"
        portable = PortableDomainStore.create(self.portable_path)
        portable.add_host("host-a", "Host A", "origin-a", START.isoformat())
        portable.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", START.isoformat())
        revision = portable.record_verified_identity("profile-a", "identity-a", START.isoformat())
        portable.bind_profile("binding-a", "host-a", "profile-a", "account-a", 1, "ACTIVE", START.isoformat())
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
        self.service = AuthorityBoundTargetService(
            self.portable_path, self.operational, self.coordinator
        )
        self.lease = self.coordinator.acquire(self.scope, "agent-a", "lease-key")

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def _record(self, key="target-key", target_ref="profile-a"):
        return self.service.record_target(
            self.scope, self.lease, "refresh", target_ref, "operator-a", key
        )

    def test_schema_and_legacy_claim_are_structurally_separate(self):
        self.operational.insert_job(
            "legacy-job", "legacy", "{}", "legacy-owner", "legacy-key"
        )
        row = self._record()
        columns = {
            item[1]
            for item in self.operational.rows(
                "PRAGMA table_info(authority_bound_targets)"
            )
        }
        self.assertTrue(
            {
                "host_id", "profile_id", "binding_generation",
                "verified_identity_ref", "verified_identity_revision",
                "authority_epoch", "fence_counter", "idempotency_key",
            }.issubset(columns)
        )
        self.assertFalse(
            self.operational.claim_job(
                row["job_id"], "agent-a", "remote_io", "2999-01-01T00:00:00+00:00"
            )
        )
        self.assertEqual(self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 1)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_targets WHERE job_id='legacy-job'"
            )[0][0],
            0,
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT status FROM authority_bound_targets WHERE target_id=?",
                (row["target_id"],),
            )[0][0],
            "claimable",
        )

    def test_same_key_replay_is_exact_and_conflicts_fail_closed(self):
        first = self._record()
        replay = self._record()
        self.assertEqual(first["target_id"], replay["target_id"])
        self.assertEqual(first["job_id"], replay["job_id"])
        self.assertEqual(replay["outcome"], "idempotent_replay")
        with self.assertRaises(AuthorityRejected):
            self._record(target_ref="different-target")
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM authority_bound_targets")[0][0], 1
        )

    def test_claim_and_no_dispatch_preserve_operational_only_scope(self):
        recorded = self._record()
        claimed = self.service.claim_target(recorded["target_id"], self.lease, "agent-a")
        self.assertEqual(claimed["status"], "claimed")
        result = self.service.attempt_dispatch(recorded["target_id"])
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["dispatch_count"], 0)
        self.assertEqual(self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 0)

    def test_enqueue_drift_quarantines_provisional_target(self):
        original = self.coordinator._revalidate_snapshot

        def drift(scope, snapshot, correlation_id):
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity("profile-a", "identity-b", START.isoformat())
            portable.close()
            original(scope, snapshot, correlation_id)

        with patch.object(self.coordinator, "_revalidate_snapshot", side_effect=drift):
            result = self._record()
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["quarantine_reason"], "verified_identity_mismatch")
        replay = self._record()
        self.assertEqual(replay["target_id"], result["target_id"])
        self.assertEqual(replay["status"], "quarantined")

    def test_claim_drift_quarantines_provisional_claim(self):
        recorded = self._record()
        original = self.coordinator._revalidate_snapshot

        def drift(scope, snapshot, correlation_id):
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity("profile-a", "identity-b", START.isoformat())
            portable.close()
            original(scope, snapshot, correlation_id)

        with patch.object(self.coordinator, "_revalidate_snapshot", side_effect=drift):
            result = self.service.claim_target(recorded["target_id"], self.lease, "agent-a")
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(result["claimed_by"], None)

    def test_restart_quarantines_old_claimable_target(self):
        recorded = self._record()
        self.coordinator.establish_fresh_authority()
        row = self.operational.rows(
            "SELECT status, quarantine_reason FROM authority_bound_targets WHERE target_id=?",
            (recorded["target_id"],),
        )[0]
        self.assertEqual(tuple(row), ("quarantined", "coordinator_restart"))

    def test_restore_quarantines_old_claimable_target(self):
        recorded = self._record()
        backup, _manifest = self.operational.backup_to(
            backup_directory(self.runtime) / "authority-target.sqlite3"
        )
        self.operational.close()
        self.operational = OperationalSQLiteRepository.restore_from(self.runtime, backup)
        row = self.operational.rows(
            "SELECT status, quarantine_reason FROM authority_bound_targets WHERE target_id=?",
            (recorded["target_id"],),
        )[0]
        self.assertEqual(tuple(row), ("quarantined", "operational_restore"))

    def test_sensitive_references_are_rejected_and_request_ids_are_not_persisted(self):
        cases = (
            ("target_ref", "raw-payload"),
            ("requested_by", "session-token"),
            ("idempotency_key", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJhIn0.signature"),
        )
        for field, value in cases:
            values = {
                "operation_kind": "refresh",
                "target_ref": "profile-a",
                "requested_by": "operator-a",
                "idempotency_key": "target-key-" + field,
            }
            values[field] = value
            with self.assertRaises(AuthorityRejected):
                self.service.record_target(
                    self.scope,
                    self.lease,
                    values["operation_kind"],
                    values["target_ref"],
                    values["requested_by"],
                    values["idempotency_key"],
                )

        result = self.service.record_target(
            self.scope,
            self.lease,
            "refresh",
            "profile-a",
            "operator-a",
            "safe-target-key",
            request_id="session-token",
        )
        self.assertNotIn("session-token", result["provenance_json"])
        self.assertNotIn("session-token", str(result))

    def test_concurrent_claims_have_one_winner(self):
        recorded = self._record()
        barrier = Barrier(2)
        results = []

        def claim(owner):
            operational = OperationalSQLiteRepository.open(self.runtime)
            coordinator = BindingAuthorityCoordinator(
                self.portable_path,
                operational,
                self.clock,
                AuthorityConfig(lease_ttl=timedelta(seconds=30)),
            )
            service = AuthorityBoundTargetService(
                self.portable_path, operational, coordinator
            )
            barrier.wait()
            try:
                results.append(
                    service.claim_target(recorded["target_id"], self.lease, owner)
                )
            except AuthorityRejected:
                results.append(None)
            finally:
                operational.close()

        threads = [
            Thread(target=claim, args=("agent-a",)),
            Thread(target=claim, args=("agent-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(
            tuple(self.operational.rows(
                "SELECT status, claimed_by FROM authority_bound_targets "
                "WHERE target_id=?",
                (recorded["target_id"],),
            )[0]),
            ("claimed", "agent-a"),
        )


if __name__ == "__main__":
    unittest.main()
