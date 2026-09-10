#!/usr/bin/env python3
"""Contract tests for configuration-only host registration and binding writes."""
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.portable_store import PortableDomainStore  # noqa: E402
from services.host_registration import (  # noqa: E402
    create_offline_binding,
    register_configured_host,
)
from repositories.aitool import UpstreamError  # noqa: E402


NOW = "2026-09-10T00:00:00+00:00"
JSON = "application/json"


class HostRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.path)
        store.add_host("host-a", "Existing Host", "existing-origin", NOW)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def read(self, query):
        store = PortableDomainStore.open(self.path)
        try:
            return store.connection.execute(query).fetchall()
        finally:
            store.close()

    def registration(self, host_id="host-a", display_name="Existing Host"):
        return register_configured_host(
            json.dumps({"host_id": host_id, "display_name": display_name}).encode(),
            JSON,
            self.path,
            "host-a",
            NOW,
        )

    def binding(self, host_id="host-a", profile_id="profile-a"):
        return create_offline_binding(
            json.dumps({"host_id": host_id, "profile_id": profile_id}).encode(),
            JSON,
            self.path,
            "host-a",
            NOW,
        )

    def test_registration_is_server_identity_scoped_and_idempotent(self):
        raw, status, _ = self.registration("host-new", "Local Host")
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(data["result"]["created"])
        self.assertEqual(data["result"]["host_id"], "host-new")
        self.assertEqual(data["runtime_effect"], "NONE")
        self.assertEqual(data["runtime_authority"], {
            "mode": "LEGACY",
            "active_runtime_owner": None,
            "reason": "Configuration writes do not establish runtime ownership.",
        })
        self.assertFalse(data["controls"]["can_start"])

        raw, status, _ = self.registration("host-new", "Local Host")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["result"]["created"])
        rows = self.read("SELECT host_id, display_name, origin_ref FROM hosts WHERE host_id='host-new'")
        self.assertEqual([tuple(row) for row in rows], [("host-new", "Local Host", "host_agent:configured")])

    def test_existing_registration_with_same_name_does_not_change_host(self):
        raw, status, _ = self.registration()
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["result"]["created"])
        rows = self.read("SELECT display_name, origin_ref FROM hosts WHERE host_id='host-a'")
        self.assertEqual([tuple(row) for row in rows], [("Existing Host", "existing-origin")])

    def test_existing_registration_rename_is_conflict_and_zero_write(self):
        before = self.path.read_bytes()
        with self.assertRaises(UpstreamError) as raised:
            self.registration("host-a", "Renamed Host")
        self.assertEqual(raised.exception.status, 409)
        self.assertIn(b"display_name", raised.exception.body)
        self.assertEqual(self.path.read_bytes(), before)
        rows = self.read("SELECT display_name FROM hosts WHERE host_id='host-a'")
        self.assertEqual([tuple(row) for row in rows], [("Existing Host",)])

    def test_client_host_id_must_match_configured_identity(self):
        before = self.path.read_bytes()
        with self.assertRaises(UpstreamError) as raised:
            self.registration("other-host", "Other")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_malformed_control_wrong_type_and_overlength_requests_fail(self):
        bodies = (
            b"{}",
            b'{"host_id":"host-a"}',
            b'{"host_id":7,"display_name":"Host"}',
            b'{"host_id":"host-a","display_name":7}',
            b'{"host_id":"host-a","display_name":"Host","state":"ACTIVE"}',
            json.dumps({"host_id": "host-a", "display_name": "x" * 201}).encode(),
        )
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(UpstreamError) as raised:
                    register_configured_host(body, JSON, self.path, "host-a", NOW)
                self.assertEqual(raised.exception.status, 400)

        binding_bodies = (
            b"{}",
            b'{"host_id":"host-a"}',
            b'{"host_id":7,"profile_id":"profile-a"}',
            b'{"host_id":"host-a","profile_id":7}',
            b'{"host_id":"host-a","profile_id":"profile-a","state":"ACTIVE"}',
            json.dumps({"host_id": "host-a", "profile_id": "x" * 201}).encode(),
        )
        for body in binding_bodies:
            with self.subTest(body=body):
                with self.assertRaises(UpstreamError) as raised:
                    create_offline_binding(body, JSON, self.path, "host-a", NOW)
                self.assertEqual(raised.exception.status, 400)

    def test_offline_binding_is_atomic_idempotent_and_audited(self):
        raw, status, _ = self.binding()
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(data["result"]["created"])
        self.assertEqual(data["result"]["state"], "OFFLINE")
        self.assertEqual(data["result"]["binding_generation"], 1)
        self.assertEqual(data["runtime_effect"], "NONE")
        first_id = data["result"]["binding_id"]

        raw, status, _ = self.binding()
        second = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(second["result"]["created"])
        self.assertEqual(second["result"]["binding_id"], first_id)
        rows = self.read(
            "SELECT binding_id, state, binding_generation FROM host_profile_bindings "
            "WHERE host_id='host-a' AND profile_id='profile-a'"
        )
        self.assertEqual([tuple(row) for row in rows], [(first_id, "OFFLINE", 1)])
        audit = self.read(
            "SELECT profile_id, event_id, event_type, actor_ref, source_ref "
            "FROM profile_audit_events WHERE profile_id='profile-a'"
        )
        self.assertEqual(len(audit), 1)
        self.assertEqual(tuple(audit[0]), (
            "profile-a",
            f"binding:{first_id}:created",
            "host_binding_created",
            "webapp:host_registration",
            "webapp:host_binding",
        ))

    def test_retired_binding_gets_next_generation(self):
        store = PortableDomainStore.open(self.path)
        store.bind_profile("retired", "host-a", "profile-a", "account-a", 1, "RETIRED", NOW)
        store.close()
        raw, status, _ = self.binding()
        self.assertEqual(status, 200)
        result = json.loads(raw)["result"]
        self.assertEqual(result["state"], "OFFLINE")
        self.assertEqual(result["binding_generation"], 2)
        self.assertEqual(len(self.read("SELECT 1 FROM profile_audit_events")), 1)

    def test_active_binding_is_never_changed_or_audited(self):
        store = PortableDomainStore.open(self.path)
        store.bind_profile("active", "host-a", "profile-a", "account-a", 1, "ACTIVE", NOW)
        store.close()
        before = self.path.read_bytes()
        with self.assertRaises(UpstreamError) as raised:
            self.binding()
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.read("SELECT 1 FROM profile_audit_events")), 0)

    def test_unknown_binding_target_does_not_create_rows(self):
        before = self.path.read_bytes()
        with self.assertRaises(UpstreamError) as raised:
            self.binding(profile_id="missing")
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_concurrent_binding_requests_serialize_generation_and_audit(self):
        barrier = threading.Barrier(4)

        def invoke():
            barrier.wait()
            return self.binding()

        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda _: invoke(), range(4)))
        payloads = [json.loads(raw) for raw, status, _ in responses]
        self.assertEqual([status for _, status, _ in responses], [200] * 4)
        self.assertEqual({item["result"]["binding_id"] for item in payloads}, {
            payloads[0]["result"]["binding_id"]
        })
        self.assertEqual(sum(item["result"]["created"] for item in payloads), 1)
        self.assertEqual(len(self.read(
            "SELECT 1 FROM host_profile_bindings "
            "WHERE host_id='host-a' AND profile_id='profile-a'"
        )), 1)
        self.assertEqual(len(self.read("SELECT 1 FROM profile_audit_events WHERE profile_id='profile-a'")), 1)
        self.assertEqual(self.read(
            "SELECT binding_generation FROM host_profile_bindings "
            "WHERE host_id='host-a' AND profile_id='profile-a'"
        )[0][0], 1)


if __name__ == "__main__":
    unittest.main()
