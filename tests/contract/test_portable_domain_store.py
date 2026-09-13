#!/usr/bin/env python3
"""Acceptance tests for the Portable Domain Store Foundation."""
import hashlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.portable_store import (
    SCHEMA_V2_CHECKSUM,
    SCHEMA_V2_SQL,
    SCHEMA_VERSION,
    BindingError,
    PortableDomainStore,
    ProfileRequiredError,
    SchemaError,
)
from services.shadow_import import (
    FileSystemGoldenSource,
    GoldenSnapshot,
    LegacyBinding,
    ShadowImportError,
    import_shadow,
)

NOW = "2026-09-09T00:00:00+00:00"


class PortableDomainStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "portable.sqlite3"
        self.store = PortableDomainStore.create(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def seed(self, profile_id, host_id, account_ref):
        self.store.add_host(host_id, host_id, "explicit-test-origin", NOW)
        self.store.add_profile(profile_id, profile_id, account_ref, "VERIFIED", NOW)
        self.store.bind_profile("binding-" + profile_id, host_id, profile_id,
                                account_ref, 1, "ACTIVE", NOW)

    def create_v2_fixture(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(SCHEMA_V2_SQL)
            connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?), (?, ?), (?, ?)",
                (
                    "store_kind", "portable_domain_store",
                    "schema_version", "2",
                    "schema_checksum", SCHEMA_V2_CHECKSUM,
                ),
            )
            connection.execute(
                "INSERT INTO hosts VALUES (?, ?, ?, ?, ?)",
                ("host-a", "Host A", "explicit-test-origin", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO remote_profiles VALUES (?, ?, ?, ?, ?, ?)",
                ("profile-a", "Profile A", "account-a", "VERIFIED", NOW, NOW),
            )
            connection.execute(
                "INSERT INTO host_profile_bindings VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("binding-a", "host-a", "profile-a", 1, "ACTIVE", NOW, NOW),
            )
            connection.commit()
        finally:
            connection.close()

    def test_empty_database_migrates_deterministically_and_reopens(self):
        self.assertEqual(
            SCHEMA_V2_CHECKSUM,
            "0e07cb21f4b7e013aba89a85a9d580ba2bafeba4b13e28de1781028e5f8d04ec",
        )
        meta = dict(self.store.connection.execute("SELECT key, value FROM schema_meta"))
        self.assertEqual(meta["schema_version"], str(SCHEMA_VERSION))
        self.assertEqual(meta["store_kind"], "portable_domain_store")
        tables = {
            row[0] for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertTrue({
            "schema_meta", "hosts", "remote_profiles", "host_profile_bindings",
            "profile_clients", "profile_schedules", "profile_policies",
            "profile_control_intents", "profile_observations", "profile_audit_events",
        }.issubset(tables))
        self.seed("profile-a", "host-a", "account-a")
        self.store.upsert_client("profile-a", "shared", "A", "HAMI", "running", "test")
        self.store.close()
        self.store = PortableDomainStore.open(self.path)
        self.assertEqual(self.store.list_clients("profile-a")[0]["display_name"], "A")

    def test_v2_migration_adds_unverified_identity_without_inference(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()

        migrated = PortableDomainStore.open(self.path)
        try:
            meta = dict(migrated.connection.execute("SELECT key, value FROM schema_meta"))
            columns = {
                row[1]
                for row in migrated.connection.execute("PRAGMA table_info(remote_profiles)")
            }
            identity = migrated.connection.execute(
                "SELECT verified_identity_ref FROM remote_profiles WHERE profile_id=?",
                ("profile-a",),
            ).fetchone()[0]
            self.assertEqual(meta["schema_version"], str(SCHEMA_VERSION))
            self.assertIn("verified_identity_ref", columns)
            self.assertIsNone(identity)
            self.assertEqual(
                migrated.connection.execute(
                    "SELECT account_ref FROM remote_profiles WHERE profile_id=?",
                    ("profile-a",),
                ).fetchone()[0],
                "account-a",
            )
        finally:
            migrated.close()

    def test_tampered_v2_schema_fails_before_identity_migration(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("ALTER TABLE remote_profiles ADD COLUMN tampered TEXT")
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SchemaError):
            PortableDomainStore.open(self.path)

    def test_wrong_v2_checksum_fails_before_identity_migration(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE schema_meta SET value='wrong' WHERE key='schema_checksum'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SchemaError):
            PortableDomainStore.open(self.path)

    def test_unsupported_v1_claim_fails_before_any_upgrade(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "UPDATE schema_meta SET value='1' WHERE key='schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SchemaError):
            PortableDomainStore.open(self.path)

        connection = sqlite3.connect(self.path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "1",
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM pragma_table_info('remote_profiles') "
                    "WHERE name='verified_identity_ref'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_partial_v2_metadata_fails_before_identity_migration(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "DELETE FROM schema_meta WHERE key='schema_checksum'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SchemaError):
            PortableDomainStore.open(self.path)

    def test_missing_v2_version_fails_closed_before_identity_migration(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "DELETE FROM schema_meta WHERE key='schema_version'"
            )
            connection.commit()
        finally:
            connection.close()

        with self.assertRaises(SchemaError):
            PortableDomainStore.open(self.path)

        connection = sqlite3.connect(self.path)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM pragma_table_info('remote_profiles') "
                    "WHERE name='verified_identity_ref'"
                ).fetchone()
            )
        finally:
            connection.close()

    def test_v2_migration_rolls_back_when_migration_fails(self):
        self.store.close()
        self.path.unlink()
        self.create_v2_fixture()

        def fail_migration(connection):
            connection.execute(
                "ALTER TABLE remote_profiles ADD COLUMN transient TEXT"
            )
            raise sqlite3.DatabaseError("migration failure")

        with patch.dict(
            "repositories.portable_store.MIGRATIONS", {3: fail_migration}
        ), self.assertRaises(sqlite3.DatabaseError):
            PortableDomainStore.open(self.path)

        connection = sqlite3.connect(self.path)
        try:
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM pragma_table_info('remote_profiles') "
                    "WHERE name='transient'"
                ).fetchone()
            )
            self.assertEqual(
                connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()[0],
                "2",
            )
        finally:
            connection.close()

    def test_verified_identity_requires_explicit_non_secret_reference(self):
        with self.assertRaises(BindingError):
            self.store.record_verified_identity("missing", "identity-a", NOW)

        self.seed("profile-a", "host-a", "account-a")
        self.assertIsNone(
            self.store.connection.execute(
                "SELECT verified_identity_ref FROM remote_profiles WHERE profile_id=?",
                ("profile-a",),
            ).fetchone()[0]
        )
        with self.assertRaises(BindingError):
            self.store.record_verified_identity("profile-a", "account-a", NOW)

        self.store.record_verified_identity("profile-a", "identity-a", NOW)
        self.assertEqual(
            self.store.connection.execute(
                "SELECT verified_identity_ref FROM remote_profiles WHERE profile_id=?",
                ("profile-a",),
            ).fetchone()[0],
            "identity-a",
        )

    def test_schema_contains_no_runtime_or_secret_authority_fields(self):
        sql = " ".join(
            row[0] or "" for row in self.store.connection.execute(
                "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
            )
        ).lower()
        for forbidden in (
            "password", "session", "token", "cookie", "telegram", "pid",
            "mutex", "lease", "task scheduler", "quick tunnel",
        ):
            self.assertNotIn(forbidden, sql)

    def test_profile_a_b_isolation_and_composite_client_ownership(self):
        self.seed("profile-a", "host-a", "account-a")
        self.seed("profile-b", "host-b", "account-b")
        for profile, group in (("profile-a", "A"), ("profile-b", "B")):
            self.store.upsert_client(profile, "same-client", group, group, "running", "fixture")
            self.store.upsert_schedule(profile, "same-schedule", group, "04:00", "08:00", True)
            self.store.upsert_policy(profile, "same-policy", "mode", group)
            self.store.add_observation(profile, "same-observation", NOW, "fixture", "state", group)
            self.store.add_audit_event(profile, "same-event", NOW, "fixture", "test", group, "fixture")
        self.assertEqual(self.store.list_clients("profile-a")[0]["group_name"], "A")
        self.assertEqual(self.store.list_clients("profile-b")[0]["group_name"], "B")
        self.assertEqual(self.store.list_schedules("profile-a")[0]["group_name"], "A")
        self.assertEqual(self.store.list_policies("profile-b")[0]["policy_value"], "B")
        self.assertEqual(self.store.list_observations("profile-a")[0]["observation_value"], "A")
        self.assertEqual(self.store.list_audit_events("profile-b")[0]["summary"], "B")
        export_a = json.dumps(self.store.export_profile("profile-a"), sort_keys=True)
        self.assertIn("profile-a", export_a)
        self.assertNotIn("profile-b", export_a)
        self.assertNotIn('"B"', export_a)

    def test_importing_a_then_b_does_not_overwrite_either_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortableDomainStore.create(Path(directory) / "portable.sqlite3")
            try:
                for profile, host, account, name in (
                    ("profile-a", "host-a", "account-a", "A"),
                    ("profile-b", "host-b", "account-b", "B"),
                ):
                    import_shadow(
                        store,
                        GoldenSnapshot(
                            clients=({"client": "shared", "name": name, "group": name, "status": "running"},),
                            schedules=({"id": "shared", "group": name, "open": "04:00", "close": "08:00"},),
                            policies=({"id": "shared", "key": "mode", "value": name},),
                        ),
                        LegacyBinding(host, profile, account, "binding-" + profile),
                        NOW,
                    )
                self.assertEqual(store.list_clients("profile-a")[0]["display_name"], "A")
                self.assertEqual(store.list_clients("profile-b")[0]["display_name"], "B")
                self.assertEqual(store.list_schedules("profile-a")[0]["group_name"], "A")
                self.assertEqual(store.list_policies("profile-b")[0]["policy_value"], "B")
            finally:
                store.close()

    def test_cycle_stopped_is_profile_scoped(self):
        self.seed("profile-a", "host-a", "account-a")
        self.seed("profile-b", "host-b", "account-b")
        self.store.upsert_cycle_stopped("profile-a", "stop-a", "REQUESTED", NOW, "test", "fixture")
        self.assertEqual(self.store.list_control_intents("profile-a")[0]["state"], "REQUESTED")
        self.assertEqual(self.store.list_control_intents("profile-b"), [])

    def test_missing_profile_scope_fails_closed(self):
        for method, args in (
            (self.store.list_clients, ()), (self.store.list_schedules, ()),
            (self.store.list_policies, ()), (self.store.list_control_intents, ()),
            (self.store.list_observations, ()), (self.store.list_audit_events, ()),
        ):
            with self.subTest(method=method.__name__), self.assertRaises(TypeError):
                method(*args)
        with self.assertRaises(ProfileRequiredError):
            self.store.list_clients("")

    def test_binding_account_mismatch_and_active_host_conflict_are_rejected(self):
        self.seed("profile-a", "host-a", "account-a")
        self.store.add_profile("profile-b", "profile-b", "account-b", "VERIFIED", NOW)
        self.store.add_host("host-b", "host-b", "explicit-test-origin", NOW)
        with self.assertRaises(BindingError):
            self.store.bind_profile("wrong-account", "host-a", "profile-b", "account-a", 2, "OFFLINE", NOW)
        with self.assertRaises(BindingError):
            self.store.bind_profile("second-active", "host-a", "profile-b", "account-b", 2, "ACTIVE", NOW)
        with self.assertRaises(BindingError):
            self.store.bind_profile("profile-active-twice", "host-b", "profile-a", "account-a", 2, "ACTIVE", NOW)

    def test_backup_manifest_and_reopen_preserve_data(self):
        self.seed("profile-a", "host-a", "account-a")
        self.store.upsert_client("profile-a", "client-a", "A", "A", "running", "fixture")
        backup, manifest = self.store.backup_to(Path(self.temp.name) / "backup.sqlite3")
        self.assertEqual(manifest["sha256"], hashlib.sha256(backup.read_bytes()).hexdigest())
        self.assertTrue(backup.with_suffix(".sqlite3.manifest.json").is_file())
        self.store.close()
        reopened = PortableDomainStore.open(backup)
        try:
            self.assertEqual(reopened.list_clients("profile-a")[0]["client_id"], "client-a")
        finally:
            reopened.close()


