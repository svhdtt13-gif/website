#!/usr/bin/env python3
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import (
    SCHEMA_V2_CHECKSUM,
    SCHEMA_V2_SQL,
    SCHEMA_V3_CHECKSUM,
    SCHEMA_V3_SQL,
    SCHEMA_V4_CHECKSUM,
    SCHEMA_V4_SQL,
    SCHEMA_V5_CHECKSUM,
    SCHEMA_V5_SQL,
    OperationalSchemaError,
    OperationalSQLiteRepository,
    operational_path,
)


class OperationalMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.operational = OperationalSQLiteRepository.create(self.runtime)

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def test_v3_to_v4_migration_rolls_back_target_schema_installation(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V3_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "3", "schema_checksum", SCHEMA_V3_CHECKSUM),
            )
            connection.commit()
        finally:
            connection.close()

        with patch.object(
            OperationalSQLiteRepository,
            "_install_authority_target_schema",
            side_effect=RuntimeError("target schema unavailable"),
        ), self.assertRaises(RuntimeError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "3",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='authority_bound_targets'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v2_to_v6_failure_rolls_back_and_reopen_resumes(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V2_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "2", "schema_checksum", SCHEMA_V2_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v2",),
            )
            connection.commit()
        finally:
            connection.close()

        with patch.object(
            OperationalSQLiteRepository,
            "_install_authority_target_schema",
            side_effect=RuntimeError("target schema unavailable"),
        ), self.assertRaises(RuntimeError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "2",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='authority_bound_targets'"
                ).fetchone()
            )
        finally:
            connection.close()

        self.operational = OperationalSQLiteRepository.open(self.runtime)
        self.assertEqual(
            self.operational.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "6",
        )
        self.assertIsNotNone(
            self.operational.rows(
                "SELECT 1 FROM sqlite_master WHERE name='authority_bound_targets'"
            )[0]
        )
        self.assertIsNotNone(
            self.operational.rows(
                "SELECT 1 FROM sqlite_master WHERE name='authority_bound_dispatch_intents'"
            )[0]
        )

    def test_v3_migration_rechecks_metadata_after_prelock_change(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V3_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "3", "schema_checksum", SCHEMA_V3_CHECKSUM),
            )
            connection.commit()
        finally:
            connection.close()

        original_transaction = OperationalSQLiteRepository.transaction

        @contextmanager
        def race_before_lock(repository, immediate=True):
            raced = sqlite3.connect(path)
            try:
                raced.execute(
                    "UPDATE schema_meta SET value='raced' WHERE key='schema_checksum'"
                )
                raced.commit()
            finally:
                raced.close()
            with original_transaction(repository, immediate) as transaction:
                yield transaction

        with patch.object(
            OperationalSQLiteRepository, "transaction", race_before_lock
        ), self.assertRaises(OperationalSchemaError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_checksum'"
                ).fetchone()[0],
                "raced",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='authority_bound_targets'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v4_to_v6_migration_installs_execution_and_dispatch_tables(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V4_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "4", "schema_checksum", SCHEMA_V4_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v4",),
            )
            connection.commit()
        finally:
            connection.close()

        repository = OperationalSQLiteRepository.open(self.runtime)
        self.operational = repository
        self.assertEqual(
            repository.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "6",
        )
        self.assertIsNotNone(
            repository.rows(
                "SELECT 1 FROM sqlite_master WHERE name='authority_bound_executions'"
            )[0]
        )
        self.assertIsNotNone(
            repository.rows(
                "SELECT 1 FROM sqlite_master WHERE name='authority_bound_attempts'"
            )[0]
        )
        self.assertIsNotNone(
            repository.rows(
                "SELECT 1 FROM sqlite_master WHERE name='authority_bound_dispatch_intents'"
            )[0]
        )

    def test_v4_to_v5_failure_rolls_back_execution_schema_installation(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V4_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "4", "schema_checksum", SCHEMA_V4_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v4",),
            )
            connection.commit()
        finally:
            connection.close()

        with patch.object(
            OperationalSQLiteRepository,
            "_install_execution_schema",
            side_effect=RuntimeError("execution schema unavailable"),
        ), self.assertRaises(RuntimeError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "4",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='authority_bound_executions'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v5_to_v6_migration_installs_dispatch_schema(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V5_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "5", "schema_checksum", SCHEMA_V5_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v5",),
            )
            connection.commit()
        finally:
            connection.close()

        repository = OperationalSQLiteRepository.open(self.runtime)
        self.operational = repository
        self.assertEqual(
            repository.rows(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            )[0][0],
            "6",
        )
        self.assertIsNotNone(
            repository.rows(
                "SELECT 1 FROM sqlite_master WHERE name='authority_bound_dispatch_intents'"
            )[0]
        )

    def test_v5_to_v6_failure_rolls_back_dispatch_schema_installation(self):
        self.operational.close()
        path = operational_path(self.runtime)
        path.unlink()
        connection = sqlite3.connect(path)
        try:
            connection.executescript(SCHEMA_V5_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?)",
                ("schema_version", "5", "schema_checksum", SCHEMA_V5_CHECKSUM),
            )
            connection.execute(
                "INSERT INTO authority_state VALUES (1, ?, 0, 0)",
                ("epoch-v5",),
            )
            connection.commit()
        finally:
            connection.close()

        with patch.object(
            OperationalSQLiteRepository,
            "_install_dispatch_schema",
            side_effect=RuntimeError("dispatch schema unavailable"),
        ), self.assertRaises(RuntimeError):
            OperationalSQLiteRepository.open(self.runtime)

        connection = sqlite3.connect(path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "5",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='authority_bound_dispatch_intents'"
                ).fetchone()
            )
        finally:
            connection.close()

if __name__ == "__main__":
    unittest.main()
