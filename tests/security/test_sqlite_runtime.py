#!/usr/bin/env python3
"""Acceptance tests for guarded SQLite generation runtime behavior."""
import multiprocessing
import os
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

from repositories.aitool import UpstreamError  # noqa: E402
from repositories.sqlite import SQLiteCandidateRepository  # noqa: E402
from services import sqlite_runtime  # noqa: E402
from services.sqlite_runtime import (  # noqa: E402
    GROUP_MASTER_DATABASE,
    GROUP_PUBLIC_SETTINGS,
    MUTEX_NAME,
    NamedGenerationMutex,
    SQLiteRuntimeCoordinator,
)
from test_sqlite_import import FakeSource, fixture_values, replace_json  # noqa: E402


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
    def test_verified_generation_reconstructs_normalized_route_shapes(self):
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
            self.assertEqual(master[1], 200)
            self.assertEqual(database[0], fixture_values()["client_database.json"].body)
            self.assertEqual(database[1], 200)
            state = runtime.state()
            self.assertEqual(
                state["groups"][GROUP_MASTER_DATABASE]["generation_id"],
                state["current"]["generation_id"],
            )
            self.assertIn("meta", master[0].decode("utf-8"))
            candidate = Path(state["current"]["candidate_path"])
            repository = SQLiteCandidateRepository.open_read_only(candidate)
            self.assertEqual(repository.rows("PRAGMA query_only")[0][0], 1)
            self.assertEqual(
                repository.rows(
                    "SELECT raw_json FROM master_meta WHERE run_id=?",
                    (state["current"]["run_id"],),
                )[0][0],
                '{"source":"test"}',
            )
            repository.close()

    def test_settings_uses_separate_group_but_published_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_PUBLIC_SETTINGS))
            response = runtime.read(
                "api/settings", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            self.assertEqual(response[0], fixture_values()["api/settings"].body)
            self.assertEqual(response[1], 200)
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

    def test_checkpoint_failure_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            with patch.object(
                SQLiteCandidateRepository,
                "close",
                side_effect=sqlite3.DatabaseError("checkpoint failed"),
            ):
                self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIsNone(runtime.state()["current"])
            self.assertFalse(list(Path(directory).glob("*.sqlite3")))

    def test_refresh_lease_prevents_parallel_importers_past_nominal_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            runtime.refresh_timeout_seconds = 0.05
            started = threading.Event()
            release = threading.Event()
            calls = []

            def slow_import(snapshot, path):
                calls.append(True)
                started.set()
                release.wait(2)
                return type(
                    "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
                )()

            runtime._importer = slow_import
            first_result = []
            first = threading.Thread(
                target=lambda: first_result.append(
                    runtime.refresh_now(GROUP_MASTER_DATABASE)
                )
            )
            first.start()
            self.assertTrue(started.wait(2))
            time.sleep(1.1)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertEqual(len(calls), 1)
            release.set()
            first.join(3)
            self.assertEqual(first_result, [False])

    def test_cross_process_pending_group_survives_active_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            state = runtime.state()
            state["refresh_lease"] = {
                "token": "other-process",
                "owner_pid": 987654,
                "started_at": "2026-09-06T10:00:00+00:00",
                "heartbeat_at": "2026-09-06T10:00:01+00:00",
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
            sqlite_runtime._atomic_json(runtime.state_path, state)
            self.assertTrue(runtime.refresh_now(GROUP_PUBLIC_SETTINGS))
            self.assertEqual(
                runtime.state()["refresh_pending"], [GROUP_PUBLIC_SETTINGS]
            )
            with patch.object(runtime, "request_refresh", return_value=True) as request:
                runtime._schedule_shared_pending()
            self.assertEqual(runtime.state()["refresh_pending"], [])
            request.assert_called_once_with(GROUP_PUBLIC_SETTINGS)

    def test_failed_owner_releases_lease_before_draining_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            runtime._importer = lambda snapshot, path: type(
                "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
            )()
            state = runtime.state()
            state["refresh_pending"] = [GROUP_PUBLIC_SETTINGS]
            sqlite_runtime._atomic_json(runtime.state_path, state)
            with patch.object(runtime, "request_refresh", return_value=True) as request:
                self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            self.assertIsNone(state["refresh_lease"])
            self.assertEqual(state["refresh_pending"], [])
            request.assert_called_once_with(GROUP_PUBLIC_SETTINGS)

    def test_cleanup_retries_until_generation_mutex_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            started = threading.Event()
            release_build = threading.Event()

            def failing_import(snapshot, path):
                started.set()
                release_build.wait(2)
                return type(
                    "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
                )()

            runtime._importer = failing_import
            state = runtime.state()
            state["refresh_pending"] = [GROUP_PUBLIC_SETTINGS]
            sqlite_runtime._atomic_json(runtime.state_path, state)
            result = []
            owner = threading.Thread(
                target=lambda: result.append(
                    runtime.refresh_now(GROUP_MASTER_DATABASE)
                )
            )
            with patch.object(runtime, "request_refresh", return_value=True) as request:
                owner.start()
                self.assertTrue(started.wait(2))
                with runtime._mutex.hold():
                    release_build.set()
                    owner.join(2)
                    self.assertEqual(result, [False])
                    self.assertIsNotNone(runtime.state()["refresh_lease"])
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    state = runtime.state()
                    if state["refresh_lease"] is None and request.call_count:
                        break
                    time.sleep(0.05)
                self.assertIsNone(runtime.state()["refresh_lease"])
                self.assertEqual(runtime.state()["refresh_pending"], [])
                request.assert_called_once_with(GROUP_PUBLIC_SETTINGS)

    def test_publication_revalidation_exit_releases_owner_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)

            def stale_build(group, staging, deadline):
                state = runtime.state()
                state["current"] = {"generation_id": "newer"}
                sqlite_runtime._atomic_json(runtime.state_path, state)
                return object(), object()

            runtime._build_candidate = stale_build
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIsNone(runtime.state()["refresh_lease"])

    def test_captured_at_is_freshness_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["captured_at"] = "2000-01-01T00:00:00+00:00"
            self.assertFalse(runtime._eligible(state, GROUP_MASTER_DATABASE))

    def test_mutex_timeout_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            runtime._mutex.timeout_seconds = 0.05
            result = []
            with runtime._mutex.hold():
                worker = threading.Thread(
                    target=lambda: result.append(
                        runtime.refresh_now(GROUP_MASTER_DATABASE)
                    )
                )
                worker.start()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [False])

    def test_local_build_timeout_does_not_hold_mutex(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            runtime.refresh_timeout_seconds = 0.05

            def slow_import(snapshot, path):
                time.sleep(0.2)
                return type(
                    "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
                )()

            runtime._importer = slow_import
            started = time.monotonic()
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertLess(time.monotonic() - started, 0.15)

    def test_write_fence_requires_observed_master_and_database_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            expected = {
                "endpoint": "api/master",
                "changes": [{"client": "client_2", "name": "Second-new"}],
            }
            with runtime.write_fence(GROUP_MASTER_DATABASE, expected):
                pass
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIn(GROUP_MASTER_DATABASE, runtime.state()["fences"])

            values = fixture_values()
            replace_json(
                values,
                "api/master",
                lambda payload: payload["clients"][0].update(name="Second-new"),
            )
            replace_json(
                values,
                "client_database.json",
                lambda payload: payload["clients"][0].update(name="Second-new"),
            )
            runtime._source_factory = lambda: FakeSource(values)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            self.assertNotIn(GROUP_MASTER_DATABASE, state["fences"])
            self.assertTrue(state["groups"][GROUP_MASTER_DATABASE]["eligible"])

    def test_normalized_corruption_falls_back_for_each_wave_one_table(self):
        cases = (
            (
                "master_clients",
                "UPDATE master_clients SET raw_json=?",
                ('{"client":"client_2","name":"tampered","group":"fixed","selected":true}',),
                "api/master",
            ),
            (
                "database_clients",
                "UPDATE database_clients SET raw_json=?",
                ('{"idx":9,"client":"client_2","name":"tampered","status":"offline","group":"fixed","selected":true}',),
                "client_database.json",
            ),
            (
                "public_settings",
                "UPDATE public_settings SET auto_telegram=1",
                (),
                "api/settings",
            ),
        )
        for table, statement, args, route in cases:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                runtime = enabled_runtime(directory, background_refresh=False)
                self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
                candidate = Path(runtime.state()["current"]["candidate_path"])
                repository = SQLiteCandidateRepository.open_existing(candidate)
                with repository.transaction():
                    repository.connection.execute(statement, args)
                repository.close()
                fallback = (b"http", 200, "application/json")
                self.assertEqual(runtime.read(route, lambda: fallback), fallback)

    def test_schema_mismatch_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["schema_version"] = 999
            sqlite_runtime._atomic_json(runtime.state_path, state)
            fallback = (b"http", 200, "application/json")
            self.assertEqual(runtime.read("api/master", lambda: fallback), fallback)

    def test_reader_fallback_does_not_wait_for_refresh_mutex(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["captured_at"] = "2000-01-01T00:00:00+00:00"
            sqlite_runtime._atomic_json(runtime.state_path, state)
            fallback = (b"http", 200, "application/json")
            started = time.monotonic()
            with runtime._mutex.hold():
                self.assertEqual(runtime.read("api/master", lambda: fallback), fallback)
            self.assertLess(time.monotonic() - started, 0.15)

    def test_startup_refresh_queues_each_missing_group_while_active(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            runtime._refresh_active = True
            started = runtime.startup_refresh()
            self.assertEqual(
                [group for group, result in started],
                [GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS],
            )
            self.assertEqual(
                set(runtime._refresh_pending),
                {GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS},
            )
            runtime._refresh_pending.clear()
            runtime._refresh_active = False

    def test_startup_refresh_requests_each_enabled_missing_group(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            with patch.object(runtime, "request_refresh", return_value=True) as refresh:
                started = runtime.startup_refresh()
            self.assertEqual(
                [group for group, result in started],
                [GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS],
            )
            self.assertEqual(refresh.call_count, 2)

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

    def test_ineligible_http_error_never_stale_serves(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            with runtime.write_fence(GROUP_MASTER_DATABASE):
                with self.assertRaises(UpstreamError) as error:
                    runtime.read(
                        "api/master",
                        lambda: (_ for _ in ()).throw(
                            UpstreamError(503, b'{"error":"http unavailable"}')
                        ),
                    )
            self.assertEqual(error.exception.status, 503)

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
        name = "Local\\WebsiteSQLiteGenerationMutexTest" + str(os.getpid())
        context = multiprocessing.get_context("spawn")
        entered = context.Queue()
        release = context.Event()
        first = context.Process(target=mutex_worker, args=(name, entered, release))
        second = context.Process(target=mutex_worker, args=(name, entered, release))
        first.start()
        first_pid = entered.get(timeout=5)
        self.assertEqual(first_pid, first.pid)
        second.start()
        with self.assertRaises(queue.Empty):
            entered.get(timeout=0.3)
        release.set()
        second_pid = entered.get(timeout=5)
        self.assertIn(second_pid, (first.pid, second.pid))
        first.join(5)
        second.join(5)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)


class FlaskRouteTests(unittest.TestCase):
    def _runtime_stub(self):
        class RuntimeStub:
            def __init__(self):
                self.routes = []

            def startup_refresh(self):
                return []

            def configured_enabled(self, _group):
                return False

            def read(self, route, fallback):
                if route not in sqlite_runtime.ROUTE_ENDPOINTS:
                    return fallback()
                self.routes.append(route)
                return fallback()

        return RuntimeStub()

    def test_sqlite_runtime_is_only_called_for_wave_one_routes(self):
        from app import create_app

        runtime = self._runtime_stub()
        app = create_app(runtime=runtime)
        app.testing = True
        with patch("app.master_service.get_api_master", return_value=(b'{"meta":{},"clients":[],"schedule":[]}', 200, "application/json")), \
             patch("app.master_service.get_master", return_value=(b'{"clients":[],"schedule":[]}', 200, "application/json")), \
             patch("app.master_service.get_database", return_value=(b'{"lastUpdated":"x","clients":[],"schedule":[]}', 200, "application/json")), \
             patch("app.settings_service.get_settings", return_value=(b"{}", 200, "application/json")):
            client = app.test_client()
            expected = {
                "/up/api/master": b'{"meta":{},"clients":[],"schedule":[]}',
                "/up/client_database.json": b'{"lastUpdated":"x","clients":[],"schedule":[]}',
                "/up/api/settings": b"{}",
                "/up/clients_master.json": b'{"clients":[],"schedule":[]}',
            }
            for path, body in expected.items():
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, body)
                self.assertIn("application/json", response.content_type)
        self.assertEqual(
            runtime.routes,
            ["api/master", "client_database.json", "api/settings"],
        )

    def test_wave_one_typed_errors_preserve_status_and_json_contract(self):
        from app import create_app

        cases = (
            ("api/master", "master_service.get_api_master"),
            ("client_database.json", "master_service.get_database"),
            ("api/settings", "settings_service.get_settings"),
            ("clients_master.json", "master_service.get_master"),
        )
        for route, target in cases:
            with self.subTest(route=route):
                app = create_app(runtime=self._runtime_stub())
                app.testing = True
                with patch(
                    "app." + target,
                    side_effect=UpstreamError(503, b'{"error":"typed"}'),
                ):
                    response = app.test_client().get("/up/" + route)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data, b'{"error":"typed"}')
                self.assertIn("application/json", response.content_type)

    def test_default_off_write_path_has_no_sqlite_fence(self):
        class RuntimeStub:
            def startup_refresh(self):
                return []

            def configured_enabled(self, _group):
                return False

            def read(self, _route, fallback):
                return fallback()

        import app as app_module
        from app import create_app

        captured = {}

        def handler(_body, _content_type, before_upstream_write=None):
            captured["fence"] = before_upstream_write
            return b"{}", 200, "application/json"

        app = create_app(runtime=RuntimeStub())
        app.testing = True
        with patch("app._write_authorized", return_value=True), patch.dict(
            app_module.WRITE_HANDLERS, {"api/master": handler}, clear=False
        ):
            response = app.test_client().post(
                "/up/api/master",
                data=b"{}",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(captured["fence"])


if __name__ == "__main__":
    unittest.main()
