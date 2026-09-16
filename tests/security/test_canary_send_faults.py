#!/usr/bin/env python3
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.portable_store import PortableDomainStore
from services.binding_authority import AuthorityRejected
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