class ShadowImporterTests(unittest.TestCase):
    def test_read_only_import_requires_explicit_binding_and_maps_stop_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "client_database.json").write_text(json.dumps({
                "clients": [{"client": "client-a", "name": "A", "group": "HAMI", "status": "running"}],
                "schedule": [{"group": "HAMI", "open": "04:00", "close": "08:00"}],
            }), encoding="utf-8")
            (root / "cycle_stopped.flag").write_text("stopped\n", encoding="utf-8")
            source = FileSystemGoldenSource(root, master_data="missing-master.json")
            before = hashlib.sha256((root / "client_database.json").read_bytes()).hexdigest()
            snapshot = source.snapshot()
            self.assertTrue(snapshot.cycle_stopped)
            store = PortableDomainStore.create(root / "portable.sqlite3")
            try:
                result = import_shadow(store, snapshot, LegacyBinding(
                    "host-a", "profile-a", "account-a", "binding-a"), NOW)
                self.assertEqual(result["clients"], 1)
                self.assertEqual(store.list_control_intents("profile-a")[0]["intent_type"], "cycle_stopped")
                self.assertEqual(hashlib.sha256((root / "client_database.json").read_bytes()).hexdigest(), before)
            finally:
                store.close()

    def test_empty_stop_flag_file_is_still_a_stop_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cycle_stopped.flag").touch()
            snapshot = FileSystemGoldenSource(root, master_data="missing-master.json").snapshot()
            self.assertTrue(snapshot.cycle_stopped)

    def test_import_same_profile_is_idempotent_and_updates_only_that_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "portable.sqlite3"
            store = PortableDomainStore.create(path)
            try:
                import_shadow(
                    store,
                    GoldenSnapshot(clients=({"client": "shared", "name": "old"},)),
                    LegacyBinding("host-a", "profile-a", "account-a", "binding-a"), NOW,
                )
                import_shadow(
                    store,
                    GoldenSnapshot(clients=({"client": "shared", "name": "new"},)),
                    LegacyBinding("host-a", "profile-a", "account-a", "binding-a"), NOW,
                )
                self.assertEqual(store.list_clients("profile-a")[0]["display_name"], "new")
            finally:
                store.close()

    def test_import_reconciles_deleted_rows_without_touching_profile_b(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortableDomainStore.create(Path(directory) / "portable.sqlite3")
            try:
                for profile, host, account in (
                    ("profile-a", "host-a", "account-a"),
                    ("profile-b", "host-b", "account-b"),
                ):
                    import_shadow(
                        store,
                        GoldenSnapshot(
                            clients=({"client": "keep", "name": profile}, {"client": "remove", "name": "stale"}),
                            schedules=({"id": "keep", "group": profile, "open": "04:00", "close": "08:00"},
                                       {"id": "remove", "group": "stale", "open": "08:00", "close": "12:00"}),
                            policies=({"id": "keep", "key": "keep", "value": profile},
                                      {"id": "remove", "key": "remove", "value": "stale"}),
                        ),
                        LegacyBinding(host, profile, account, "binding-" + profile), NOW,
                    )
                import_shadow(
                    store,
                    GoldenSnapshot(
                        clients=({"client": "keep", "name": "updated"},),
                        schedules=({"id": "keep", "group": "profile-a", "open": "05:00", "close": "09:00"},),
                        policies=({"id": "keep", "key": "keep", "value": "updated"},),
                    ),
                    LegacyBinding("host-a", "profile-a", "account-a", "binding-profile-a"), NOW,
                )
                self.assertEqual([row["client_id"] for row in store.list_clients("profile-a")], ["keep"])
                self.assertEqual([row["schedule_id"] for row in store.list_schedules("profile-a")], ["keep"])
                self.assertEqual([row["policy_id"] for row in store.list_policies("profile-a")], ["keep"])
                self.assertEqual([row["client_id"] for row in store.list_clients("profile-b")], ["keep", "remove"])
                self.assertEqual([row["schedule_id"] for row in store.list_schedules("profile-b")], ["keep", "remove"])
                self.assertEqual([row["policy_id"] for row in store.list_policies("profile-b")], ["keep", "remove"])
            finally:
                store.close()

    def test_invalid_snapshot_rolls_back_host_binding_and_domain_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortableDomainStore.create(Path(directory) / "portable.sqlite3")
            try:
                invalid = GoldenSnapshot(
                    clients=({"client": "valid", "name": "valid"}, {"name": "missing-id"}),
                    schedules=({"id": "schedule", "group": "A", "open": "04:00", "close": "08:00"},),
                )
                with self.assertRaises(ShadowImportError):
                    import_shadow(
                        store, invalid,
                        LegacyBinding("host-a", "profile-a", "account-a", "binding-a"), NOW,
                    )
                for table in ("hosts", "remote_profiles", "host_profile_bindings", "profile_clients",
                              "profile_schedules", "profile_policies", "profile_control_intents",
                              "profile_observations", "profile_audit_events"):
                    self.assertEqual(store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
            finally:
                store.close()

    def test_resume_clears_only_legacy_stop_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flag_a = root / "cycle_stopped.flag"
            flag_a.touch()
            store = PortableDomainStore.create(root / "portable.sqlite3")
            try:
                binding_a = LegacyBinding("host-a", "profile-a", "account-a", "binding-a")
                import_shadow(store, FileSystemGoldenSource(root, master_data="missing.json").snapshot(), binding_a, NOW)
                store.upsert_cycle_stopped("profile-a", "future-user-stop", "REQUESTED", NOW,
                                           "user:manual", "future user intent")
                root_b = root / "b"
                root_b.mkdir()
                (root_b / "cycle_stopped.flag").touch()
                binding_b = LegacyBinding("host-b", "profile-b", "account-b", "binding-b")
                import_shadow(store, FileSystemGoldenSource(root_b, master_data="missing.json").snapshot(), binding_b, NOW)
                flag_a.unlink()
                import_shadow(store, FileSystemGoldenSource(root, master_data="missing.json").snapshot(), binding_a, NOW)
                intents = {row["intent_id"]: row["state"] for row in store.list_control_intents("profile-a")}
                self.assertEqual(intents["legacy-cycle-stopped"], "CLEARED")
                self.assertEqual(intents["future-user-stop"], "REQUESTED")
                self.assertEqual(store.list_control_intents("profile-b")[0]["state"], "REQUESTED")
            finally:
                store.close()

    def test_import_does_not_infer_binding_or_accept_secret_account_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            source = GoldenSnapshot(clients=({"client": "a"},))
            store = PortableDomainStore.create(Path(directory) / "portable.sqlite3")
            try:
                with self.assertRaises(ShadowImportError):
                    import_shadow(store, source, LegacyBinding("h", "p", "session-token", "b"), NOW)
            finally:
                store.close()

    def test_source_rejects_path_escape(self):
        with self.assertRaises(ShadowImportError):
            FileSystemGoldenSource(".", client_database="../client_database.json")


if __name__ == "__main__":
    unittest.main()
