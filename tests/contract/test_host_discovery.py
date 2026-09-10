#!/usr/bin/env python3
"""Contract checks for the read-only Host Agent Discovery Foundation."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.portable_store import PortableDomainStore  # noqa: E402
from services.host_discovery import PREREQUISITE_ALLOWLIST, get_host_discovery  # noqa: E402


NOW = "2026-09-09T00:00:00+00:00"


class HostDiscoveryTests(unittest.TestCase):
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
        (self.root / "tools" / "cache").mkdir(parents=True)
        (self.root / "tools" / "AutoCycle.ps1").write_text("# fixture\n", encoding="utf-8")
        (self.root / "continuous_sync_remote.ps1").write_text("# fixture\n", encoding="utf-8")
        (self.root / "tools" / "cache" / "cycle_state.json").write_text("{}\n", encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_identity_binding_and_observation_are_separate(self):
        before = self.store_path.read_bytes()
        raw, status, _ = get_host_discovery(self.store_path, "host-a", self.root)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["configured_host_identity"], {
            "host_id": "host-a", "configured": True, "source": "explicit_config"
        })
        self.assertEqual(data["portable_binding"]["current_binding"]["binding_id"], "binding-a")
        self.assertEqual(data["local_runtime_observation"]["mode"], "OBSERVATION_ONLY")
        self.assertEqual(data["local_runtime_observation"]["process_observation"], "NOT_PERFORMED")
        self.assertIsNone(data["local_runtime_observation"]["runtime_owner"])
        self.assertEqual(data["runtime_authority"]["mode"], "LEGACY")
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertFalse(data["controls"]["can_start"])
        self.assertEqual(self.store_path.read_bytes(), before)
        names = {item["name"] for item in data["local_runtime_observation"]["prerequisites"]}
        self.assertEqual(names, {name for name, _ in PREREQUISITE_ALLOWLIST})

    def test_missing_host_id_does_not_infer_or_create_host_binding(self):
        raw, status, _ = get_host_discovery(self.store_path, "", self.root)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertFalse(data["configured_host_identity"]["configured"])
        self.assertIsNone(data["configured_host_identity"]["host_id"])
        self.assertEqual(data["portable_binding"]["status"], "UNBOUND")
        store = PortableDomainStore.open(self.store_path)
        try:
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM hosts").fetchone()[0], 1)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM host_profile_bindings").fetchone()[0], 1)
        finally:
            store.close()

    def test_missing_store_fails_closed(self):
        raw, status, _ = get_host_discovery(Path(self.temp.name) / "missing.sqlite3", "host-a", self.root)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "portable profile store unavailable"})


if __name__ == "__main__":
    unittest.main()
