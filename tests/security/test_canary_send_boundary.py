#!/usr/bin/env python3
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

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
from services.canary_send import CanarySingleShotService
from services.canary_send_transport import (
    CanaryDestinationRefused,
    SingleShotCanaryTransport,
)

from tests.security.canary_send_support import TEST_CREDENTIAL, CanarySendFixture


class CanarySendBoundaryTests(CanarySendFixture, unittest.TestCase):
    def test_success_records_sanitized_metadata(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)

        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", transport
        )

        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["outcome"], "succeeded")
        self.assertEqual(result["status_class"], "http_200")
        self.assertIsNotNone(result["response_fingerprint"])
        self.assertIsNotNone(result["succeeded_at"])
        self.assertEqual(result["destination_ref"], stub.url)
        self.assertEqual(stub.hit_count(), 1)
        hit = stub.hits[0]
        self.assertEqual(hit["headers"].get("X-Pre-Send-Identity"), armed["pre_send_identity"])
        self.assertEqual(
            hit["headers"].get("X-Canary-Idempotency-Key"),
            armed["canary_idempotency_key"],
        )
        self.assertEqual(hit["headers"].get("Authorization"), TEST_CREDENTIAL)

    def test_success_replay_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        first = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        self.assertEqual(first["state"], "succeeded")

        replay_transport = self.transport(stub)
        replay = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a", replay_transport
        )

        self.assertEqual(replay["state"], "succeeded")
        self.assertEqual(replay["outcome"], "idempotent_replay")
        self.assertEqual(replay_transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 1)

    def test_unknown_replay_sends_zero(self):
        armed = self.armed_candidate()
        self.sender.arming.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-send-1", "ambiguous_boundary", armed["pre_send_identity"],
        )
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_unknown_candidate_never_actionable(self):
        armed = self.armed_candidate()
        self.sender.arming.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-send-2", "ambiguous_boundary", armed["pre_send_identity"],
        )
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_reconciled_replay_sends_zero(self):
        armed = self.armed_candidate()
        unknown = self.sender.arming.record_ambiguous(
            armed["canary_candidate_id"], self.lease, "agent-a",
            "evidence-send-3", "ambiguous_boundary", armed["pre_send_identity"],
        )
        evidence = {
            "evidence_ref": "evidence-send-4",
            "result": "succeeded",
            "source": "read_only",
            "canary_idempotency_key": unknown["canary_idempotency_key"],
            "pre_send_identity": unknown["pre_send_identity"],
            "envelope_fingerprint": unknown["envelope_fingerprint"],
        }
        reconciled = self.sender.arming.reconcile_unknown(
            armed["canary_candidate_id"], self.lease, "agent-a", evidence
        )
        self.assertEqual(reconciled["state"], "reconciled")
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_wrong_owner_sends_zero(self):
        armed = self.armed_candidate()
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-b", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_expired_lease_sends_zero(self):
        armed = self.armed_candidate()
        self.clock.current += timedelta(seconds=31)
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_released_lease_sends_zero(self):
        armed = self.armed_candidate()
        self.coordinator.release(self.lease, "agent-a", "test-release")
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_binding_drift_sends_zero(self):
        armed = self.armed_candidate()
        portable = PortableDomainStore.open(self.portable_path)
        try:
            with portable.transaction():
                portable.connection.execute(
                    "UPDATE host_profile_bindings SET state='RETIRED' "
                    "WHERE binding_id='binding-a'"
                )
        finally:
            portable.close()
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_epoch_mismatch_sends_zero(self):
        armed = self.armed_candidate()
        self.coordinator.establish_fresh_authority()
        stub = self.stub()
        transport = self.transport(stub)
        with self.assertRaises(AuthorityRejected):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a", transport
            )
        self.assertEqual(transport.transmissions, 0)
        self.assertEqual(stub.hit_count(), 0)

    def test_destination_outside_allowlist_sends_zero(self):
        self.armed_candidate()
        with self.assertRaises(CanaryDestinationRefused):
            SingleShotCanaryTransport("http://example.com/canary")
        stub = self.stub()
        self.assertEqual(stub.hit_count(), 0)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_canary_send_intents"
            )[0][0],
            0,
        )

    def test_ten_concurrent_callers_max_one_mutation(self):
        armed = self.armed_candidate()
        stub = self.stub()

        def send_once(_index):
            operational = OperationalSQLiteRepository.open(self.runtime)
            try:
                coordinator = BindingAuthorityCoordinator(
                    self.portable_path, operational, self.clock,
                    AuthorityConfig(lease_ttl=timedelta(seconds=30)),
                )
                service = CanarySingleShotService(
                    self.portable_path, operational, coordinator,
                    committed_wait_seconds=5.0,
                )
                return service.send(
                    armed["canary_candidate_id"], self.lease, "agent-a",
                    SingleShotCanaryTransport(stub.url),
                )
            finally:
                operational.close()

        with ThreadPoolExecutor(max_workers=10) as executor:
            results = list(executor.map(send_once, range(10)))

        self.assertEqual(stub.hit_count(), 1)
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_canary_send_intents"
            )[0][0],
            1,
        )
        states = {result["state"] for result in results}
        self.assertEqual(states, {"succeeded"})
        self.assertIn("idempotent_replay", {result["outcome"] for result in results})

    def test_no_open_transaction_during_network(self):
        armed = self.armed_candidate()
        stub = self.stub()
        seen = {}

        original_post = SingleShotCanaryTransport.single_post

        def hook(transport_self, body_json, idempotency_key, pre_send_identity):
            seen["in_transaction"] = self.operational.connection.in_transaction
            return original_post(
                transport_self, body_json, idempotency_key, pre_send_identity
            )

        with patch.object(
            SingleShotCanaryTransport, "single_post", autospec=True,
            side_effect=lambda t, b, i, p: hook(t, b, i, p),
        ):
            self.sender.send(
                armed["canary_candidate_id"], self.lease, "agent-a",
                self.transport(stub),
            )
        self.assertIn("in_transaction", seen)
        self.assertFalse(seen["in_transaction"])

    def test_attempts_and_jobs_counts_unchanged(self):
        armed = self.armed_candidate()
        attempts_before = self.operational.rows(
            "SELECT COUNT(*) FROM authority_bound_attempts"
        )[0][0]
        jobs_before = self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0]
        stub = self.stub()
        self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT COUNT(*) FROM authority_bound_attempts"
            )[0][0],
            attempts_before,
        )
        self.assertEqual(
            self.operational.rows("SELECT COUNT(*) FROM jobs")[0][0], jobs_before
        )

    def test_credential_never_persisted_or_logged(self):
        armed = self.armed_candidate()
        stub = self.stub()
        self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        self.assertEqual(stub.hits[0]["headers"].get("Authorization"), TEST_CREDENTIAL)
        db_path = self.operational.path
        raw = db_path.read_bytes()
        self.assertNotIn(TEST_CREDENTIAL.encode(), raw)
        row = self.send_intent_row(armed["pre_send_identity"])
        blob = (
            (row["destination_ref"] or "") + (row["status_class"] or "")
            + (row["reason_class"] or "") + (row["response_fingerprint"] or "")
        )
        self.assertNotIn("test-credential", blob)

    def test_restart_never_revives_actionability(self):
        armed = self.armed_candidate()
        stub = self.stub()
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        self.assertEqual(result["state"], "succeeded")
        self.coordinator.establish_fresh_authority()
        row = self.send_intent_row(armed["pre_send_identity"])
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(stub.hit_count(), 1)

    def test_restore_never_revives_stale_send(self):
        armed = self.armed_candidate()
        stub = self.stub()
        result = self.sender.send(
            armed["canary_candidate_id"], self.lease, "agent-a",
            self.transport(stub),
        )
        self.assertEqual(result["state"], "succeeded")
        backup, _manifest = self.operational.backup_to(
            backup_directory(self.runtime) / "send-backup.sqlite3"
        )
        self.operational.close()
        self.operational = OperationalSQLiteRepository.restore_from(
            self.runtime, backup
        )
        row = self.send_intent_row(armed["pre_send_identity"])
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(stub.hit_count(), 1)


if __name__ == "__main__":
    unittest.main()
