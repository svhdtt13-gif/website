#!/usr/bin/env python3
"""Route-level regressions for the read-only Profile Manager boundary."""
import sqlite3
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


class ProfileManagerRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.valid_path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.valid_path)
        store.add_host("host-a", "Host A", "explicit-host-a", NOW)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.bind_profile("binding-a", "host-a", "profile-a", "account-a", 1, "ACTIVE", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path, method="GET", query_string=None):
        with patch.object(config, "PORTABLE_STORE_PATH", path):
            app = create_app(runtime=NoopRuntime())
            return app.test_client().open(
                "/up/api/profile_manager",
                method=method,
                query_string=query_string,
            )

    def assert_unavailable(self, path):
        response = self.request(path)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {"error": "portable profile store unavailable"})

    def test_get_success_projects_profile_state(self):
        response = self.request(self.valid_path)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["read_only"])
        self.assertEqual(response.get_json()["profiles"][0]["profile_id"], "profile-a")

    def test_query_and_non_get_methods_are_rejected_at_route(self):
        response = self.request(self.valid_path, query_string={"profile_id": "profile-a"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json(), {"error": "profile manager does not accept query parameters"})
        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS"):
            with self.subTest(method=method):
                response = self.request(self.valid_path, method=method)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.get_json(), {"error": "profile manager is read-only"})

    def test_malformed_wrong_kind_and_future_schema_fail_closed(self):
        malformed = Path(self.temp.name) / "malformed.sqlite3"
        malformed.write_bytes(b"not sqlite")
        self.assert_unavailable(malformed)

        wrong_kind = Path(self.temp.name) / "wrong-kind.sqlite3"
        source = PortableDomainStore.create(wrong_kind)
        source.close()
        connection = sqlite3.connect(wrong_kind)
        connection.execute("UPDATE schema_meta SET value='wrong_kind' WHERE key='store_kind'")
        connection.commit()
        connection.close()
        self.assert_unavailable(wrong_kind)

        future = Path(self.temp.name) / "future.sqlite3"
        source = PortableDomainStore.create(future)
        source.close()
        connection = sqlite3.connect(future)
        connection.execute("UPDATE schema_meta SET value='999' WHERE key='schema_version'")
        connection.commit()
        connection.close()
        self.assert_unavailable(future)


if __name__ == "__main__":
    unittest.main()
