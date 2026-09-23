from __future__ import annotations

import hmac
import sqlite3
import time
from collections.abc import Mapping
from pathlib import Path, PurePath
from typing import Final

from services.legacy_handoff_trust import HmacKeyProvider
from services.legacy_observation_trust import observation_attestation

from repositories.legacy_authority_authorization import AuthorizationPersistenceMixin
from repositories.legacy_authority_fence import FenceLifecycleMixin, TransitionError
from repositories.legacy_authority_migration import (
    AuthoritySchemaMigrationMixin,
    _execute_schema_script,
)
from repositories.legacy_authority_observation import (
    ObservationEvidence,
    ObservationPersistenceMixin,
)
from repositories.legacy_authority_receipt import ReceiptLifecycleMixin
from repositories.legacy_authority_schema import (
    SCHEMA_CHECKSUM,
    SCHEMA_IDENTITY,
    SCHEMA_SQL,
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
    "LegacyAuthorityStoreFactory",
    "ReceiptConflictError",
    "TransitionError",
    "legacy_store_path",
)

STORE_KIND: Final = "legacy_canary_authority"
SCHEMA_VERSION: Final = 5
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
    AuthoritySchemaMigrationMixin,
    AuthorizationPersistenceMixin,
    FenceLifecycleMixin,
    ReceiptLifecycleMixin,
    ObservationPersistenceMixin,
):
    def __init__(self, connection: sqlite3.Connection, path: Path, quarantined: bool = False, observation_identity: str | None = None, observation_key_provider: HmacKeyProvider | None = None) -> None:
        self.connection = connection
        self.path = path
        self._quarantined = quarantined
        self._observation_identity = observation_identity
        self._observation_key_provider = observation_key_provider

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.set_authorizer(_deny_cross_database)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            mode = ""
            for _attempt in range(100):
                try:
                    mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
                    break
                except sqlite3.OperationalError as error:
                    if "locked" not in str(error).casefold() or _attempt == 99:
                        raise
                    time.sleep(0.05)
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
        return cls._create(path, None, None)

    @classmethod
    def _create(cls, path: Path, observation_identity: str | None, observation_key_provider: HmacKeyProvider | None) -> LegacyAuthorityStore:
        cls._require_safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = cls._connect(path)
        store = cls(connection, path, observation_identity=observation_identity, observation_key_provider=observation_key_provider)
        try:
            with ImmediateTransaction(connection):
                objects = connection.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type IN ('table','index','trigger','view') "
                    "AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                if not objects:
                    _execute_schema_script(connection, SCHEMA_SQL)
                    connection.execute(
                        "INSERT INTO observation_generation_sequence(key,last_generation_id,last_record_digest) VALUES ('global',0,'')"
                    )
                    connection.execute(
                        "INSERT INTO observation_boundary_sequence(key,last_generation_floor) VALUES ('global',0)"
                    )
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
        return cls._open(path, None, None)

    @classmethod
    def _open(cls, path: Path, observation_identity: str | None, observation_key_provider: HmacKeyProvider | None) -> LegacyAuthorityStore:
        cls._require_safe_path(path)
        store = cls(cls._connect(path), path, observation_identity=observation_identity, observation_key_provider=observation_key_provider)
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

    def _verify_observation_attestation(self, evidence: ObservationEvidence) -> str:
        if self._observation_identity is None or self._observation_key_provider is None:
            raise LegacyAuthorityStoreError("trusted observation verifier is not configured")
        expected = observation_attestation(evidence, self._observation_identity, self._observation_key_provider)
        if not hmac.compare_digest(evidence.attestation_fingerprint, expected):
            raise LegacyAuthorityStoreError("detached observation attestation is invalid")
        return expected


class LegacyAuthorityStoreFactory:
    def __init__(self, observation_identity: str, observation_key_provider: HmacKeyProvider) -> None:
        self._observation_identity = observation_identity
        self._observation_key_provider = observation_key_provider

    def create(self, path: Path) -> LegacyAuthorityStore:
        return LegacyAuthorityStore._create(path, self._observation_identity, self._observation_key_provider)

    def open(self, path: Path) -> LegacyAuthorityStore:
        return LegacyAuthorityStore._open(path, self._observation_identity, self._observation_key_provider)
