from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Final

from repositories.legacy_authority_authorization import AuthorizationPersistenceMixin
from repositories.legacy_authority_fence import FenceLifecycleMixin, TransitionError
from repositories.legacy_authority_receipt import ReceiptLifecycleMixin
from repositories.legacy_authority_schema import (
    SCHEMA_CHECKSUM,
    SCHEMA_IDENTITY,
    SCHEMA_SQL,
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM,
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_IDENTITY,
    V3_SCHEMA_CHECKSUM,
    V3_SCHEMA_IDENTITY,
    V4_UNIQUE_INDEX_SQL,
    schema_identity,
)
from repositories.legacy_authority_store_types import (
    LegacyAuthorityStoreError,
    ReceiptConflictError,
    StoreQuarantinedError,
)
from repositories.legacy_authority_transaction import ImmediateTransaction
from repositories.legacy_authority_types import LegacyValidationError

__all__ = (
    "LegacyAuthorityStore",
    "ReceiptConflictError",
    "TransitionError",
    "legacy_store_path",
)

STORE_KIND: Final = "legacy_canary_authority"
SCHEMA_VERSION: Final = 4
DB_FILENAME: Final = "legacy_canary.sqlite3"

def legacy_store_path(runtime_root: PurePath) -> Path:
    rendered = str(runtime_root).replace("\\", "/")
    lowered = rendered.casefold()
    if lowered.startswith("d:/") or "p4_operational.sqlite3" in lowered:
        raise LegacyValidationError("LEGACY authority store path is forbidden")
    return Path(runtime_root) / "legacy" / DB_FILENAME


def _deny_cross_database(action: int, _arg1: str | None, _arg2: str | None, _db: str | None, _source: str | None) -> int:
    return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK


