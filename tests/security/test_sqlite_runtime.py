#!/usr/bin/env python3
"""Acceptance tests for guarded SQLite generation runtime behavior."""
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

from repositories.aitool import UpstreamError  # noqa: E402
from services.sqlite_runtime import (  # noqa: E402
    GROUP_MASTER_DATABASE,
    GROUP_PUBLIC_SETTINGS,
    MUTEX_NAME,
    NamedGenerationMutex,
    SQLiteRuntimeCoordinator,
)
from test_sqlite_import import FakeSource, fixture_values  # noqa: E402


def enabled_runtime(directory, background_refresh=False, mutex_name=MUTEX_NAME):
    return SQLiteRuntimeCoordinator(
        runtime_dir=directory,
        read_enabled=True,
        group_enabled={
            GROUP_MASTER_DATABASE: True,
            GROUP_PUBLIC_SETTINGS: True,
        },
        background_refresh=background_refresh,
        mutex_name=mutex_name,
        source_factory=lambda: FakeSource(fixture_values()),
    )


def mutex_worker(name, entered, release):
    with NamedGenerationMutex(name, timeout_seconds=5).hold():
        entered.put(os.getpid())
        release.wait(5)


class SQLiteRuntimeTests(unittest.TestCase):
    def test_verified_generation_preserves_route_shapes_and_meta(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            master = runtime.read(
                "api/master", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            database = runtime.read(
                "client_database.json", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            self.assertEqual(master[0], fixture_values()["api/master"].body)
            self.assertEqual(database[0], fixture_values()["client_database.json"].body)
            state = runtime.state()
            self.assertEqual(
                state["groups"][GROUP_MASTER_DATABASE]["generation_id"],
                state["current"]["generation_id"],
            )
            self.assertIn("meta", master[0].decode("utf-8"))

    def test_settings_uses_separate_group_but_same_published_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_PUBLIC_SETTINGS))
            response = runtime.read(
                "api/settings", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            self.assertEqual(response[0], fixture_values()["api/settings"].body)
            state = runtime.state()
            self.assertEqual(
                state["groups"][GROUP_PUBLIC_SETTINGS]["generation_id"],
                state["current"]["generation_id"],
            )

    def test_failed_staging_is_never_current(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            runtime._importer = lambda snapshot, path: type(
                "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
            )()
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIsNone(runtime.state()["current"])
            self.assertFalse(list(Path(directory).glob("*.staging")))

    def test_pre_dispatch_fence_survives_restart_and_blocks_stale_read(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            with runtime.write_fence(GROUP_MASTER_DATABASE):
                state = runtime.state()
                self.assertFalse(state["groups"][GROUP_MASTER_DATABASE]["eligible"])
                self.assertEqual(
                    state["fences"][GROUP_MASTER_DATABASE]["reason"],
                    "pre_dispatch_write",
                )
            restarted = enabled_runtime(directory)
            self.assertFalse(
                restarted.state()["groups"][GROUP_MASTER_DATABASE]["eligible"]
            )
            self.assertRaises(
                UpstreamError,
                restarted.read,
                "api/master",
                lambda: (_ for _ in ()).throw(
                    UpstreamError(502, b'{"error":"typed http failure"}')
                ),
            )

    def test_runtime_eligibility_does_not_change_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            with runtime.write_fence(GROUP_PUBLIC_SETTINGS):
                pass
            self.assertTrue(runtime.read_enabled)
            self.assertTrue(runtime.group_enabled[GROUP_PUBLIC_SETTINGS])
            self.assertFalse(
                runtime.state()["groups"][GROUP_PUBLIC_SETTINGS]["eligible"]
            )

    @unittest.skipUnless(os.name == "nt", "requires Windows named mutex")
    def test_two_process_named_mutex_is_single_flight(self):
        name = r"Local\WebsiteSQLiteGenerationMutexTest" + str(os.getpid())
        context = multiprocessing.get_context("spawn")
        entered = context.Queue()
        release = context.Event()
        first = context.Process(target=mutex_worker, args=(name, entered, release))
        second = context.Process(target=mutex_worker, args=(name, entered, release))
        first.start()
        self.assertEqual(entered.get(timeout=5), first.pid)
        second.start()
        time.sleep(0.3)
        self.assertTrue(entered.empty())
        release.set()
        self.assertIn(entered.get(timeout=5), (first.pid, second.pid))
        self.assertIn(entered.get(timeout=5), (first.pid, second.pid))
        first.join(5)
        second.join(5)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)


if __name__ == "__main__":
    unittest.main()
