from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_schema import (
    SCHEMA_CHECKSUM,
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM,
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_SQL,
    V3_SCHEMA_CHECKSUM,
    V3_SCHEMA_SQL,
)
from repositories.legacy_authority_store import (
    LegacyAuthorityStore,
    LegacyAuthorityStoreError,
    legacy_store_path,
)

FROZEN_V3_CHECKSUM = (
    "37095a6df77a40f6b2a6b3a8de9f2b28342b31ccc3fb1347569609368baf5316"
)


def _insert_v3_handoff(
    connection: sqlite3.Connection,
    *,
    suffix: str,
    envelope_fingerprint: str,
) -> None:
    connection.execute(
        "INSERT INTO handoffs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            f"handoff-{suffix}",
            "is3b1.v1",
            "is3b2.v1",
            f"send-{suffix}",
            f"idem-{suffix}",
            envelope_fingerprint,
            '{"operation_kind":"group_on","target_ref":"client:one"}',
            "group_on",
            "client:one",
            7,
            "identity:one",
            3,
            f"epoch-{suffix}",
            int(suffix),
            "exporter:one",
            "attestation:one",
        ),
    )


class LegacyAuthorityMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.path = legacy_store_path(Path(self.temporary.name))
        self.path.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def create_v3(self, *, checksum: str = V3_SCHEMA_CHECKSUM) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(V3_SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("store_kind", "legacy_canary_authority"),
                ("schema_version", "3"),
                ("schema_checksum", checksum),
                ("restore_state", "canonical"),
                ("restore_marker", ""),
            ),
        )
        return connection

    def test_frozen_v3_checksum_matches_pre_unique_schema(self) -> None:
        self.assertEqual(V3_SCHEMA_CHECKSUM, FROZEN_V3_CHECKSUM)

    def test_create_builds_only_canonical_v4(self) -> None:
        store = LegacyAuthorityStore.create(self.path)
        try:
            metadata = store.schema_metadata()
            index = store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' "
                "AND name='unique_handoffs_envelope_fingerprint'"
            ).fetchone()

            self.assertEqual(metadata["schema_version"], "4")
            self.assertEqual(metadata["schema_checksum"], SCHEMA_CHECKSUM)
            self.assertIsNotNone(index)
        finally:
            store.close()

    def test_exact_frozen_v3_is_upgraded_without_losing_rows(self) -> None:
        connection = self.create_v3()
        _insert_v3_handoff(
            connection,
            suffix="1",
            envelope_fingerprint="sha256:envelope-1",
        )
        connection.commit()
        connection.close()

        store = LegacyAuthorityStore.open(self.path)
        try:
            self.assertEqual(store.schema_metadata()["schema_version"], "4")
            self.assertEqual(
                store.connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0],
                1,
            )
        finally:
            store.close()

        reopened = LegacyAuthorityStore.open(self.path)
        reopened.close()

    def test_inline_unique_transitional_database_is_canonicalized_to_v4(self) -> None:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(TRANSITIONAL_INLINE_UNIQUE_SCHEMA_SQL)
        connection.executemany(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
            (
                ("store_kind", "legacy_canary_authority"),
                ("schema_version", "3"),
                (
                    "schema_checksum",
                    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM,
                ),
                ("restore_state", "canonical"),
                ("restore_marker", ""),
            ),
        )
        _insert_v3_handoff(
            connection,
            suffix="1",
            envelope_fingerprint="sha256:envelope-1",
        )
        connection.commit()
        connection.close()

        store = LegacyAuthorityStore.open(self.path)
        try:
            handoffs_sql = store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='handoffs'"
            ).fetchone()[0]
            self.assertEqual(store.schema_metadata()["schema_version"], "4")
            self.assertNotIn("envelope_fingerprint TEXT NOT NULL UNIQUE", handoffs_sql)
            self.assertEqual(
                store.connection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0],
                1,
            )
        finally:
            store.close()

    def test_duplicate_v3_fingerprint_rejects_and_rolls_back(self) -> None:
        connection = self.create_v3()
        _insert_v3_handoff(
            connection,
            suffix="1",
            envelope_fingerprint="sha256:duplicate",
        )
        _insert_v3_handoff(
            connection,
            suffix="2",
            envelope_fingerprint="sha256:duplicate",
        )
        connection.commit()
        connection.close()

        with self.assertRaises(LegacyAuthorityStoreError):
            LegacyAuthorityStore.open(self.path)

        with closing(sqlite3.connect(self.path)) as inspection:
            metadata = dict(inspection.execute("SELECT key, value FROM schema_meta"))
            named_index = inspection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' "
                "AND name='unique_handoffs_envelope_fingerprint'"
            ).fetchone()
            self.assertEqual(metadata["schema_version"], "3")
            self.assertEqual(metadata["schema_checksum"], V3_SCHEMA_CHECKSUM)
            self.assertEqual(inspection.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0], 2)
            self.assertIsNone(named_index)

    def test_bad_v3_metadata_is_rejected_without_migration(self) -> None:
        connection = self.create_v3(checksum="0" * 64)
        connection.commit()
        connection.close()

        with self.assertRaises(LegacyAuthorityStoreError):
            LegacyAuthorityStore.open(self.path)

        with closing(sqlite3.connect(self.path)) as inspection:
            metadata = dict(inspection.execute("SELECT key, value FROM schema_meta"))
            self.assertEqual(metadata["schema_version"], "3")
            self.assertEqual(metadata["schema_checksum"], "0" * 64)

    def test_two_connections_can_concurrently_open_and_migrate_v3(self) -> None:
        connection = self.create_v3()
        connection.commit()
        connection.close()

        def open_version() -> str:
            store = LegacyAuthorityStore.open(self.path)
            try:
                return store.schema_metadata()["schema_version"]
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            versions = list(executor.map(lambda _index: open_version(), range(2)))

        self.assertEqual(versions, ["4", "4"])


if __name__ == "__main__":
    unittest.main()
