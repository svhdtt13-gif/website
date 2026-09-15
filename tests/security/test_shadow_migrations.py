#!/usr/bin/env python3
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import (
    SCHEMA_V6_CHECKSUM,
    SCHEMA_V6_SQL,
    OperationalSchemaError,
    OperationalSQLiteRepository,
    operational_path,
)


class ShadowMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.operational = OperationalSQLiteRepository.create(self.runtime)
        self.operational.close()
        operational_path(self.runtime).unlink()

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def _write_v6_database(self):
        path = operational_path(self.runtime)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V6_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "6", "schema_checksum", SCHEMA_V6_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v6",),
            )
            connection.execute(
                "INSERT INTO jobs(job_id, kind, target_json, requested_by, "
                "idempotency_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("job-v6", "refresh", "{}", "operator-a", "job-key-v6", "queued", "now"),
            )
            connection.execute(
                "INSERT INTO authority_bound_targets ("
                "target_id, job_id, operation_kind, target_ref, requested_by, "
                "idempotency_key, host_id, profile_id, binding_generation, "
                "verified_identity_ref, verified_identity_revision, authority_epoch, "
                "fence_counter, context_fingerprint, source_snapshot_digest, "
                "provenance_json, status, created_at) VALUES ("
                "?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "target-v6", "job-v6", "refresh", "profile-a", "operator-a",
                    "target-key-v6", "host-a", "profile-a", 1, "identity-a", 1,
                    "epoch-v6", 1, "context-v6", "snapshot-v6", "{}", "claimed", "now",
                ),
            )
            connection.execute(
                "INSERT INTO authority_bound_executions ("
                "execution_id, target_id, job_id, idempotency_key, host_id, profile_id, "
                "binding_generation, verified_identity_ref, verified_identity_revision, "
                "authority_epoch, fence_counter, owner_id, lease_id, status, attempt_count, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "execution-v6", "target-v6", "job-v6", "execution-key-v6", "host-a",
                    "profile-a", 1, "identity-a", 1, "epoch-v6", 1, "agent-a", "lease-v6",
                    "claimed", 1, "now",
                ),
            )
            connection.execute(
                "INSERT INTO authority_bound_attempts ("
                "attempt_id, execution_id, target_id, attempt_number, idempotency_key, "
                "host_id, profile_id, binding_generation, verified_identity_ref, "
                "verified_identity_revision, authority_epoch, fence_counter, owner_id, "
                "lease_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "attempt-v6", "execution-v6", "target-v6", 1, "target-key-v6", "host-a",
                    "profile-a", 1, "identity-a", 1, "epoch-v6", 1, "agent-a", "lease-v6",
                    "claimed", "now",
                ),
            )
            connection.execute(
                "INSERT INTO authority_bound_dispatch_intents ("
                "intent_id, execution_id, attempt_id, target_id, job_id, idempotency_key, "
                "operation_kind, target_ref, request_fingerprint, host_id, profile_id, "
                "binding_generation, verified_identity_ref, verified_identity_revision, "
                "authority_epoch, fence_counter, owner_id, lease_id, status, created_at, prepared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "intent-v6", "execution-v6", "attempt-v6", "target-v6", "job-v6",
                    "dispatch-v6", "refresh", "profile-a", "request-v6", "host-a",
                    "profile-a", 1, "identity-a", 1, "epoch-v6", 1, "agent-a", "lease-v6",
                    "prepared", "now", "now",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_v6_to_v7_installs_shadow_evaluations(self):
        self._write_v6_database()
        repository = OperationalSQLiteRepository.open(self.runtime)
        self.operational = repository
        self.assertEqual(
            repository.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "7",
        )
        self.assertIsNotNone(
            repository.rows(
                "SELECT 1 FROM sqlite_master "
                "WHERE name='authority_bound_shadow_evaluations'"
            )[0]
        )

    def test_v6_to_v7_preserves_populated_is2_data_and_reopens(self):
        self._write_v6_database()
        repository = OperationalSQLiteRepository.open(self.runtime)
        repository.close()
        self.operational = OperationalSQLiteRepository.open(self.runtime)

        self.assertEqual(
            self.operational.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "7",
        )
        self.assertEqual(
            tuple(
                self.operational.rows(
                    "SELECT job_id, status FROM jobs WHERE job_id='job-v6'"
                )[0]
            ),
            ("job-v6", "queued"),
        )
        self.assertEqual(
            tuple(
                self.operational.rows(
                    "SELECT intent_id, status FROM authority_bound_dispatch_intents "
                    "WHERE intent_id='intent-v6'"
                )[0]
            ),
            ("intent-v6", "prepared"),
        )

    def test_v6_checksum_tamper_rejects_before_shadow_migration(self):
        self._write_v6_database()
        connection = sqlite3.connect(operational_path(self.runtime))
        try:
            connection.execute(
                "UPDATE schema_meta SET value='tampered' WHERE key='schema_checksum'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(OperationalSchemaError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(operational_path(self.runtime))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "6",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE name='authority_bound_shadow_evaluations'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v6_to_v7_failure_rolls_back_shadow_schema(self):
        self._write_v6_database()
        with patch.object(
            OperationalSQLiteRepository,
            "_install_shadow_schema",
            side_effect=RuntimeError("shadow schema unavailable"),
        ), self.assertRaises(RuntimeError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(operational_path(self.runtime))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "6",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE name='authority_bound_shadow_evaluations'"
                ).fetchone()
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
