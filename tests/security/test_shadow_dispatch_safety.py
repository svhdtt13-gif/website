#!/usr/bin/env python3
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "webapp" / "backend"
SHADOW_FILES = (
    BACKEND / "services" / "shadow_dispatch.py",
    BACKEND / "services" / "shadow_dispatch_envelope.py",
    BACKEND / "services" / "shadow_dispatch_transport.py",
)


class ShadowDispatchSafetyTests(unittest.TestCase):
    def test_shadow_modules_have_no_network_or_scheduler_imports(self):
        forbidden_modules = {
            "socket",
            "requests",
            "httpx",
            "websocket",
            "apscheduler",
            "flask",
            "subprocess",
        }
        for path in SHADOW_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            imports = {
                node.names[0].name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import) and node.names
            }
            imports.update(
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            )
            self.assertTrue(
                forbidden_modules.isdisjoint(imports),
                f"forbidden integration import in {path}: {imports & forbidden_modules}",
            )

    def test_transport_module_exposes_only_null_and_recording_transports(self):
        source = (BACKEND / "services" / "shadow_dispatch_transport.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("send", source.lower())
        self.assertNotIn("dispatch", source.lower())
        self.assertIn("class NullShadowTransport", source)
        self.assertIn("class RecordingShadowTransport", source)

    def test_shadow_dispatch_does_not_write_legacy_jobs_or_use_active_dispatch(self):
        source = (BACKEND / "services" / "shadow_dispatch.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("insert_job", source)
        self.assertNotIn("UPDATE jobs", source)
        self.assertNotIn("ACTIVE", source)
        self.assertNotIn("takeover", source.lower())


if __name__ == "__main__":
    unittest.main()
