#!/usr/bin/env python3
"""Route-level regressions for Host Agent Discovery read-only boundary."""
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


NOW = "2026-09-09T00:00:00+00:00"


class NoopRuntime:
    pass


class HostDiscoveryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "host-root"
        self.root.mkdir()
        self.store_path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.store_path)
        store.add_host("host-a", "Host A", "explicit-host-a", NOW)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.bind_profile("binding-a", "host-a", "profile-a", "account-a", 1, "ACTIVE", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, host_id="host-a", method="GET", query_string=None, store_path=None):
        with patch.multiple(
            config,
            PORTABLE_STORE_PATH=store_path or self.store_path,
            HOST_AGENT_HOST_ID=host_id,
            HOST_DISCOVERY_ROOT=self.root,
        ):
            app = create_app(runtime=NoopRuntime())
            return app.test_client().open(
                "/up/api/host_discovery",
                method=method,
                query_string=query_string,
            )

    def test_get_success_returns_three_separate_layers(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["configured_host_identity"]["host_id"], "host-a")
        self.assertEqual(data["portable_binding"]["current_binding"]["profile_id"], "profile-a")
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertIsNone(data["local_runtime_observation"]["runtime_owner"])
        self.assertNotIn("origin_ref", data["portable_binding"]["host"])
        self.assertNotIn("account_ref", data["portable_binding"]["current_binding"])

    def test_query_and_non_get_are_rejected(self):
        response = self.request(query_string={"host_id": "host-a"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "host discovery does not accept query parameters"})
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.request(method=method)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json(), {"error": "host discovery is read-only"})

    def test_missing_explicit_identity_fails_closed(self):
        response = self.request(host_id="")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "configured host identity unavailable"})

    def test_unknown_explicit_identity_is_unbound_without_creation(self):
        response = self.request(host_id="unknown-host")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["configured_host_identity"]["host_id"], "unknown-host")
        self.assertEqual(data["portable_binding"]["status"], "UNBOUND")
        self.assertEqual(data["portable_binding"]["host_lookup"], "UNKNOWN")

    def test_malformed_store_is_generic_503(self):
        malformed = Path(self.temp.name) / "malformed.sqlite3"
        malformed.write_bytes(b"not sqlite")
        response = self.request(store_path=malformed)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "portable profile store unavailable"})


if __name__ == "__main__":
    unittest.main()
