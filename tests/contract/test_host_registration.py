#!/usr/bin/env python3
"""Contract tests for configuration-only host registration and binding writes."""
import json
import sys
import tempfile
import unittest
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

    def test_registration_is_server_identity_scoped_and_idempotent(self):
        raw, status, _ = register_configured_host(
            b'{"display_name":"Local Host"}', JSON, self.path, "host-new", NOW
        )
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(data["result"]["created"])
        self.assertEqual(data["result"]["host_id"], "host-new")
        self.assertEqual(data["runtime_authority"], {
            "mode": "LEGACY",
            "active_runtime_owner": None,
            "reason": "Configuration writes do not establish runtime ownership.",
        })
        self.assertFalse(data["controls"]["can_start"])

        raw, status, _ = register_configured_host(
            b'{"display_name":"Local Host"}', JSON, self.path, "host-new", NOW
        )
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(raw)["result"]["created"])
        rows = self.read("SELECT host_id, display_name, origin_ref FROM hosts WHERE host_id='host-new'")
        self.assertEqual([tuple(row) for row in rows], [("host-new", "Local Host", "host_agent:configured")])

    def test_existing_registration_preserves_origin_reference(self):
        register_configured_host(b'{"display_name":"Renamed Host"}', JSON, self.path, "host-a", NOW)
        rows = self.read("SELECT display_name, origin_ref FROM hosts WHERE host_id='host-a'")
        self.assertEqual([tuple(row) for row in rows], [("Renamed Host", "existing-origin")])

    def test_client_cannot_supply_identity_or_store_fields(self):
        before = self.path.read_bytes()
        for body in (
            b'{"host_id":"spoofed"}',
            b'{"origin_ref":"C:\\\\local\\\\path"}',
            b'{"account_ref":"account-a"}',
        ):
            with self.subTest(body=body):
                with self.assertRaises(UpstreamError) as raised:
                    register_configured_host(body, JSON, self.path, "host-a", NOW)
                self.assertEqual(raised.exception.status, 400)
                self.assertEqual(self.path.read_bytes(), before)

    def test_missing_configured_identity_fails_closed(self):
        with self.assertRaises(UpstreamError) as raised:
            register_configured_host(b"{}", JSON, self.path, "", NOW)
        self.assertEqual(raised.exception.status, 503)

    def test_offline_binding_is_atomic_and_idempotent(self):
        raw, status, _ = create_offline_binding(
            b'{"profile_id":"profile-a"}', JSON, self.path, "host-a", NOW
        )
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertTrue(data["result"]["created"])
        self.assertEqual(data["result"]["state"], "OFFLINE")
        self.assertEqual(data["result"]["binding_generation"], 1)
        first_id = data["result"]["binding_id"]
        after_first = self.path.read_bytes()

        raw, status, _ = create_offline_binding(
            b'{"profile_id":"profile-a"}', JSON, self.path, "host-a", NOW
        )
        second = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(second["result"]["created"])
        self.assertEqual(second["result"]["binding_id"], first_id)
        self.assertEqual(self.path.read_bytes(), after_first)
        rows = self.read(
            "SELECT binding_id, state, binding_generation FROM host_profile_bindings "
            "WHERE host_id='host-a' AND profile_id='profile-a'"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(tuple(rows[0])[1:], ("OFFLINE", 1))

    def test_retired_binding_gets_next_generation(self):
        store = PortableDomainStore.open(self.path)
        store.bind_profile("retired", "host-a", "profile-a", "account-a", 1, "RETIRED", NOW)
        store.close()
        raw, status, _ = create_offline_binding(
            b'{"profile_id":"profile-a"}', JSON, self.path, "host-a", NOW
        )
        self.assertEqual(status, 200)
        result = json.loads(raw)["result"]
        self.assertEqual(result["state"], "OFFLINE")
        self.assertEqual(result["binding_generation"], 2)

    def test_active_binding_is_never_changed(self):
        store = PortableDomainStore.open(self.path)
        store.bind_profile("active", "host-a", "profile-a", "account-a", 1, "ACTIVE", NOW)
        store.close()
        before = self.path.read_bytes()
        with self.assertRaises(UpstreamError) as raised:
            create_offline_binding(b'{"profile_id":"profile-a"}', JSON, self.path, "host-a", NOW)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_binding_target_does_not_create_rows(self):
        before = self.path.read_bytes()
        with self.assertRaises(UpstreamError) as raised:
            create_offline_binding(b'{"profile_id":"missing"}', JSON, self.path, "host-a", NOW)
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
