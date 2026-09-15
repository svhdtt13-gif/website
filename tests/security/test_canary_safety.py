#!/usr/bin/env python3
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "webapp" / "backend" / "services"
CANARY_FILES = (
    SERVICES / "canary_arming.py",
    SERVICES / "canary_envelope.py",
    SERVICES / "canary_transport.py",
)


class CanarySafetyTests(unittest.TestCase):
    def test_canary_modules_have_no_network_process_or_runtime_integration(self):
        forbidden_modules = {
            "socket", "requests", "httpx", "websocket", "subprocess", "flask",
            "apscheduler",
        }
        forbidden_names = {"send", "dispatch", "connect", "request", "post", "put"}
        for path in CANARY_FILES:
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            imports = {
                alias.name.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            }
            imports.update(
                node.module.split(".")[0]
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) and node.module
            )
            names = {
                node.name.lower()
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            self.assertTrue(forbidden_modules.isdisjoint(imports))
            self.assertTrue(forbidden_names.isdisjoint(names))

    def test_canary_schema_and_service_expose_no_delivery_or_retry_state(self):
        source = "\n".join(path.read_text(encoding="utf-8") for path in CANARY_FILES)
        repository = (
            ROOT / "webapp" / "backend" / "repositories" / "operational_sqlite.py"
        ).read_text(encoding="utf-8")
        canary_schema = repository.split("CANARY_CANDIDATES_TABLE_SQL =", 1)[1].split(
            "SCHEMA_SQL =", 1
        )[0]
        for forbidden in ("sent", "dispatched", "acknowledged", "retry"):
            self.assertNotIn(forbidden, source.lower())
            self.assertNotIn("'" + forbidden + "'", canary_schema.lower())


if __name__ == "__main__":
    unittest.main()
