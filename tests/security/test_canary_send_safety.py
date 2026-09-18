#!/usr/bin/env python3
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "webapp" / "backend" / "services"
TRANSPORT_FILE = SERVICES / "canary_send_transport.py"
SERVICE_FILE = SERVICES / "canary_send.py"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"), str(path))


def _imports(tree):
    found = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    found.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    return found


def _names(tree):
    return {
        node.name.lower()
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _urlopen_sites(tree):
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "open"
    ]


class CanarySendSafetyTests(unittest.TestCase):
    def test_exactly_one_network_module_with_one_call_site(self):
        transport_calls = _urlopen_sites(_tree(TRANSPORT_FILE))
        service_calls = _urlopen_sites(_tree(SERVICE_FILE))
        self.assertEqual(len(transport_calls), 1)
        self.assertEqual(len(service_calls), 0)

    def test_no_framework_or_concurrency_imports(self):
        forbidden = {
            "socket", "requests", "httpx", "websocket", "subprocess", "flask",
            "apscheduler", "threading", "queue", "concurrent", "multiprocessing",
        }
        for path in (TRANSPORT_FILE, SERVICE_FILE):
            self.assertTrue(
                forbidden.isdisjoint(_imports(_tree(path))), str(path)
            )

    def test_service_has_no_network_imports(self):
        imports = _imports(_tree(SERVICE_FILE))
        self.assertTrue({"urllib", "http"}.isdisjoint(imports))

    def test_transport_has_no_database_imports(self):
        imports = _imports(_tree(TRANSPORT_FILE))
        self.assertTrue({"repositories", "sqlite3"}.isdisjoint(imports))

    def test_no_retry_or_backoff_identifiers(self):
        for path in (TRANSPORT_FILE, SERVICE_FILE):
            names = _names(_tree(path))
            source = path.read_text(encoding="utf-8").lower()
            for token in ("retry", "retries", "backoff", "back_off"):
                self.assertNotIn(
                    token, names, f"{token} in {path.name}"
                )
                for line in source.splitlines():
                    stripped = line.strip()
                    if stripped.startswith(("#", '"""', "'''")):
                        continue
                    if token in stripped and "never" not in stripped:
                        self.fail(f"{path.name}:{stripped} contains {token!r}")

    def test_no_loop_or_sleep_in_transport(self):
        tree = _tree(TRANSPORT_FILE)
        loops = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.For, ast.While))
        ]
        self.assertEqual(loops, [])
        source = TRANSPORT_FILE.read_text(encoding="utf-8")
        self.assertNotIn("sleep", source)
        self.assertNotIn("time.", source)

    def test_send_states_exclude_failure_and_delivery(self):
        repository = (
            ROOT / "webapp" / "backend" / "repositories" / "operational_sqlite.py"
        ).read_text(encoding="utf-8")
        send_schema = repository.split("CANARY_SEND_INTENTS_TABLE_SQL =", 1)[1].split(
            "SCHEMA_SQL =", 1
        )[0]
        self.assertIn("'committed','succeeded','unknown'", send_schema.replace(" ", ""))
        for forbidden in ("'failed'", "'sent'", "'dispatched'", "'acknowledged'"):
            self.assertNotIn(forbidden, send_schema)

    def test_no_scheduler_dispatcher_or_flask_wiring(self):
        for path in (TRANSPORT_FILE, SERVICE_FILE):
            tree = _tree(path)
            docstrings = {
                ast.get_docstring(node) or ""
                for node in ast.walk(tree)
                if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            }
            source = path.read_text(encoding="utf-8").lower()
            for doc in docstrings:
                source = source.replace(doc.lower(), "")
            code_lines = [
                line for line in source.splitlines()
                if not line.strip().startswith("#")
            ]
            code = "\n".join(code_lines)
            for forbidden in (
                "scheduler", "autocycle", "dispatcher", "claim_job",
                "flask", "route(", "@app.",
            ):
                self.assertNotIn(
                    forbidden, code, f"{forbidden} in {path.name}"
                )


if __name__ == "__main__":
    unittest.main()
