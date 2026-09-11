#!/usr/bin/env python3
"""Route regressions for guarded Portable Domain Store configuration writes."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

config = importlib.import_module("config")
create_app = importlib.import_module("app").create_app
PortableDomainStore = importlib.import_module(
    "repositories.portable_store"
).PortableDomainStore
_services = importlib.import_module("services")
host_discovery = _services.host_discovery
profile_manager = _services.profile_manager

NOW = "2026-09-10T00:00:00+00:00"


class NoopRuntime:
    pass


class HostRegistrationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "host-root"
        self.root.mkdir()
        self.path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.path)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, endpoint, body=b"{}", method="POST", token: str | None = "write-secret", query_string=None):
        with patch.multiple(
            config,
            PORTABLE_STORE_PATH=self.path,
            HOST_AGENT_HOST_ID="host-a",
            HOST_DISCOVERY_ROOT=self.root,
            WEBAPP_WRITE_TOKEN="write-secret",
        ):
            app = create_app(runtime=NoopRuntime())
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            return app.test_client().open(
                endpoint,
                method=method,
                data=body,
                headers=headers,
                query_string=query_string,
            )

    def rows(self, query):
        store = PortableDomainStore.open(self.path)
        try:
            return store.connection.execute(query).fetchall()
        finally:
            store.close()

    def registration_body(self, host_id="host-a", display_name="Host A"):
        return json.dumps({"host_id": host_id, "display_name": display_name}).encode()

    def binding_body(self, host_id="host-a", profile_id="profile-a"):
        return json.dumps({"host_id": host_id, "profile_id": profile_id}).encode()

    def test_registration_requires_bearer_and_does_not_write_on_rejection(self):
        before = self.path.read_bytes()
        for token in (None, "wrong"):
            response = self.request("/up/api/host_registration", token=token)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json(), {"error": "write authentication required"})
            self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM hosts")[0][0], 0)

    def test_registration_uses_exact_configured_identity_and_is_repeatable(self):
        response = self.request(
            "/up/api/host_registration",
            body=self.registration_body(),
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["result"]["host_id"], "host-a")
        self.assertEqual(data["runtime_effect"], "NONE")
        self.assertEqual(data["runtime_authority"]["mode"], "LEGACY")
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        response = self.request(
            "/up/api/host_registration",
            body=self.registration_body(),
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["result"]["created"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM hosts")[0][0], 1)

    def test_rename_and_mismatched_request_identity_are_conflicts_without_write(self):
        self.request("/up/api/host_registration", body=self.registration_body())
        before = self.path.read_bytes()
        response = self.request(
            "/up/api/host_registration",
            body=self.registration_body(display_name="Renamed Host"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("runtime_effect", response.get_json())
        self.assertEqual(self.path.read_bytes(), before)
        response = self.request(
            "/up/api/host_registration",
            body=self.registration_body(host_id="other-host", display_name="Other"),
        )
        self.assertEqual(response.status_code, 409)
        self.assertNotIn("runtime_authority", response.get_json())
        self.assertEqual(self.path.read_bytes(), before)

    def test_binding_requires_exact_contract_and_forces_offline(self):
        self.request("/up/api/host_registration", body=self.registration_body())
        response = self.request(
            "/up/api/host_binding",
            body=self.binding_body(),
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["result"]["state"], "OFFLINE")
        self.assertEqual(data["result"]["binding_generation"], 1)
        self.assertEqual(data["runtime_effect"], "NONE")
        self.assertEqual(data["runtime_authority"]["mode"], "LEGACY")
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertFalse(data["controls"]["can_activate"])
        self.assertEqual(len(self.rows("SELECT 1 FROM profile_audit_events")), 1)

    def test_malformed_wrong_type_control_and_overlength_requests_fail_before_write(self):
        registration_values = (
            b"{}",
            b'{"host_id":"host-a"}',
            b'{"host_id":7,"display_name":"Host"}',
            b'{"host_id":"host-a","display_name":7}',
            b'{"host_id":"host-a","display_name":"Host","state":"ACTIVE"}',
            json.dumps({"host_id": "host-a", "display_name": "x" * 201}).encode(),
        )
        binding_values = (
            b"{}",
            b'{"host_id":"host-a"}',
            b'{"host_id":7,"profile_id":"profile-a"}',
            b'{"host_id":"host-a","profile_id":7}',
            b'{"host_id":"host-a","profile_id":"profile-a","state":"ACTIVE"}',
            json.dumps({"host_id": "host-a", "profile_id": "x" * 201}).encode(),
        )
        before = self.path.read_bytes()
        for body in registration_values:
            with self.subTest(endpoint="registration", body=body):
                response = self.request("/up/api/host_registration", body=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)
        for body in binding_values:
            with self.subTest(endpoint="binding", body=body):
                response = self.request("/up/api/host_binding", body=body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)

    def test_client_cannot_supply_server_owned_fields(self):
        before = self.path.read_bytes()
        forbidden = (
            {"host_id": "host-a", "display_name": "Host A", "origin_ref": "local"},
            {"host_id": "host-a", "display_name": "Host A", "account_ref": "account-a"},
            {"host_id": "host-a", "profile_id": "profile-a", "state": "ACTIVE"},
            {"host_id": "host-a", "profile_id": "profile-a", "binding_generation": 9},
        )
        for value in forbidden:
            with self.subTest(value=value):
                endpoint = "/up/api/host_binding" if "profile_id" in value else "/up/api/host_registration"
                response = self.request(endpoint, body=json.dumps(value).encode())
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)

    def test_query_and_non_post_are_rejected_before_store_write(self):
        before = self.path.read_bytes()
        response = self.request(
            "/up/api/host_registration",
            query_string={"host_id": "host-a"},
        )
        self.assertEqual(response.status_code, 400)
        for method in ("GET", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.request("/up/api/host_binding", method=method)
                self.assertEqual(response.status_code, 403)
        self.assertEqual(self.path.read_bytes(), before)

    def test_post_write_read_models_remain_legacy_authority(self):
        self.request("/up/api/host_registration", body=self.registration_body())
        self.request("/up/api/host_binding", body=self.binding_body())
        discovery_raw, discovery_status, _ = host_discovery.get_host_discovery(
            self.path, "host-a", self.root
        )
        profile_raw, profile_status, _ = profile_manager.get_profile_manager(self.path)
        self.assertEqual(discovery_status, 200)
        self.assertEqual(profile_status, 200)
        discovery = json.loads(discovery_raw)
        profile = json.loads(profile_raw)
        for data in (discovery, profile):
            self.assertEqual(data["runtime_authority"]["mode"], "LEGACY")
            self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertEqual(discovery["portable_binding"]["current_binding"]["state"], "OFFLINE")
        self.assertEqual(profile["profiles"][0]["binding"]["state"], "OFFLINE")

    def test_missing_configured_identity_fails_closed(self):
        with patch.multiple(
            config,
            PORTABLE_STORE_PATH=self.path,
            HOST_AGENT_HOST_ID="",
            WEBAPP_WRITE_TOKEN="write-secret",
        ):
            app = create_app(runtime=NoopRuntime())
            response = app.test_client().post(
                "/up/api/host_registration",
                data=self.registration_body(),
                content_type="application/json",
                headers={"Authorization": "Bearer write-secret"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "configured host identity unavailable"})


if __name__ == "__main__":
    unittest.main()