class LegacyAuthorityStore(
    AuthorizationPersistenceMixin,
    FenceLifecycleMixin,
    ReceiptLifecycleMixin,
):
    def __init__(self, connection: sqlite3.Connection, path: Path, quarantined: bool = False) -> None:
        self.connection = connection
        self.path = path
        self._quarantined = quarantined

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.set_authorizer(_deny_cross_database)
            connection.execute("PRAGMA foreign_keys = ON")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).casefold() != "wal":
                raise LegacyAuthorityStoreError(
                    "LEGACY authority store requires WAL"
                )
            connection.execute("PRAGMA synchronous = NORMAL")
        except (sqlite3.DatabaseError, LegacyAuthorityStoreError):
            connection.close()
            raise
        return connection

    @classmethod
    def create(cls, path: Path) -> LegacyAuthorityStore:
        cls._require_safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = cls._connect(path)
        store = cls(connection, path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.executemany(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (
                    ("store_kind", STORE_KIND),
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("schema_checksum", SCHEMA_CHECKSUM),
                    ("restore_state", "canonical"),
                    ("restore_marker", ""),
                ),
            )
            store._validate()
        except (sqlite3.DatabaseError, LegacyAuthorityStoreError):
            connection.close()
            raise
        return store

    @classmethod
    def open(cls, path: Path) -> LegacyAuthorityStore:
        cls._require_safe_path(path)
        store = cls(cls._connect(path), path)
        try:
            store._prepare_schema()
        except (sqlite3.DatabaseError, LegacyAuthorityStoreError):
            store.close()
            raise
        store._quarantined = store.schema_metadata()["restore_state"] == "quarantined"
        return store

    @classmethod
    def open_restored(cls, path: Path, restore_marker: str) -> LegacyAuthorityStore:
        if not restore_marker or restore_marker != restore_marker.strip():
            raise LegacyValidationError("restore_marker must be a non-empty canonical value")
        store = cls.open(path)
        if store._quarantined:
            store.close()
            raise StoreQuarantinedError("LEGACY authority store is already quarantined")
        with store._immediate():
            store.connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='restore_state'",
                ("quarantined",),
            )
            store.connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='restore_marker'",
                (restore_marker,),
            )
        store._quarantined = True
        return store

    @staticmethod
    def _require_safe_path(path: Path) -> None:
        rendered = str(path).replace("\\", "/").casefold()
        if rendered.startswith("d:/") or path.name != DB_FILENAME or path.parent.name.casefold() != "legacy" or "p4_operational" in rendered:
            raise LegacyValidationError("invalid LEGACY authority store path")

    def close(self) -> None:
        self.connection.close()

    def _validate(self) -> None:
        metadata = self.schema_metadata()
        required_metadata = {
            "store_kind": STORE_KIND,
            "schema_version": str(SCHEMA_VERSION),
            "schema_checksum": SCHEMA_CHECKSUM,
        }
        if any(metadata.get(key) != value for key, value in required_metadata.items()):
            raise LegacyAuthorityStoreError("LEGACY authority schema metadata mismatch")
        if metadata.get("restore_state") not in ("canonical", "quarantined"):
            raise LegacyAuthorityStoreError("LEGACY authority restore state mismatch")
        if schema_identity(self.connection) != SCHEMA_IDENTITY:
            raise LegacyAuthorityStoreError("LEGACY authority schema identity mismatch")
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LegacyAuthorityStoreError("LEGACY authority integrity check failed")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LegacyAuthorityStoreError("LEGACY authority foreign key check failed")

    def _prepare_schema(self) -> None:
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            with self._immediate():
                metadata = self.schema_metadata()
                version = metadata.get("schema_version")
                if version == "3":
                    self._migrate_v3_locked(metadata)
                elif version == str(SCHEMA_VERSION):
                    self._validate()
                else:
                    raise LegacyAuthorityStoreError(
                        "LEGACY authority schema metadata mismatch"
                    )
        finally:
            self.connection.execute("PRAGMA legacy_alter_table = OFF")
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v3_locked(self, metadata: Mapping[str, str]) -> None:
        required_metadata = {
            "store_kind": STORE_KIND,
            "schema_version": "3",
        }
        if any(metadata.get(key) != value for key, value in required_metadata.items()):
            raise LegacyAuthorityStoreError("LEGACY authority v3 metadata mismatch")
        if metadata.get("restore_state") not in ("canonical", "quarantined"):
            raise LegacyAuthorityStoreError("LEGACY authority restore state mismatch")
        current_identity = schema_identity(self.connection)
        checksum = metadata.get("schema_checksum")
        is_exact_v3 = (
            checksum == V3_SCHEMA_CHECKSUM and current_identity == V3_SCHEMA_IDENTITY
        )
        is_transitional_inline = (
            checksum == TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM
            and current_identity == TRANSITIONAL_INLINE_UNIQUE_SCHEMA_IDENTITY
        )
        if not is_exact_v3 and not is_transitional_inline:
            raise LegacyAuthorityStoreError("LEGACY authority v3 schema identity mismatch")
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LegacyAuthorityStoreError("LEGACY authority integrity check failed")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LegacyAuthorityStoreError("LEGACY authority foreign key check failed")
        duplicate = self.connection.execute(
            "SELECT envelope_fingerprint FROM handoffs "
            "GROUP BY envelope_fingerprint HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise LegacyAuthorityStoreError(
                "LEGACY authority v3 contains duplicate envelope fingerprints"
            )
        if is_transitional_inline:
            self._rebuild_transitional_handoffs_locked()
        self.connection.execute(V4_UNIQUE_INDEX_SQL)
        self.connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_version'",
            (str(SCHEMA_VERSION),),
        )
        self.connection.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
            (SCHEMA_CHECKSUM,),
        )
        self._validate()

    def _rebuild_transitional_handoffs_locked(self) -> None:
        table_sql_row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='handoffs'"
        ).fetchone()
        trigger_rows = self.connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
            "AND tbl_name='handoffs' ORDER BY name"
        ).fetchall()
        if table_sql_row is None or len(trigger_rows) != 2:
            raise LegacyAuthorityStoreError(
                "LEGACY authority transitional handoff schema mismatch"
            )
        canonical_table_sql = str(table_sql_row["sql"]).replace(
            "envelope_fingerprint TEXT NOT NULL UNIQUE",
            "envelope_fingerprint TEXT NOT NULL",
        )
        trigger_sql = tuple(str(row["sql"]) for row in trigger_rows)
        self.connection.execute("DROP TRIGGER handoffs_immutable_delete")
        self.connection.execute("DROP TRIGGER handoffs_immutable_update")
        self.connection.execute("ALTER TABLE handoffs RENAME TO handoffs_inline_old")
        self.connection.execute(canonical_table_sql)
        self.connection.execute(
            "INSERT INTO handoffs SELECT * FROM handoffs_inline_old"
        )
        self.connection.execute("DROP TABLE handoffs_inline_old")
        for statement in trigger_sql:
            self.connection.execute(statement)

    def schema_metadata(self) -> Mapping[str, str]:
        return {row["key"]: row["value"] for row in self.connection.execute("SELECT key, value FROM schema_meta")}

    def _immediate(self) -> ImmediateTransaction:
        return ImmediateTransaction(self.connection)

    def _artifact(self, identity: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM authorization_artifacts WHERE pre_send_identity=?", (identity,)).fetchone()
        if row is None:
            raise TransitionError("authorization artifact is missing")
        return row

    def _require_live(self) -> None:
        if self._quarantined:
            raise StoreQuarantinedError("LEGACY authority store is quarantined")
