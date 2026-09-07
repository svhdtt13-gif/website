#!/usr/bin/env python3
"""Security tests for the Wave1 SQLite stability contract addendum."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

from repositories.sqlite import SQLiteCandidateRepository  # noqa: E402
from services.sqlite_import import (  # noqa: E402
    SOURCE_SET_FULL,
    SOURCE_SET_WAVE1,
    WAVE1_RUNTIME_SOURCE_ORDER,
    UnstableSnapshotError,
    capture_stable_snapshot,
    import_candidate,
)
from services.sqlite_runtime import (  # noqa: E402
    GROUP_MASTER_DATABASE,
    SQLiteRuntimeCoordinator,
)
from test_sqlite_import import (  # noqa: E402
    FakeSource,
    fixture_values,
    replace_json,
    source_value,
)


class EndpointMutatingSource(FakeSource):
    def __init__(self, values, endpoint):
        super().__init__(values)
        self.endpoint = endpoint
        self.changed = False

    def fetch(self, endpoint):
        value = super().fetch(endpoint)
        if not self.changed and len(self.calls) > len(WAVE1_RUNTIME_SOURCE_ORDER):
            if endpoint == self.endpoint:
                payload = json.loads(value.body.decode("utf-8"))
                if endpoint == "api/master":
                    payload["clients"][0]["name"] += "-changed"
                elif endpoint == "client_database.json":
                    payload["clients"][0]["name"] += "-changed"
                else:
                    payload["tunnel_port"] += 1
                value = source_value(endpoint, payload, value.content_type)
                self.values[endpoint] = value
                self.changed = True
        return value


class HttpOnlyChangingSource(FakeSource):
    def __init__(self, values):
        super().__init__(values)
        self.changed = False

    def fetch(self, endpoint):
        value = super().fetch(endpoint)
        if not self.changed and len(self.calls) == len(WAVE1_RUNTIME_SOURCE_ORDER):
            replace_json(
                self.values,
                "api/cycle/status",
                lambda payload: payload.update({"checked_at": "changed"}),
            )
            self.changed = True
        return value


class SQLiteWave1AddendumTests(unittest.TestCase):
    def test_wave1_snapshot_fetches_only_three_sources(self):
        source = FakeSource(fixture_values())
        snapshot = capture_stable_snapshot(
            source,
            source_order=WAVE1_RUNTIME_SOURCE_ORDER,
            source_set=SOURCE_SET_WAVE1,
        )
        self.assertEqual(source.calls, list(WAVE1_RUNTIME_SOURCE_ORDER) * 2)
        self.assertEqual(tuple(snapshot.values), WAVE1_RUNTIME_SOURCE_ORDER)
        self.assertEqual(snapshot.source_order, WAVE1_RUNTIME_SOURCE_ORDER)
        self.assertEqual(snapshot.source_set, SOURCE_SET_WAVE1)
        self.assertNotIn("api/cycle/status", source.calls)
        self.assertNotIn("api/ai_fix/status", source.calls)

    def test_wave1_import_reconstructs_three_domains_and_stores_three_sources(self):
        snapshot = capture_stable_snapshot(
            FakeSource(fixture_values()),
            source_order=WAVE1_RUNTIME_SOURCE_ORDER,
            source_set=SOURCE_SET_WAVE1,
        )
        with tempfile.TemporaryDirectory() as directory:
            receipt = import_candidate(snapshot, Path(directory) / "candidate.sqlite3")
            self.assertEqual(receipt.status, "verified")
            self.assertEqual(receipt.source_set, SOURCE_SET_WAVE1)
            self.assertEqual(receipt.checks["source_set"], SOURCE_SET_WAVE1)
            self.assertEqual(receipt.checks["checks"]["source_set"], SOURCE_SET_WAVE1)
            repository = SQLiteCandidateRepository.open_existing(receipt.candidate_path)
            self.assertEqual(
                repository.rows(
                    "SELECT endpoint FROM source_snapshots WHERE run_id=? ORDER BY position",
                    (receipt.run_id,),
                ),
                [(endpoint,) for endpoint in WAVE1_RUNTIME_SOURCE_ORDER],
            )
            self.assertEqual(
                repository.rows(
                    "SELECT COUNT(*) FROM master_clients WHERE run_id=?",
                    (receipt.run_id,),
                )[0][0],
                2,
            )
            self.assertEqual(
                repository.rows(
                    "SELECT COUNT(*) FROM database_clients WHERE run_id=?",
                    (receipt.run_id,),
                )[0][0],
                2,
            )
            self.assertEqual(
                repository.rows(
                    "SELECT COUNT(*) FROM public_settings WHERE run_id=?",
                    (receipt.run_id,),
                )[0][0],
                1,
            )
            checks_json = repository.rows(
                "SELECT checks_json FROM import_runs WHERE run_id=?",
                (receipt.run_id,),
            )[0][0]
            self.assertEqual(json.loads(checks_json)["source_set"], SOURCE_SET_WAVE1)
            self.assertEqual(
                repository.rows(
                    "SELECT COUNT(*) FROM cycle_state WHERE run_id=?",
                    (receipt.run_id,),
                )[0][0],
                0,
            )
            repository.close()

    def test_wave1_source_changes_fail_stability_for_each_runtime_source(self):
        for endpoint in WAVE1_RUNTIME_SOURCE_ORDER:
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(UnstableSnapshotError):
                    capture_stable_snapshot(
                        EndpointMutatingSource(fixture_values(), endpoint),
                        max_passes=2,
                        source_order=WAVE1_RUNTIME_SOURCE_ORDER,
                        source_set=SOURCE_SET_WAVE1,
                    )

    def test_http_only_change_does_not_affect_wave1_stability(self):
        source = HttpOnlyChangingSource(fixture_values())
        snapshot = capture_stable_snapshot(
            source,
            max_passes=2,
            source_order=WAVE1_RUNTIME_SOURCE_ORDER,
            source_set=SOURCE_SET_WAVE1,
        )
        self.assertEqual(snapshot.source_set, SOURCE_SET_WAVE1)
        self.assertEqual(source.calls, list(WAVE1_RUNTIME_SOURCE_ORDER) * 2)

    def test_runtime_publishes_wave1_and_rejects_non_wave1_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = SQLiteRuntimeCoordinator(
                runtime_dir=directory,
                read_enabled=True,
                group_enabled={GROUP_MASTER_DATABASE: True},
                background_refresh=False,
                source_factory=lambda: FakeSource(fixture_values()),
            )
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            self.assertEqual(state["current"]["source_set"], SOURCE_SET_WAVE1)
            self.assertTrue(runtime._eligible(state, GROUP_MASTER_DATABASE))
            state["current"]["source_set"] = SOURCE_SET_FULL
            self.assertFalse(runtime._eligible(state, GROUP_MASTER_DATABASE))


if __name__ == "__main__":
    unittest.main()
