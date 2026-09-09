#!/usr/bin/env python3
"""Contract checks for the read-only Remote Profile Manager foundation."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.portable_store import PortableDomainStore  # noqa: E402
from services.profile_manager import get_profile_manager  # noqa: E402


NOW = "2026-09-09T00:00:00+00:00"


class ProfileManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.path)
        store.add_host("host-a", "Host A", "explicit-host-a", NOW)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.bind_profile("binding-a", "host-a", "profile-a", "account-a", 1, "ACTIVE", NOW)
        store.add_host("host-b", "Host B", "explicit-host-b", NOW)
        store.add_profile("profile-b", "Profile B", "account-b", "OFFLINE", NOW)
        store.bind_profile("binding-b", "host-b", "profile-b", "account-b", 1, "OFFLINE", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def test_projection_lists_profiles_and_separates_runtime_authority(self):
        raw, status, content_type = get_profile_manager(self.path)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        self.assertTrue(data["read_only"])
        self.assertEqual(data["viewed_profile_id"], None)
        self.assertEqual(data["runtime_authority"]["mode"], "LEGACY")
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertFalse(data["controls"]["can_switch"])
        self.assertEqual([item["profile_id"] for item in data["profiles"]], ["profile-a", "profile-b"])
        self.assertEqual(data["profiles"][0]["binding"]["binding_id"], "binding-a")
        self.assertEqual(data["active_bindings"][0]["profile_id"], "profile-a")

    def test_profile_statuses_are_exposed_without_inventing_runtime_owner(self):
        raw, _, _ = get_profile_manager(self.path)
        profiles = {item["profile_id"]: item for item in json.loads(raw)["profiles"]}
        self.assertEqual(profiles["profile-a"]["status"], "VERIFIED")
        self.assertEqual(profiles["profile-b"]["status"], "OFFLINE")
        self.assertIsNone(json.loads(raw)["runtime_authority"]["active_runtime_owner"])

    def test_missing_store_returns_generic_unavailable_without_traceback(self):
        raw, status, _ = get_profile_manager(Path(self.temp.name) / "missing.sqlite3")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "portable profile store unavailable"})

    def test_reader_does_not_modify_store(self):
        before = self.path.read_bytes()
        get_profile_manager(self.path)
        self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
