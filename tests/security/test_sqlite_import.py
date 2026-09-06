#!/usr/bin/env python3
"""Disposable tests for the Phase 3 candidate importer and SQLite store."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.sqlite import CandidatePathError, SQLiteCandidateRepository  # noqa: E402
import services.sqlite_import as sqlite_import  # noqa: E402
from services.sqlite_import import (  # noqa: E402
    AiToolHttpSource,
    FidelityError,
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
            "meta": {"source": "test"},
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
            "models": ["gpt-luna-5.5"],
            "watcher": {"auto": False},
            "pending": [{"file": "ai_fix_cycle_20260906_100000.json", "kind": "cycle",
                          "time": "2026-09-06T10:00:00", "summary": "", "model": "", "answer": ""}],
            "recent_done": [{"file": "ai_fix_web_20260906_095900.done.json", "kind": "web",
                             "time": "2026-09-06T09:59:00", "summary": "ok", "model": "gpt", "answer": "done"}],
            "recent_failed": [{"file": "ai_fix_userimport_20260906_095800.failed.json", "kind": "userimport",
                                "time": "2026-09-06T09:58:00", "summary": "failed", "model": "", "answer": ""}],
        }),
        "api/cycle/backup": source_value("api/cycle/backup", {
            "backups": [{"name": "cycle_20260906.zip", "size": 12,
                          "mtime": "2026-09-06T10:00:00", "label": "test",
                          "created_at": "2026-09-06T10:00:00",
                          "files": [{"path": "cache/cycle.log", "sha256": "a", "size": 1}],
                          "script_files": [{"path": "AutoCycle.ps1", "sha256": "b", "size": 2}]}],
        }),
        "api/status": source_value("api/status", {
            "clients": 2, "lastUpdated": "2026-09-06", "time": "10:00:00",
        }),
        "api/sync_status": source_value("api/sync_status", {
            "continuous_running": True, "continuous_pid": 123,
            "interval_sec": 10800, "status_interval_sec": 20,
            "last_sync": "2026-09-06 10:00:00", "extraction_method": "test",
            "total_clients": 2, "source": "test",
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


class StubRepository:
    def __init__(self, values):
        self.values = values
        self.calls = []

    def get(self, endpoint):
        self.calls.append(endpoint)
        value = self.values[endpoint]
        return value.body, 200, value.content_type


def replace_json(values, endpoint, mutate):
    payload = json.loads(values[endpoint].body.decode("utf-8"))
    mutate(payload)
    values[endpoint] = source_value(endpoint, payload, values[endpoint].content_type)


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

    def test_http_source_covers_matrix_and_settings_boundary(self):
        values = fixture_values()
        repository = StubRepository(values)
        with patch.object(sqlite_import.settings_service, "get_settings",
                          return_value=(values["api/settings"].body, 200, "application/json")):
            source = AiToolHttpSource(repository=repository)
            fetched = {endpoint: source.fetch(endpoint) for endpoint in SOURCE_ORDER}
        self.assertEqual(repository.calls, [endpoint for endpoint in SOURCE_ORDER if endpoint != "api/settings"])
        self.assertEqual(fetched["api/settings"].body, values["api/settings"].body)
        self.assertEqual(tuple(fetched), SOURCE_ORDER)
        with self.assertRaises(Exception):
            source.fetch("cache/not-allowlisted.txt")

    def _assert_rejected(self, mutate):
        values = fixture_values()
        mutate(values)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FidelityError):
                import_candidate(capture_stable_snapshot(FakeSource(values)),
                                 Path(directory) / "candidate.sqlite3")

    def test_missing_master_selected_is_rejected(self):
        self._assert_rejected(lambda values: replace_json(
            values, "api/master", lambda payload: payload["clients"][0].pop("selected")))

    def test_orphan_database_client_is_rejected(self):
        self._assert_rejected(lambda values: replace_json(
            values, "client_database.json", lambda payload: payload["clients"][0].update({"client": "orphan"})))

    def test_missing_cycle_state_is_rejected(self):
        self._assert_rejected(lambda values: replace_json(
            values, "api/cycle/status", lambda payload: payload.pop("state")))

    def test_invalid_cycle_ledger_value_is_rejected(self):
        self._assert_rejected(lambda values: replace_json(
            values, "api/cycle/status", lambda payload: payload["state"]["done"].update({"04:00|HAMI": 1})))

    def test_unsafe_ai_filename_is_rejected(self):
        self._assert_rejected(lambda values: replace_json(
            values, "api/ai_fix/status", lambda payload: payload["pending"][0].update({"file": "..\\secret.json"})))

    def test_candidate_import_preserves_all_domains(self):
        snapshot = capture_stable_snapshot(FakeSource(fixture_values()))
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            receipt = import_candidate(snapshot, candidate)
            self.assertEqual(receipt.status, "verified")
            self.assertTrue(receipt.checks["ok"])
            checks = receipt.checks["checks"]
            for key in (
                "master_order_and_projection", "database_order_and_projection",
                "settings_redacted_projection", "cycle_state_projection",
                "cycle_slot_ledger", "manual_overrides", "ai_fix_rows_and_order",
                "backup_metadata_and_order", "api/status_observation",
                "api/sync_status_observation", "activity_raw_bytes",
                "change_raw_bytes", "action_raw_bytes",
            ):
                self.assertTrue(checks[key], key)

            repository = SQLiteCandidateRepository.open_existing(candidate)
            self.assertEqual(repository.rows("PRAGMA journal_mode")[0][0].lower(), "wal")
            self.assertEqual(repository.rows("PRAGMA foreign_keys")[0][0], 1)
            self.assertIn("master_clients", {
                row[2] for row in repository.rows("PRAGMA foreign_key_list(database_clients)")
            })
            self.assertEqual(
                repository.rows("SELECT client_id FROM master_clients WHERE run_id=? ORDER BY position",
                                (receipt.run_id,)),
                [("client_2",), ("client_1",)],
            )
            repository.close()

    def test_shadow_mismatch_cannot_be_verified_for_each_domain(self):
        mutations = {
            "cycle": lambda repo, run_id: repo.connection.execute(
                "UPDATE cycle_state SET state_json='{}' WHERE run_id=?", (run_id,)),
            "overrides": lambda repo, run_id: repo.connection.execute(
                "UPDATE manual_overrides SET raw_json='{}' WHERE run_id=?", (run_id,)),
            "ai": lambda repo, run_id: repo.connection.execute(
                "UPDATE ai_fix_requests SET raw_json='{}' WHERE run_id=?", (run_id,)),
            "backup": lambda repo, run_id: repo.connection.execute(
                "UPDATE backup_metadata SET raw_json='{}' WHERE run_id=?", (run_id,)),
            "status": lambda repo, run_id: repo.connection.execute(
                "UPDATE source_observations SET payload_json='{}' WHERE run_id=? AND endpoint='api/status'", (run_id,)),
        }
        for name, mutate in mutations.items():
            with self.subTest(domain=name), tempfile.TemporaryDirectory() as directory:
                snapshot = capture_stable_snapshot(FakeSource(fixture_values()))
                original = sqlite_import.shadow_verify

                def corrupt_and_verify(repo, run_id, current_snapshot, mutate=mutate, original=original):
                    mutate(repo, run_id)
                    return original(repo, run_id, current_snapshot)

                with patch.object(sqlite_import, "shadow_verify", side_effect=corrupt_and_verify):
                    receipt = import_candidate(snapshot, Path(directory) / "candidate.sqlite3")
                self.assertEqual(receipt.status, "failed")
                self.assertFalse(receipt.checks["ok"])

    def test_candidate_path_must_be_new(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.sqlite3"
            candidate.write_bytes(b"existing")
            with self.assertRaises(CandidatePathError):
                SQLiteCandidateRepository.create(candidate)


if __name__ == "__main__":
    unittest.main()
