#!/usr/bin/env python3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import OperationalSQLiteRepository
from repositories.portable_store import PortableDomainStore
from services.binding_authority import (
    AuthorityConfig,
    AuthorityRejected,
    BindingAuthorityCoordinator,
)
from services.canary_send import CanarySingleShotService
from services.canary_send_transport import (
    CanaryDestinationRefused,
    CanarySendAmbiguous,
    SingleShotCanaryTransport,
)

from tests.security.canary_send_support import CanarySendFixture, StubBehavior


class CanarySendFaultTests(CanarySendFixture, unittest.TestCase):
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

    def test_drift_before_presend_commit_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        original = self.sender.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self._retire_binding()
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.sender.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)
        row = self.send_intent_row(armed["pre_send_identity"])
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "unknown")
        candidate = self.candidate_row(armed["canary_candidate_id"])
        self.assertEqual(candidate["state"], "armed")

    def test_expiry_after_commit_before_network_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        original = self.sender.shadow._guard_current
        calls = []

        def flank(intent, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self.clock.current += timedelta(seconds=31)
            return original(intent, lease, owner_id, correlation_id)

        with patch.object(
            self.sender.shadow, "_guard_current", side_effect=flank
        ), self.assertRaises(AuthorityRejected) as rejected:
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)
        row = self.send_intent_row(armed["pre_send_identity"])
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "unknown")

    def test_failure_before_connect_sends_zero(self):
        armed = self.armed_candidate()
        transport = SingleShotCanaryTransport("http://127.0.0.1:1/canary", timeout=2.0)
        with self.assertRaises(CanarySendAmbiguous):
            transport.single_post("{}", armed["canary_idempotency_key"], armed["pre_send_identity"])
        self.assertEqual(transport.transmissions, 1)

    def test_refused_connection_marks_unknown_without_mutation(self):
        armed = self.armed_candidate()
        transport = SingleShotCanaryTransport("http://127.0.0.1:1/canary", timeout=2.0)
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["outcome"], "unknown")

    def test_timeout_in_write_marks_unknown(self):
        armed = self.armed_candidate()
        stub = self.stub(StubBehavior(mode="success", delay=3.0))
        transport = self.transport(stub, timeout=0.5)
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(result["state"], "unknown")
        candidate = self.candidate_row(armed["canary_candidate_id"])
        self.assertEqual(candidate["state"], "unknown")

    def test_response_lost_after_write_marks_unknown(self):
        armed = self.armed_candidate()
        stub = self.stub(StubBehavior(mode="reset_after_read"))
        transport = self.transport(stub, timeout=5.0)
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["outcome"], "unknown")
        candidate = self.candidate_row(armed["canary_candidate_id"])
        self.assertEqual(candidate["state"], "unknown")

    def test_success_then_commit_error_recovers_to_unknown_without_resend(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        with patch.object(
            self.sender, "_record_success_outcome",
            side_effect=RuntimeError("simulated crash before commit"),
        ), self.assertRaises(RuntimeError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(stub.hit_count(), 1)
        row = self.send_intent_row(armed["pre_send_identity"])
        self.assertEqual(row["state"], "committed")

        fresh_transport = self.transport(stub)
        replay = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", fresh_transport
        )
        self.assertEqual(replay["state"], "unknown")
        self.assertEqual(fresh_transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 1)

    def test_timeout_downgrade_with_drift_mutates_nothing(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        with patch.object(
            self.sender, "_record_success_outcome",
            side_effect=RuntimeError("simulated crash before commit"),
        ), self.assertRaises(RuntimeError):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        row_before = self.send_intent_row(armed["pre_send_identity"])
        self.assertEqual(row_before["state"], "committed")

        original_guarded = self.sender._guarded_intent_state
        calls = []

        def flank(canary_candidate_id, lease, owner_id, correlation_id):
            calls.append(1)
            if len(calls) == 2:
                self.clock.current += timedelta(seconds=31)
            return original_guarded(
                canary_candidate_id, lease, owner_id, correlation_id
            )

        marked = []
        original_mark = self.sender._mark_unknown_locked

        def spy_mark(identity, reason_class, correlation_id):
            marked.append(identity)
            return original_mark(identity, reason_class, correlation_id)

        fresh_transport = self.transport(stub)
        with patch.object(
            self.sender, "_guarded_intent_state", side_effect=flank
        ), patch.object(
            self.sender, "_mark_unknown_locked", side_effect=spy_mark
        ), self.assertRaises(AuthorityRejected) as rejected:
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a",
                fresh_transport,
            )

        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(marked, [])
        self.assertEqual(fresh_transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 1)
        row_after = self.send_intent_row(armed["pre_send_identity"])
        self.assertEqual(row_after, row_before)

    def test_error_status_response_marks_unknown(self):
        armed = self.armed_candidate()
        stub = self.stub(StubBehavior(mode="error_status", status=500))
        transport = self.transport(stub)
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(result["state"], "unknown")

    def test_redirect_marks_unknown(self):
        armed = self.armed_candidate()
        stub = self.stub(StubBehavior(mode="redirect"))
        transport = self.transport(stub)
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(stub.hit_count(), 1)

    def test_succeeded_replay_after_expiry_rejects_authority_first(self):
        armed = self.armed_candidate()
        stub = self.stub()
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        self.assertEqual(result["state"], "succeeded")
        self.clock.current += timedelta(seconds=31)
        fresh_transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected) as rejected:
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a",
                fresh_transport,
            )
        self.assertEqual(
            rejected.exception.evidence.reason_class,
            "lease_expired_requires_reconciliation",
        )
        self.assertEqual(fresh_transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 1)

    def test_unknown_replay_after_drift_rejects_authority_first(self):
        armed = self.armed_candidate()
        transport = SingleShotCanaryTransport("http://127.0.0.1:1/canary", timeout=2.0)
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )
        self.assertEqual(result["state"], "unknown")
        self._retire_binding()
        fresh_transport = self.transport(self.stub())
        with self.assertRaises(AuthorityRejected) as rejected:
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a",
                fresh_transport,
            )
        self.assertNotEqual(
            rejected.exception.evidence.reason_class, "canary_send_not_actionable"
        )
        self.assertEqual(fresh_transport.transmissions, 0)

    def test_contended_loser_with_drift_during_poll_rejects(self):
        armed = self.armed_candidate()
        stub = self.stub(StubBehavior(mode="success", delay=2.0))
        errors = {}

        def send_winner():
            operational = OperationalSQLiteRepository.open(self.runtime)
            try:
                coordinator = BindingAuthorityCoordinator(
                    self.portable_path, operational, self.clock,
                    AuthorityConfig(lease_ttl=timedelta(seconds=60)),
                )
                service = CanarySingleShotService(
                    self.portable_path, operational, coordinator,
                    committed_wait_seconds=5.0,
                )
                return service.send(
                    armed["canary_candidate_id"], self.lease, "agent-a",
                    SingleShotCanaryTransport(stub.url, timeout=10.0),
                )
            finally:
                operational.close()

        def send_loser():
            operational = OperationalSQLiteRepository.open(self.runtime)
            try:
                coordinator = BindingAuthorityCoordinator(
                    self.portable_path, operational, self.clock,
                    AuthorityConfig(lease_ttl=timedelta(seconds=60)),
                )
                service = CanarySingleShotService(
                    self.portable_path, operational, coordinator,
                    committed_wait_seconds=5.0,
                )
                original_resolve = service._resolve_committed

                def resolve_and_expire(
                    existing, candidate_id, lease, owner_id, correlation_id
                ):
                    self.clock.current += timedelta(seconds=61)
                    return original_resolve(
                        existing, candidate_id, lease, owner_id, correlation_id
                    )

                with patch.object(
                    service, "_resolve_committed", side_effect=resolve_and_expire
                ):
                    return service.send(
                        armed["canary_candidate_id"], self.lease, "agent-a",
                        SingleShotCanaryTransport(stub.url, timeout=10.0),
                    )
            except AuthorityRejected as rejected:
                errors["reason"] = rejected.evidence.reason_class
                raise
            finally:
                operational.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            winner_future = executor.submit(send_winner)
            for _ in range(100):
                if self.send_intent_row(armed["pre_send_identity"]) is not None:
                    break
                import time as _time

                _time.sleep(0.05)
            loser_future = executor.submit(send_loser)
            winner = winner_future.result(timeout=30)
            with self.assertRaises(AuthorityRejected):
                loser_future.result(timeout=30)

        self.assertEqual(winner["state"], "succeeded")
        self.assertEqual(errors.get("reason"), "lease_expired_requires_reconciliation")
        self.assertEqual(stub.hit_count(), 1)
        self.assertEqual(
            self.send_intent_row(armed["pre_send_identity"])["state"], "succeeded"
        )

    def test_destination_with_query_or_fragment_refused(self):
        stub = self.stub()
        with self.assertRaises(CanaryDestinationRefused):
            SingleShotCanaryTransport(stub.url + "?token=SECRET-VALUE-1")
        with self.assertRaises(CanaryDestinationRefused):
            SingleShotCanaryTransport(stub.url + "#section")
        self.assertEqual(stub.hit_count(), 0)

    def test_persisted_destinations_carry_no_query_or_fragment(self):
        armed = self.armed_candidate()
        stub = self.stub()
        self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        rows = self.operational.rows(
            "SELECT destination_ref FROM authority_bound_canary_send_intents"
        )
        self.assertTrue(rows)
        for (destination_ref,) in rows:
            self.assertNotIn("?", destination_ref)
            self.assertNotIn("#", destination_ref)
            self.assertNotIn("token", destination_ref.lower())
        self.assertEqual(rows[0][0], stub.url)

    def test_transport_single_use_refuses_second_post(self):
        stub = self.stub()
        transport = self.transport(stub)
        transport.single_post("{}", "key-1", "pre-send-1")
        with self.assertRaises(CanaryDestinationRefused):
            transport.single_post("{}", "key-1", "pre-send-1")
        self.assertEqual(stub.hit_count(), 1)

    def test_destination_with_credentials_refused(self):
        with self.assertRaises(CanaryDestinationRefused):
            SingleShotCanaryTransport("http://user:pass@127.0.0.1:9/canary")

    def test_non_loopback_destination_refused(self):
        with self.assertRaises(CanaryDestinationRefused):
            SingleShotCanaryTransport("http://10.0.0.5/canary")


if __name__ == "__main__":
    unittest.main()
