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

from repositories.operational_sqlite import OperationalSQLiteRepository
from repositories.portable_store import PortableDomainStore
from services.binding_authority import (
    AuthorityConfig,
    AuthorityRejected,
    BindingAuthorityCoordinator,
    BindingScope,
    FenceIdentity,
    RejectionEvidence,
)

START = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self):
        self.current = START

    def now(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


class BindingAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.portable_path = self.runtime / "portable.sqlite3"
        portable = PortableDomainStore.create(self.portable_path)
        portable.add_host("host-a", "Host A", "explicit-test-origin", START.isoformat())
        portable.add_profile(
            "profile-a", "Profile A", "account-a", "VERIFIED", START.isoformat()
        )
        portable.record_verified_identity("profile-a", "identity-a", START.isoformat())
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
        self.scope = BindingScope("host-a", "profile-a", 1, "identity-a")

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def test_acquire_allocates_epoch_scoped_fence_without_jobs(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")

        self.assertEqual(lease.fence.fence_counter, 1)
        self.assertTrue(lease.fence.authority_epoch)
        self.assertEqual(lease.scope, self.scope)
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 0
        )

    def test_competing_agent_is_rejected_without_partial_state(self):
        self.coordinator.acquire(self.scope, "agent-a", "idem-a")

        with self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(self.scope, "agent-b", "idem-b")

        row = self.operational.rows(
            "SELECT owner_id, fence_counter FROM fenced_leases"
        )[0]
        self.assertEqual(tuple(row), ("agent-a", 1))

    def test_expiry_requires_explicit_reconciliation_before_takeover(self):
        old = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.clock.advance(31)

        with self.assertRaises(AuthorityRejected) as raised:
            self.coordinator.acquire(
                self.scope, "agent-b", "idem-b", request_id="expiry-check"
            )

        self.assertEqual(
            raised.exception.evidence.reason_class,
            "expired_lease_requires_reconciliation",
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT state FROM fenced_leases WHERE lease_id=?",
                (old.lease_id,),
            )[0][0],
            "ACQUIRED",
        )
        self.coordinator.reconcile_expired(old, request_id="expiry-reconcile")
        current = self.coordinator.acquire(self.scope, "agent-b", "idem-b")

        self.assertEqual(current.fence.authority_epoch, old.fence.authority_epoch)
        self.assertGreater(current.fence.fence_counter, old.fence.fence_counter)
        with self.assertRaises(AuthorityRejected):
            self.coordinator.heartbeat(old, "agent-a")

    def test_restore_epoch_rejects_old_fence_even_with_same_counter(self):
        old = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.operational.quarantine_restored_authority()
        self.coordinator.establish_fresh_authority()
        self.clock.advance(31)
        current = self.coordinator.acquire(self.scope, "agent-b", "idem-b")

        self.assertNotEqual(current.fence.authority_epoch, old.fence.authority_epoch)
        self.assertEqual(current.fence.fence_counter, 1)
        with self.assertRaises(AuthorityRejected):
            self.coordinator.heartbeat(old, "agent-a")

    def test_restart_epoch_rejects_old_holder_before_new_acquisition(self):
        old = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        restarted = BindingAuthorityCoordinator(
            self.portable_path,
            self.operational,
            self.clock,
            AuthorityConfig(lease_ttl=timedelta(seconds=30)),
        )

        fresh = restarted.establish_fresh_authority()

        self.assertNotEqual(fresh.authority_epoch, old.fence.authority_epoch)
        with self.assertRaises(AuthorityRejected):
            self.coordinator.check_eligibility(old, "agent-a", "idem-a")
        current = restarted.acquire(self.scope, "agent-b", "idem-b")
        self.assertEqual(current.fence.fence_counter, 1)

    def test_reopen_after_process_connections_close_requires_fresh_bootstrap(self):
        self.operational.close()
        self.operational = OperationalSQLiteRepository.open(self.runtime)
        restarted = BindingAuthorityCoordinator(
            self.portable_path,
            self.operational,
            self.clock,
            AuthorityConfig(lease_ttl=timedelta(seconds=30)),
        )

        with self.assertRaises(AuthorityRejected) as raised:
            restarted.acquire(self.scope, "agent-a", "idem-a")
        self.assertEqual(
            raised.exception.evidence.reason_class,
            "fresh_authority_bootstrap_required",
        )
        restarted.establish_fresh_authority()
        self.assertEqual(
            restarted.acquire(self.scope, "agent-a", "idem-a").fence.fence_counter,
            1,
        )

    def test_all_connections_share_fresh_bootstrap_gate(self):
        self.operational.close()
        first = OperationalSQLiteRepository.open(self.runtime)
        second = OperationalSQLiteRepository.open(self.runtime)
        try:
            self.assertTrue(first.requires_fresh_bootstrap)
            self.assertTrue(second.requires_fresh_bootstrap)
        finally:
            second.close()
            self.operational = first

    def test_heartbeat_and_release_are_scoped_to_current_lease(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.clock.advance(5)
        heartbeated = self.coordinator.heartbeat(lease, "agent-a")

        self.assertEqual(heartbeated.state, "HEARTBEATING")
        self.assertGreater(heartbeated.expires_at, lease.expires_at)
        self.coordinator.release(heartbeated, "agent-a", "planned_shutdown")
        self.assertEqual(
            tuple(self.operational.rows(
                "SELECT state, release_reason FROM fenced_leases WHERE lease_id=?",
                (lease.lease_id,),
            )[0]),
            ("RELEASED", "planned_shutdown"),
        )

    def test_expiry_is_rejected_without_eligibility_side_effect(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.clock.advance(31)

        with self.assertRaises(AuthorityRejected):
            self.coordinator.check_eligibility(lease, "agent-a", "idem-a")
        self.assertEqual(
            self.operational.rows(
                "SELECT state FROM fenced_leases WHERE lease_id=?",
                (lease.lease_id,),
            )[0][0],
            "ACQUIRED",
        )

    def test_eligibility_revalidates_identity_and_has_zero_dispatch(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")

        result = self.coordinator.check_eligibility(lease, "agent-a", "idem-a")

        self.assertEqual(result, "AUTHORITY_ELIGIBILITY / FENCED_BINDING_MATCH")
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], 0
        )

    def test_stale_verified_identity_is_rejected(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        portable = PortableDomainStore.open(self.portable_path)
        portable.record_verified_identity("profile-a", "identity-b", START.isoformat())
        portable.close()

        with self.assertRaises(AuthorityRejected):
            self.coordinator.check_eligibility(lease, "agent-a", "idem-a")

    def test_missing_verified_identity_is_rejected(self):
        portable = PortableDomainStore.open(self.portable_path)
        with portable.transaction():
            portable.connection.execute(
                "UPDATE remote_profiles SET verified_identity_ref=NULL WHERE profile_id='profile-a'"
            )
        portable.close()

        with self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(self.scope, "agent-a", "idem-a")

    def test_rejection_evidence_is_structured_and_sanitized(self):
        for request_id in ("/tmp/session-secret-token", "profile-a", "ses_abc123"):
            with self.assertRaises(AuthorityRejected) as raised:
                self.coordinator.acquire(
                    BindingScope("host-a", "profile-a", 2, "identity-a"),
                    "agent-a",
                    "idem-a",
                    request_id=request_id,
                )

            evidence = raised.exception.evidence
            self.assertIsInstance(evidence, RejectionEvidence)
            self.assertEqual(evidence.reason_class, "portable_binding_unavailable")
            self.assertRegex(evidence.correlation_id, r"^corr-[0-9a-f]{16}$")
            serialized = str(raised.exception).lower()
            for secret in ("/tmp", "session", "secret", "token", "profile-a", "ses_abc123"):
                self.assertNotIn(secret, serialized)

    def test_acquire_rolls_back_when_portable_binding_changes_before_commit(self):
        original_revalidate = self.coordinator._revalidate_snapshot

        def mutate_identity(scope, snapshot, correlation_id):
            self.assertEqual(
                self.operational.rows("SELECT COUNT(*) FROM fenced_leases")[0][0],
                1,
            )
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity(
                "profile-a", "identity-b", START.isoformat()
            )
            portable.close()
            original_revalidate(scope, snapshot, correlation_id)

        with patch.object(
            self.coordinator, "_revalidate_snapshot", side_effect=mutate_identity
        ), \
                self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(self.scope, "agent-a", "idem-a")

        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM fenced_leases")[0][0],
            0,
        )

    def test_acquire_rolls_back_when_binding_generation_changes_before_commit(self):
        original_revalidate = self.coordinator._revalidate_snapshot

        def mutate_generation(scope, snapshot, correlation_id):
            portable = PortableDomainStore.open(self.portable_path)
            with portable.transaction(immediate=True):
                portable.connection.execute(
                    "UPDATE host_profile_bindings SET binding_generation=2 "
                    "WHERE host_id='host-a' AND profile_id='profile-a' "
                    "AND binding_generation=1"
                )
            portable.close()
            original_revalidate(scope, snapshot, correlation_id)

        with patch.object(
            self.coordinator, "_revalidate_snapshot", side_effect=mutate_generation
        ), self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(self.scope, "agent-a", "idem-a")

        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM fenced_leases")[0][0],
            0,
        )

    def test_heartbeat_rolls_back_when_portable_binding_changes_before_commit(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        original_revalidate = self.coordinator._revalidate_snapshot

        def mutate_identity(scope, snapshot, correlation_id):
            self.assertEqual(
                self.operational.rows(
                    "SELECT state FROM fenced_leases WHERE lease_id=?",
                    (lease.lease_id,),
                )[0][0],
                "HEARTBEATING",
            )
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity(
                "profile-a", "identity-b", START.isoformat()
            )
            portable.close()
            original_revalidate(scope, snapshot, correlation_id)

        with patch.object(
            self.coordinator, "_revalidate_snapshot", side_effect=mutate_identity
        ), \
                self.assertRaises(AuthorityRejected):
            self.coordinator.heartbeat(lease, "agent-a")

        self.assertEqual(
            self.operational.rows(
                "SELECT state FROM fenced_leases WHERE lease_id=?",
                (lease.lease_id,),
            )[0][0],
            "ACQUIRED",
        )

    def test_release_rolls_back_when_portable_binding_changes_before_commit(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        original_revalidate = self.coordinator._revalidate_snapshot

        def mutate_identity(scope, snapshot, correlation_id):
            self.assertEqual(
                self.operational.rows(
                    "SELECT state FROM fenced_leases WHERE lease_id=?",
                    (lease.lease_id,),
                )[0][0],
                "RELEASED",
            )
            portable = PortableDomainStore.open(self.portable_path)
            portable.record_verified_identity(
                "profile-a", "identity-b", START.isoformat()
            )
            portable.close()
            original_revalidate(scope, snapshot, correlation_id)

        with patch.object(
            self.coordinator, "_revalidate_snapshot", side_effect=mutate_identity
        ), \
                self.assertRaises(AuthorityRejected):
            self.coordinator.release(lease, "agent-a", "planned_shutdown")

        self.assertEqual(
            self.operational.rows(
                "SELECT state FROM fenced_leases WHERE lease_id=?",
                (lease.lease_id,),
            )[0][0],
            "ACQUIRED",
        )

    def test_offline_binding_is_rejected_without_activation(self):
        portable = PortableDomainStore.open(self.portable_path)
        with portable.transaction():
            portable.connection.execute(
                "UPDATE host_profile_bindings SET state='OFFLINE' "
                "WHERE host_id='host-a' AND profile_id='profile-a'"
            )
        portable.close()

        with self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(self.scope, "agent-a", "idem-a")

    def test_malformed_persisted_timestamp_is_structured_rejection(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.operational.connection.execute(
            "UPDATE fenced_leases SET expires_at='not-a-timestamp' WHERE lease_id=?",
            (lease.lease_id,),
        )

        with self.assertRaises(AuthorityRejected) as raised:
            self.coordinator.heartbeat(lease, "agent-a")

        self.assertEqual(
            raised.exception.evidence.reason_class,
            "lease_timestamp_invalid",
        )

    def test_generation_mismatch_is_rejected(self):
        wrong_scope = BindingScope("host-a", "profile-a", 2, "identity-a")

        with self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(wrong_scope, "agent-a", "idem-a")

    def test_idempotency_key_cannot_cross_owner_or_scope(self):
        lease = self.coordinator.acquire(self.scope, "agent-a", "idem-a")

        with self.assertRaises(AuthorityRejected):
            self.coordinator.acquire(self.scope, "agent-b", "idem-a")
        self.assertEqual(self.coordinator.acquire(self.scope, "agent-a", "idem-a"), lease)

    def test_idempotency_key_cannot_replay_released_or_expired_authority(self):
        released = self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.coordinator.release(released, "agent-a", "planned_shutdown")
        with self.assertRaises(AuthorityRejected) as released_error:
            self.coordinator.acquire(self.scope, "agent-a", "idem-a")
        self.assertEqual(
            released_error.exception.evidence.reason_class,
            "lease_not_current",
        )

        expired = self.coordinator.acquire(self.scope, "agent-a", "idem-b")
        self.clock.advance(31)
        with self.assertRaises(AuthorityRejected) as expired_error:
            self.coordinator.acquire(self.scope, "agent-a", "idem-b")
        self.assertEqual(
            expired_error.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(expired.state, "ACQUIRED")

    def test_caller_cannot_supply_authority_timestamp_or_ttl(self):
        with self.assertRaises(TypeError):
            self.coordinator.acquire(
                self.scope, "agent-a", "idem-a", now=START, ttl=timedelta(hours=1)
            )

    def test_concurrent_acquisition_has_one_winner(self):
        barrier = Barrier(2)
        results = []

        def acquire(owner, key):
            operational = OperationalSQLiteRepository.open(self.runtime)
            coordinator = BindingAuthorityCoordinator(
                self.portable_path,
                operational,
                self.clock,
                AuthorityConfig(lease_ttl=timedelta(seconds=30)),
            )
            barrier.wait()
            try:
                results.append((owner, coordinator.acquire(self.scope, owner, key)))
            except AuthorityRejected:
                results.append((owner, None))
            finally:
                operational.close()

        threads = [
            Thread(target=acquire, args=("agent-a", "idem-a")),
            Thread(target=acquire, args=("agent-b", "idem-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(lease is not None for _, lease in results), 1)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM fenced_leases WHERE state IN ('ACQUIRED','HEARTBEATING')"
            )[0][0],
            1,
        )

    def test_fence_identity_compares_epoch_and_counter(self):
        self.assertNotEqual(FenceIdentity("epoch-a", 1), FenceIdentity("epoch-b", 1))


if __name__ == "__main__":
    unittest.main()
