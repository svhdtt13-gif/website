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
    SCHEMA_V8_CHECKSUM,
    SCHEMA_V8_SQL,
    OperationalSchemaError,
    OperationalSQLiteRepository,
    operational_path,
)


class CanarySendMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.operational = OperationalSQLiteRepository.create(self.runtime)
        self.operational.close()
        operational_path(self.runtime).unlink()

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def _write_v8_database(self, populated=False):
        path = operational_path(self.runtime)
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V8_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "8", "schema_checksum", SCHEMA_V8_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v8",),
            )
            if populated:
                connection.execute(
                    "INSERT INTO jobs(job_id, kind, target_json, requested_by, "
                    "idempotency_key, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("job-v8", "refresh", "{}", "operator-a", "job-key-v8",
                     "queued", "now"),
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
                        "target-v8", "job-v8", "refresh", "profile-a", "operator-a",
                        "target-key-v8", "host-a", "profile-a", 1, "identity-a", 1,
                        "epoch-v8", 1, "context-v8", "snapshot-v8", "{}", "claimed",
                        "now",
                    ),
                )
                connection.execute(
                    "INSERT INTO authority_bound_executions ("
                    "execution_id, target_id, job_id, idempotency_key, host_id, profile_id, "
                    "binding_generation, verified_identity_ref, verified_identity_revision, "
                    "authority_epoch, fence_counter, owner_id, lease_id, status, attempt_count, "
                    "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "execution-v8", "target-v8", "job-v8", "execution-key-v8",
                        "host-a", "profile-a", 1, "identity-a", 1, "epoch-v8", 1,
                        "agent-a", "lease-v8", "claimed", 1, "now",
                    ),
                )
                connection.execute(
                    "INSERT INTO authority_bound_attempts ("
                    "attempt_id, execution_id, target_id, attempt_number, idempotency_key, "
                    "host_id, profile_id, binding_generation, verified_identity_ref, "
                    "verified_identity_revision, authority_epoch, fence_counter, owner_id, "
                    "lease_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        "attempt-v8", "execution-v8", "target-v8", 1, "target-key-v8",
                        "host-a", "profile-a", 1, "identity-a", 1, "epoch-v8", 1,
                        "agent-a", "lease-v8", "claimed", "now",
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
                        "intent-v8", "execution-v8", "attempt-v8", "target-v8",
                        "job-v8", "dispatch-v8", "refresh", "profile-a", "request-v8",
                        "host-a", "profile-a", 1, "identity-a", 1, "epoch-v8", 1,
                        "agent-a", "lease-v8", "prepared", "now", "now",
                    ),
                )
                connection.execute(
                    "INSERT INTO authority_bound_shadow_evaluations ("
                    "shadow_evaluation_id, intent_id, execution_id, attempt_id, target_id, "
                    "job_id, intent_idempotency_key, request_idempotency_key, operation_kind, "
                    "target_ref, host_id, profile_id, binding_generation, verified_identity_ref, "
                    "verified_identity_revision, authority_epoch, fence_counter, owner_id, lease_id, "
                    "envelope_version, transport_kind, portable_snapshot_fingerprint, "
                    "prepared_request_fingerprint, envelope_json, envelope_fingerprint, "
                    "transport_fingerprint, outcome, reason_class, created_at, evaluated_at) VALUES ("
                    + ",".join("?" for _ in range(30)) + ")",
                    (
                        "shadow-v8", "intent-v8", "execution-v8", "attempt-v8",
                        "target-v8", "job-v8", "dispatch-v8", "shadow-key-v8",
                        "refresh", "profile-a", "host-a", "profile-a", 1,
                        "identity-a", 1, "epoch-v8", 1, "agent-a", "lease-v8",
                        1, "recording", "snapshot-v8", "request-v8", "{}",
                        "b" * 64, "b" * 64, "matched", None, "now", "now",
                    ),
                )
                connection.execute(
                    "INSERT INTO authority_bound_canary_candidates ("
                    "canary_candidate_id, shadow_evaluation_id, intent_id, execution_id, "
                    "attempt_id, target_id, job_id, intent_idempotency_key, "
                    "shadow_idempotency_key, canary_idempotency_key, operation_kind, "
                    "destination_ref, host_id, profile_id, binding_generation, "
                    "verified_identity_ref, verified_identity_revision, authority_epoch, "
                    "fence_counter, owner_id, lease_id, portable_snapshot_fingerprint, "
                    "shadow_envelope_fingerprint, contract_version, transport_kind, "
                    "transport_contract_version, pre_send_identity, envelope_json, "
                    "envelope_fingerprint, state, reason_class, created_at, armed_at"
                    ") VALUES (" + ",".join("?" for _ in range(33)) + ")",
                    (
                        "canary-v8", "shadow-v8", "intent-v8", "execution-v8",
                        "attempt-v8", "target-v8", "job-v8", "dispatch-v8",
                        "shadow-key-v8", "canary-key-v8", "refresh", "profile-a",
                        "host-a", "profile-a", 1, "identity-a", 1, "epoch-v8", 1,
                        "agent-a", "lease-v8", "snapshot-v8", "b" * 64, "is3b1.v1",
                        "recording", "is3b1.v1", "pre-send-v8", "{}", "c" * 64,
                        "armed", None, "now", "now",
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def test_v8_to_v9_installs_send_intents(self):
        self._write_v8_database()
        repository = OperationalSQLiteRepository.open(self.runtime)
        self.operational = repository
        self.assertEqual(
            repository.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "9",
        )
        self.assertIsNotNone(
            repository.rows(
                "SELECT 1 FROM sqlite_master "
                "WHERE name='authority_bound_canary_send_intents'"
            )[0]
        )

    def test_v8_to_v9_preserves_populated_data_and_reopens(self):
        self._write_v8_database(populated=True)
        repository = OperationalSQLiteRepository.open(self.runtime)
        repository.close()
        self.operational = OperationalSQLiteRepository.open(self.runtime)
        self.assertEqual(
            self.operational.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "9",
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT state FROM authority_bound_canary_candidates "
                "WHERE canary_candidate_id='canary-v8'"
            )[0][0],
            "armed",
        )
        self.assertEqual(
            self.operational.rows(
                "SELECT outcome FROM authority_bound_shadow_evaluations "
                "WHERE shadow_evaluation_id='shadow-v8'"
            )[0][0],
            "matched",
        )

    def test_v8_checksum_tamper_rejects_before_send_migration(self):
        self._write_v8_database()
        path = operational_path(self.runtime)
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE schema_meta SET value='tampered' WHERE key='schema_checksum'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(OperationalSchemaError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "8",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE name='authority_bound_canary_send_intents'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v8_identity_tamper_rejects_before_send_migration(self):
        self._write_v8_database()
        path = operational_path(self.runtime)
        connection = sqlite3.connect(path)
        try:
            connection.execute("DROP INDEX canary_candidates_state_idx")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(OperationalSchemaError):
            OperationalSQLiteRepository.open(self.runtime)

    def test_v8_to_v9_failure_rolls_back_schema_and_metadata(self):
        self._write_v8_database()
        with patch.object(
            OperationalSQLiteRepository,
            "_install_send_schema",
            side_effect=RuntimeError("send schema unavailable"),
        ), self.assertRaises(RuntimeError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(operational_path(self.runtime))
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "8",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE name='authority_bound_canary_send_intents'"
                ).fetchone()
            )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
