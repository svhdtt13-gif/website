#!/usr/bin/env python3
"""Disposable tests for the Phase 3 candidate importer and SQLite store."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.sqlite import CandidatePathError, SQLiteCandidateRepository  # noqa: E402
from services.sqlite_import import (  # noqa: E402
    AiToolHttpSource,
    SOURCE_ORDER,
    SourceValue,
    UnstableSnapshotError,
    _canonical_bytes,
    capture_stable_snapshot,
    import_candidate,
)


def json_body(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def source_value(endpoint, body, content_type="application/json"):
    if not isinstance(body, bytes):
        body = json_body(body)
    return SourceValue(
        endpoint=endpoint,
        body=body,
        content_type=content_type,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        canonical_sha256=hashlib.sha256(_canonical_bytes(endpoint, body)).hexdigest(),
    )


def fixture_values():
    activity = b'{"event":"one"}\r\n\nnot-json\n'
    change = b'{"event":"change","n":1}\n{"event":"change","n":1}\n'
    action = b"[2026-09-06 10:00:00] action one\r\n"
    return {
        "api/master": source_value("api/master", {
            "clients": [
                {"client": "client_2", "name": "Second", "remote_name": "R2",
                 "group": "fixed", "selected": True, "status": "offline"},
                {"client": "client_1", "name": "First", "remote_name": "R1",
                 "group": "HAMI", "selected": False, "status": "running"},
            ],
            "schedule": [{"group": "HAMI", "time": "04:00", "close": "08:00"}],
        }),
        "client_database.json": source_value("client_database.json", {
            "lastUpdated": "2026-09-06",
            "clients": [
                {"idx": 9, "client": "client_2", "name": "Second", "status": "offline",
                 "group": "fixed", "selected": True},
                {"idx": 3, "client": "client_1", "name": "First", "status": "running",
                 "group": "HAMI", "selected": False},
            ],
            "schedule": [{"time": "04:00", "group": "HAMI", "open": ["First"],
                           "close": ["NA"], "closeAt": "08:00", "closeGroup": "NA",
                           "closeTime": "04:00"}],
        }),
        "api/settings": source_value("api/settings", {
            "tunnel_port": 8080, "auto_restart_tunnel": True,
            "auto_telegram": False, "auto_open_browser": True,
        }),
        "api/cycle/status": source_value("api/cycle/status", {
            "checked_at": "2026-09-06T10:00:00+00:00", "cycle_running": True,
            "state": {"today": "2026-09-06", "done": {"04:00|HAMI": "04:01:29"}},
            "manual_overrides": [{"client": "client_1", "until": "2026-09-06T10:15:00+00:00",
                                   "detected_at": "2026-09-06T10:00:00+00:00",
                                   "change": "offline->running"}],
        }),
        "cache/activity_history.jsonl": source_value("cache/activity_history.jsonl", activity, "application/x-ndjson"),
        "cache/change_log.jsonl": source_value("cache/change_log.jsonl", change, "application/x-ndjson"),
        "cache/action.log": source_value("cache/action.log", action, "text/plain"),
        "api/ai_fix/status": source_value("api/ai_fix/status", {
            "pending": [{"file": "ai_fix_cycle_20260906_100000.json", "kind": "cycle"}],
            "recent_done": [{"file": "ai_fix_web_20260906_095900.json", "kind": "web",
                             "result": {"summary": "ok"}}],
            "recent_failed": [{"file": "ai_fix_userimport_20260906_095800.json", "kind": "userimport"}],
        }),
        "api/cycle/backup": source_value("api/cycle/backup", {
            "backups": [{"name": "cycle_20260906.zip", "size": 12,
                          "mtime": "2026-09-06T10:00:00", "label": "test"}],
        }),
        "api/status": source_value("api/status", {
            "clients": 2, "lastUpdated": "2026-09-06", "time": "10:00:00",
        }),
        "api/sync_status": source_value("api/sync_status", {
            "continuous_running": True, "continuous_pid": 123,
            "interval_sec": 10800, "status_interval_sec": 20,
        }),
    }


class FakeSource:
    def __init__(self, values, mutate_after=None):
        self.values = values
        self.calls = []
        self.mutate_after = mutate_after

    def fetch(self, endpoint):
        self.calls.append(endpoint)
        value = self.values[endpoint]
        if self.mutate_after and len(self.calls) > self.mutate_after and endpoint == "api/master":
            payload = json.loads(value.body.decode("utf-8"))
            payload["clients"][0]["name"] = "changed"
            return source_value(endpoint, payload)
        return value


class SQLiteImportTests(unittest.TestCase):
    def test_stable_snapshot_uses_fixed_order_and_two_passes(self):
        source = FakeSource(fixture_values())
        snapshot = capture_stable_snapshot(source)
        self.assertEqual(tuple(snapshot.values), SOURCE_ORDER)
        self.assertEqual(tuple(source.calls[:len(SOURCE_ORDER)]), SOURCE_ORDER)
        self.assertEqual(len(source.calls), len(SOURCE_ORDER) * 2)

    def test_unstable_snapshot_fails_closed(self):
        source = FakeSource(fixture_values(), mutate_after=len(SOURCE_ORDER))
        with self.assertRaises(UnstableSnapshotError):
            capture_stable_snapshot(source, max_passes=2)

    def test_candidate_import_preserves_fidelity_and_redacts_settings(self):
        snapshot = capture_stable_snapshot(FakeSource(fixture_values()))
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            receipt = import_candidate(snapshot, candidate)
            self.assertEqual(receipt.status, "verified")
            self.assertTrue(receipt.checks["ok"])
            checks = receipt.checks["checks"]
            self.assertTrue(checks["master_order_and_projection"])
            self.assertTrue(checks["database_order_and_projection"])
            self.assertTrue(checks["settings_redacted_projection"])
            self.assertTrue(checks["activity_raw_bytes"])
            self.assertTrue(checks["change_raw_bytes"])
            self.assertTrue(checks["action_raw_bytes"])
            self.assertTrue(checks["no_settings_secret_columns"])

            repository = SQLiteCandidateRepository.open_existing(candidate)
            self.assertEqual(repository.rows("PRAGMA journal_mode")[0][0].lower(), "wal")
            self.assertEqual(repository.rows("PRAGMA foreign_keys")[0][0], 1)
            self.assertEqual(
                repository.rows("SELECT client_id FROM master_clients WHERE run_id=? ORDER BY position",
                                (receipt.run_id,)),
                [("client_2",), ("client_1",)],
            )
            settings_snapshot = repository.rows(
                "SELECT raw_bytes FROM source_snapshots WHERE run_id=? AND endpoint='api/settings'",
                (receipt.run_id,),
            )[0][0]
            self.assertNotIn(b"telegram_bot_token", settings_snapshot)
            self.assertNotIn(b"cloudflared_path", settings_snapshot)
            repository.close()

    def test_candidate_path_must_be_new(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            candidate.write_bytes(b"existing")
            with self.assertRaises(CandidatePathError):
                SQLiteCandidateRepository.create(candidate)

    def test_source_client_rejects_unknown_endpoint(self):
        with self.assertRaises(Exception):
            AiToolHttpSource(repository=object()).fetch("cache/secret.txt")


if __name__ == "__main__":
    unittest.main()
