#!/usr/bin/env python3
"""Runtime acceptance tests for the P4 operational SQLite foundation."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import (  # noqa: E402
    OPERATIONAL_FILENAME,
    OperationalIntegrityError,
    OperationalPathError,
    OperationalSQLiteRepository,
    SCHEMA_VERSION,
    SCHEMA_CHECKSUM,
    backup_directory,
    operational_path,
)


class OperationalSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.repository = OperationalSQLiteRepository.create(self.runtime)

    def tearDown(self):
        self.repository.close()
        self.temp.cleanup()

    def test_schema_wal_foreign_keys_and_integrity_are_verified(self):
        self.assertEqual(
            self.repository.rows("PRAGMA journal_mode")[0][0].lower(), "wal"
        )
        self.assertEqual(self.repository.rows("PRAGMA foreign_keys")[0][0], 1)
        self.assertEqual(self.repository.rows("PRAGMA integrity_check")[0][0], "ok")
        schema = {
            row[0]: row[1]
            for row in self.repository.rows("SELECT key, value FROM schema_meta")
        }
        self.assertEqual(schema["schema_version"], str(SCHEMA_VERSION))
        self.assertEqual(schema["schema_checksum"], SCHEMA_CHECKSUM)
        tables = {
            row[0]
            for row in self.repository.rows(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue(
            {
                "schema_meta", "jobs", "leases", "checkpoints",
                "manual_overrides", "worker_events",
            }.issubset(tables)
        )

    def test_only_dedicated_operational_path_is_openable(self):
        p3 = self.runtime / "sqlite" / "verified.sqlite3"
        p3.parent.mkdir(parents=True)
        p3.write_bytes(b"p3-generation")
        with self.assertRaises(OperationalPathError):
            OperationalSQLiteRepository.open_path(p3, self.runtime)
        with self.assertRaises(OperationalPathError):
            OperationalSQLiteRepository.open_path(
                self.runtime / "arbitrary.sqlite3", self.runtime
            )
        self.assertEqual(operational_path(self.runtime).name, OPERATIONAL_FILENAME)

    def test_attach_and_p3_write_are_denied_without_mutating_p3(self):
        p3 = self.runtime / "sqlite" / "verified.sqlite3"
        p3.parent.mkdir(parents=True)
        p3.write_bytes(b"immutable-p3")
        before_hash = hashlib.sha256(p3.read_bytes()).hexdigest()
        before_mtime = p3.stat().st_mtime_ns
        with self.assertRaises(sqlite3.DatabaseError):
            self.repository.connection.execute(
                "ATTACH DATABASE ? AS p3", (str(p3),)
            )
        self.assertEqual(hashlib.sha256(p3.read_bytes()).hexdigest(), before_hash)
        self.assertEqual(p3.stat().st_mtime_ns, before_mtime)

    def test_transaction_primitive_rolls_back_all_changes(self):
        with self.assertRaises(RuntimeError):
            with self.repository.transaction():
                self.repository.connection.execute(
                    "INSERT INTO checkpoints VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("checkpoint-1", "test", None, None, None, None, "[]", "now", "{}"),
                )
                raise RuntimeError("force rollback")
        self.assertEqual(
            self.repository.rows(
                "SELECT COUNT(*) FROM checkpoints WHERE checkpoint_id='checkpoint-1'"
            )[0][0],
            0,
        )

    def test_job_claim_and_lease_heartbeat_are_atomic(self):
        self.repository.insert_job(
            "job-1", "test", '{"target":"x"}', "test", "idem-1",
            created_at="2026-09-09T10:00:00+00:00",
        )
        self.assertTrue(
            self.repository.claim_job(
                "job-1", "owner-a", "remote_io", "2026-09-09T10:05:00+00:00",
                now="2026-09-09T10:00:01+00:00",
            )
        )
        job = self.repository.rows(
            "SELECT status, owner_id, attempt, lease_until FROM jobs WHERE job_id='job-1'"
        )[0]
        lease = self.repository.rows(
            "SELECT owner_id, heartbeat_at, expires_at FROM leases WHERE lease_name='remote_io'"
        )[0]
        self.assertEqual(
            tuple(job),
            ("claimed", "owner-a", 1, "2026-09-09T10:05:00+00:00"),
        )
        self.assertEqual(
            tuple(lease),
            ("owner-a", "2026-09-09T10:00:01+00:00", "2026-09-09T10:05:00+00:00"),
        )
        self.assertTrue(
            self.repository.heartbeat_lease(
                "remote_io", "owner-a", "2026-09-09T10:01:00+00:00",
                "2026-09-09T10:10:00+00:00",
            )
        )
        self.assertEqual(
            self.repository.rows(
                "SELECT lease_until FROM jobs WHERE job_id='job-1'"
            )[0][0],
            "2026-09-09T10:10:00+00:00",
        )

    def test_active_lease_blocks_other_owner_without_partial_claim(self):
        self.repository.insert_job("job-2", "test", "{}", "test", "idem-2")
        self.assertTrue(
            self.repository.claim_job(
                "job-2", "owner-a", "scheduler", "2999-01-01T00:00:00+00:00",
                now="2026-09-09T10:00:00+00:00",
            )
        )
        self.assertFalse(
            self.repository.claim_job(
                "job-2", "owner-b", "scheduler", "2999-01-01T00:01:00+00:00",
                now="2026-09-09T10:00:01+00:00",
            )
        )
        row = self.repository.rows(
            "SELECT owner_id, attempt FROM jobs WHERE job_id='job-2'"
        )[0]
        self.assertEqual(tuple(row), ("owner-a", 1))

    def test_backup_restore_preserves_p3_hash_and_mtime(self):
        p3 = self.runtime / "sqlite" / "f2e2bd.sqlite3"
        p3.parent.mkdir(parents=True)
        p3.write_bytes(b"immutable-generation")
        p3_hash = hashlib.sha256(p3.read_bytes()).hexdigest()
        p3_mtime = p3.stat().st_mtime_ns

        self.repository.insert_job("job-3", "test", "{}", "test", "idem-3")
        backup, manifest = self.repository.backup_to(
            backup_directory(self.runtime) / "slice1a.sqlite3"
        )
        self.assertTrue(backup.is_file())
        self.assertTrue(manifest.is_file())
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(metadata["sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        self.repository.insert_job("job-after-backup", "test", "{}", "test", "idem-after")
        self.repository.close()
        self.repository = OperationalSQLiteRepository.restore_from(self.runtime, backup)
        job_ids = [row[0] for row in self.repository.rows("SELECT job_id FROM jobs ORDER BY job_id")]
        self.assertEqual(job_ids, ["job-3"])
        self.assertEqual(hashlib.sha256(p3.read_bytes()).hexdigest(), p3_hash)
        self.assertEqual(p3.stat().st_mtime_ns, p3_mtime)

    def test_corrupt_backup_is_rejected_before_restore(self):
        backup, _manifest = self.repository.backup_to(
            backup_directory(self.runtime) / "corrupt.sqlite3"
        )
        self.repository.close()
        backup.write_bytes(backup.read_bytes() + b"tamper")
        with self.assertRaises(OperationalIntegrityError):
            OperationalSQLiteRepository.restore_from(self.runtime, backup)
        self.repository = OperationalSQLiteRepository.open(self.runtime)


if __name__ == "__main__":
    unittest.main()
