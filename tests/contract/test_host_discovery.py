#!/usr/bin/env python3
"""Contract checks for the read-only Host Agent Discovery Foundation."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.portable_store import PortableDomainStore  # noqa: E402
from services import host_discovery  # noqa: E402
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
        fixture_before = {
            path: path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file()
        }
        raw, status, _ = get_host_discovery(self.store_path, "host-a", self.root)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["configured_host_identity"], {
            "host_id": "host-a", "configured": True, "source": "explicit_config"
        })
        self.assertEqual(data["portable_binding"]["current_binding"]["binding_id"], "binding-a")
        self.assertEqual(data["portable_binding"]["host_lookup"], "FOUND")
        self.assertNotIn("origin_ref", data["portable_binding"]["host"])
        self.assertNotIn("account_ref", data["portable_binding"]["current_binding"])
        self.assertEqual(data["local_runtime_observation"]["mode"], "OBSERVATION_ONLY")
        self.assertEqual(data["local_runtime_observation"]["process_observation"], "NOT_PERFORMED")
        self.assertIsNone(data["local_runtime_observation"]["runtime_owner"])
        self.assertEqual(data["runtime_authority"]["mode"], "LEGACY")
        self.assertIsNone(data["runtime_authority"]["active_runtime_owner"])
        self.assertTrue(all(value is False for key, value in data["controls"].items() if key.startswith("can_")))
        self.assertEqual(self.store_path.read_bytes(), before)
        self.assertEqual(
            fixture_before,
            {
                path: path.read_bytes()
                for path in self.root.rglob("*")
                if path.is_file()
            },
        )
        prerequisites = {
            item["name"]: item for item in data["local_runtime_observation"]["prerequisites"]
        }
        names = set(prerequisites)
        self.assertEqual(names, {name for name, _ in PREREQUISITE_ALLOWLIST})
        self.assertEqual(prerequisites["autocycle_script"]["state"], "PRESENT")
        self.assertEqual(prerequisites["remote_sync_script"]["state"], "PRESENT")
        self.assertEqual(prerequisites["cycle_state"]["state"], "PRESENT")
        self.assertEqual(prerequisites["client_database"]["state"], "ABSENT")
        self.assertEqual(prerequisites["cycle_stopped_flag"]["state"], "ABSENT")

    def test_missing_host_id_fails_closed(self):
        raw, status, _ = get_host_discovery(self.store_path, "", self.root)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "configured host identity unavailable"})

    def test_unknown_explicit_host_is_unbound_without_writes(self):
        before = self.store_path.read_bytes()
        raw, status, _ = get_host_discovery(self.store_path, "unknown-host", self.root)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        self.assertEqual(data["configured_host_identity"]["host_id"], "unknown-host")
        self.assertEqual(data["portable_binding"]["status"], "UNBOUND")
        self.assertEqual(data["portable_binding"]["host_lookup"], "UNKNOWN")
        self.assertEqual(data["portable_binding"]["bindings"], [])
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_probe_error_is_unknown_without_existence_inference(self):
        real_stat = Path.stat

        def fail_cycle_state(path, *args, **kwargs):
            if str(path).endswith("cycle_state.json"):
                raise OSError("probe failed")
            return real_stat(path, *args, **kwargs)

        with patch.object(host_discovery.Path, "stat", new=fail_cycle_state):
            raw, status, _ = get_host_discovery(self.store_path, "host-a", self.root)
        data = json.loads(raw)
        self.assertEqual(status, 200)
        prerequisite = next(
            item for item in data["local_runtime_observation"]["prerequisites"]
            if item["name"] == "cycle_state"
        )
        self.assertEqual(prerequisite["state"], "UNKNOWN")
        self.assertEqual(prerequisite["error"], "probe_error")

    def test_allowlist_cannot_escape_configured_root(self):
        before = self.store_path.read_bytes()
        with patch.object(host_discovery, "PREREQUISITE_ALLOWLIST", (("escape", "../outside"),)):
            raw, status, _ = get_host_discovery(self.store_path, "host-a", self.root)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "host prerequisite allowlist escaped root"})
        self.assertEqual(self.store_path.read_bytes(), before)

    def test_missing_store_fails_closed(self):
        raw, status, _ = get_host_discovery(Path(self.temp.name) / "missing.sqlite3", "host-a", self.root)
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(raw), {"error": "portable profile store unavailable"})


if __name__ == "__main__":
    unittest.main()
