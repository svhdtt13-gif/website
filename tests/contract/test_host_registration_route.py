#!/usr/bin/env python3
"""Route regressions for guarded Portable Domain Store configuration writes."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

import config  # noqa: E402
from app import create_app  # noqa: E402
from repositories.portable_store import PortableDomainStore  # noqa: E402


NOW = "2026-09-10T00:00:00+00:00"


class NoopRuntime:
    pass


class HostRegistrationRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.path)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, endpoint, body=b"{}", method="POST", token="write-secret", query_string=None):
        with patch.multiple(
            config,
            PORTABLE_STORE_PATH=self.path,
            HOST_AGENT_HOST_ID="host-a",
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

    def test_registration_requires_bearer_and_does_not_write_on_rejection(self):
        before = self.path.read_bytes()
        for token in (None, "wrong"):
            response = self.request("/up/api/host_registration", token=token)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json(), {"error": "write authentication required"})
            self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
            self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM hosts")[0][0], 0)

    def test_registration_uses_configured_identity_and_is_repeatable(self):
        response = self.request(
            "/up/api/host_registration",
            body=b'{"display_name":"Host A"}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["result"]["host_id"], "host-a")
        response = self.request(
            "/up/api/host_registration",
            body=b'{"display_name":"Host A"}',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["result"]["created"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM hosts")[0][0], 1)

    def test_binding_requires_only_profile_id_and_forces_offline(self):
        self.request("/up/api/host_registration", body=b"{}")
        response = self.request(
            "/up/api/host_binding",
            body=b'{"profile_id":"profile-a"}',
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["result"]["state"], "OFFLINE")
        self.assertEqual(data["result"]["binding_generation"], 1)
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertFalse(data["controls"]["can_activate"])

    def test_client_cannot_supply_host_state_generation_or_references(self):
        self.request("/up/api/host_registration", body=b"{}")
        before = self.path.read_bytes()
        forbidden = (
            {"host_id": "spoofed", "display_name": "Host A"},
            {"profile_id": "profile-a", "state": "ACTIVE"},
            {"profile_id": "profile-a", "binding_generation": 9},
            {"profile_id": "profile-a", "origin_ref": "local"},
            {"profile_id": "profile-a", "account_ref": "account-a"},
        )
        for value in forbidden:
            with self.subTest(value=value):
                response = self.request(
                    "/up/api/host_binding",
                    body=json.dumps(value).encode(),
                )
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
                data=b"{}",
                content_type="application/json",
                headers={"Authorization": "Bearer write-secret"},
            )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "configured host identity unavailable"})


if __name__ == "__main__":
    unittest.main()
