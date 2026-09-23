from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Protocol

from repositories.legacy_authority_schema import (
    SCHEMA_CHECKSUM,
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM,
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_IDENTITY,
    V3_SCHEMA_CHECKSUM,
    V3_SCHEMA_IDENTITY,
    V4_SCHEMA_CHECKSUM,
    V4_SCHEMA_IDENTITY,
    V4_UNIQUE_INDEX_SQL,
    V5_OBSERVATION_SQL,
    schema_identity,
)
from repositories.legacy_authority_store_types import LegacyAuthorityStoreError


class _MigrationHost(Protocol):
    connection: sqlite3.Connection

    def _immediate(self) -> AbstractContextManager[None]: ...

    def schema_metadata(self) -> Mapping[str, str]: ...

    def _validate(self) -> None: ...

    def _migrate_v3_locked(self, metadata: Mapping[str, str]) -> None: ...

    def _migrate_v4_locked(self, metadata: Mapping[str, str]) -> None: ...

    def _rebuild_transitional_handoffs_locked(self) -> None: ...

    def _initialize_observation_sequences(self) -> None: ...


def _execute_schema_script(connection: sqlite3.Connection, sql: str) -> None:
    statement = ""
    for line in sql.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise LegacyAuthorityStoreError("incomplete LEGACY schema statement")


class AuthoritySchemaMigrationMixin:
    def _prepare_schema(self: _MigrationHost) -> None:
        self.connection.execute("PRAGMA foreign_keys = OFF")
        self.connection.execute("PRAGMA legacy_alter_table = ON")
        try:
            with self._immediate():
                metadata = self.schema_metadata()
                version = metadata.get("schema_version")
                if version == "3":
                    self._migrate_v3_locked(metadata)
                elif version == "4":
                    self._migrate_v4_locked(metadata)
                elif version == "5":
                    self._validate()
                else:
                    raise LegacyAuthorityStoreError("LEGACY authority schema metadata mismatch")
        finally:
            self.connection.execute("PRAGMA legacy_alter_table = OFF")
            self.connection.execute("PRAGMA foreign_keys = ON")

    def _migrate_v3_locked(self: _MigrationHost, metadata: Mapping[str, str]) -> None:
        required_metadata = {"store_kind": "legacy_canary_authority", "schema_version": "3"}
        if any(metadata.get(key) != value for key, value in required_metadata.items()):
            raise LegacyAuthorityStoreError("LEGACY authority v3 metadata mismatch")
        if metadata.get("restore_state") not in ("canonical", "quarantined"):
            raise LegacyAuthorityStoreError("LEGACY authority restore state mismatch")
        current_identity = schema_identity(self.connection)
        checksum = metadata.get("schema_checksum")
        is_exact_v3 = checksum == V3_SCHEMA_CHECKSUM and current_identity == V3_SCHEMA_IDENTITY
        is_transitional_inline = checksum == TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM and current_identity == TRANSITIONAL_INLINE_UNIQUE_SCHEMA_IDENTITY
        if not is_exact_v3 and not is_transitional_inline:
            raise LegacyAuthorityStoreError("LEGACY authority v3 schema identity mismatch")
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LegacyAuthorityStoreError("LEGACY authority integrity check failed")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LegacyAuthorityStoreError("LEGACY authority foreign key check failed")
        duplicate = self.connection.execute("SELECT envelope_fingerprint FROM handoffs GROUP BY envelope_fingerprint HAVING COUNT(*) > 1 LIMIT 1").fetchone()
        if duplicate is not None:
            raise LegacyAuthorityStoreError("LEGACY authority v3 contains duplicate envelope fingerprints")
        if is_transitional_inline:
            self._rebuild_transitional_handoffs_locked()
        self.connection.execute(V4_UNIQUE_INDEX_SQL)
        _execute_schema_script(self.connection, V5_OBSERVATION_SQL)
        self._initialize_observation_sequences()
        self.connection.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", ("5",))
        self.connection.execute("UPDATE schema_meta SET value=? WHERE key='schema_checksum'", (SCHEMA_CHECKSUM,))
        self._validate()

    def _migrate_v4_locked(self: _MigrationHost, metadata: Mapping[str, str]) -> None:
        required_metadata = {"store_kind": "legacy_canary_authority", "schema_version": "4", "schema_checksum": V4_SCHEMA_CHECKSUM}
        if any(metadata.get(key) != value for key, value in required_metadata.items()):
            raise LegacyAuthorityStoreError("LEGACY authority v4 metadata mismatch")
        if schema_identity(self.connection) != V4_SCHEMA_IDENTITY:
            raise LegacyAuthorityStoreError("LEGACY authority v4 schema identity mismatch")
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LegacyAuthorityStoreError("LEGACY authority integrity check failed")
        _validate_v4_lineage(self.connection)
        _execute_schema_script(self.connection, V5_OBSERVATION_SQL)
        self._initialize_observation_sequences()
        self.connection.execute("UPDATE schema_meta SET value=? WHERE key='schema_version'", ("5",))
        self.connection.execute("UPDATE schema_meta SET value=? WHERE key='schema_checksum'", (SCHEMA_CHECKSUM,))
        self._validate()
    def _initialize_observation_sequences(self: _MigrationHost) -> None:
        invalid_boundary = self.connection.execute(
            "SELECT 1 FROM receipts WHERE "
            "(post_dispatch_observation_boundary_id IS NULL) <> "
            "(post_dispatch_observation_generation_floor IS NULL) OR "
            "(post_dispatch_observation_boundary_id IS NOT NULL AND "
            "(post_dispatch_observation_generation_floor <= pre_operation_observation_generation_floor OR state <> 'applied')) LIMIT 1"
        ).fetchone()
        if invalid_boundary is not None:
            raise LegacyAuthorityStoreError("LEGACY authority contains an invalid observation boundary")
        floors = self.connection.execute(
            "SELECT authority_epoch,post_dispatch_observation_generation_floor FROM receipts "
            "WHERE post_dispatch_observation_boundary_id IS NOT NULL "
            "ORDER BY authority_epoch,fence_counter"
        ).fetchall()
        previous_by_epoch: dict[str, int] = {}
        for row in floors:
            epoch = str(row[0])
            floor = int(row[1])
            previous = previous_by_epoch.get(epoch)
            if previous is not None and floor <= previous:
                raise LegacyAuthorityStoreError("LEGACY authority observation floors are not ordered")
            previous_by_epoch[epoch] = floor
        self.connection.execute("INSERT INTO observation_generation_sequence(key,last_generation_id,last_record_digest) VALUES ('global',0,'')")
        self.connection.execute("INSERT INTO observation_boundary_sequence(key,last_generation_floor) VALUES ('global',0)")
        self.connection.execute(
            "INSERT INTO observation_boundaries(boundary_id,pre_send_identity,generation_floor,allocated_after_terminal) "
            "SELECT post_dispatch_observation_boundary_id,pre_send_identity,post_dispatch_observation_generation_floor,1 "
            "FROM receipts WHERE post_dispatch_observation_boundary_id IS NOT NULL AND post_dispatch_observation_generation_floor IS NOT NULL"
        )
        self.connection.execute(
            "UPDATE observation_boundary_sequence SET last_generation_floor=COALESCE((SELECT MAX(generation_floor) FROM observation_boundaries),0) WHERE key='global'"
        )

    def _rebuild_transitional_handoffs_locked(self: _MigrationHost) -> None:
        table_sql_row = self.connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='handoffs'").fetchone()
        trigger_rows = self.connection.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name='handoffs' ORDER BY name").fetchall()
        if table_sql_row is None or len(trigger_rows) != 2:
            raise LegacyAuthorityStoreError("LEGACY authority transitional handoff schema mismatch")
        canonical_table_sql = str(table_sql_row["sql"]).replace("envelope_fingerprint TEXT NOT NULL UNIQUE", "envelope_fingerprint TEXT NOT NULL")
        trigger_sql = tuple(str(row["sql"]) for row in trigger_rows)
        self.connection.execute("DROP TRIGGER handoffs_immutable_delete")
        self.connection.execute("DROP TRIGGER handoffs_immutable_update")
        self.connection.execute("ALTER TABLE handoffs RENAME TO handoffs_inline_old")
        self.connection.execute(canonical_table_sql)
        self.connection.execute("INSERT INTO handoffs SELECT * FROM handoffs_inline_old")
        self.connection.execute("DROP TABLE handoffs_inline_old")
        for statement in trigger_sql:
            self.connection.execute(statement)


def _validate_v4_lineage(connection: sqlite3.Connection) -> None:
    mismatched_artifact = connection.execute(
        """
        SELECT 1
        FROM authorization_artifacts AS artifact
        JOIN handoffs AS handoff ON handoff.handoff_id = artifact.handoff_id
        WHERE artifact.pre_send_identity <> handoff.pre_send_identity
           OR artifact.canary_idempotency_key <> handoff.canary_idempotency_key
           OR artifact.authority_epoch <> handoff.authority_epoch
           OR artifact.fence_counter <> handoff.fence_counter
           OR artifact.envelope_exporter_identity <> handoff.exporter_identity
           OR artifact.source_envelope_contract_version <> handoff.source_envelope_contract_version
           OR artifact.transport_contract_version <> handoff.transport_contract_version
           OR artifact.operation_kind <> handoff.operation_kind
           OR artifact.target_ref <> handoff.target_ref
           OR artifact.envelope_fingerprint <> handoff.envelope_fingerprint
        LIMIT 1
        """
    ).fetchone()
    if mismatched_artifact is not None:
        raise LegacyAuthorityStoreError("LEGACY authority v4 artifact lineage mismatch")
    mismatched_fence = connection.execute(
        """
        SELECT 1
        FROM fences AS fence
        JOIN authorization_artifacts AS artifact
          ON artifact.pre_send_identity = fence.pre_send_identity
        WHERE fence.canary_run_id <> artifact.canary_run_id
           OR fence.fence_identity <> artifact.fence_identity
           OR fence.authority_epoch <> artifact.authority_epoch
           OR fence.fence_counter <> artifact.fence_counter
        LIMIT 1
        """
    ).fetchone()
    if mismatched_fence is not None:
        raise LegacyAuthorityStoreError("LEGACY authority v4 fence lineage mismatch")
    mismatched_receipt = connection.execute(
        """
        SELECT 1
        FROM receipts AS receipt
        JOIN authorization_artifacts AS artifact
          ON artifact.pre_send_identity = receipt.pre_send_identity
        WHERE receipt.canary_idempotency_key <> artifact.canary_idempotency_key
           OR receipt.handoff_id <> artifact.handoff_id
           OR receipt.envelope_fingerprint <> artifact.envelope_fingerprint
           OR receipt.canary_run_id <> artifact.canary_run_id
           OR receipt.fence_identity <> artifact.fence_identity
           OR receipt.authority_epoch <> artifact.authority_epoch
           OR receipt.fence_counter <> artifact.fence_counter
           OR receipt.source_identity_ref <> artifact.source_identity_ref
           OR receipt.artifact_producer_identity <> artifact.artifact_producer_identity
           OR receipt.envelope_exporter_identity <> artifact.envelope_exporter_identity
           OR receipt.source_envelope_contract_version <> artifact.source_envelope_contract_version
           OR receipt.transport_contract_version <> artifact.transport_contract_version
           OR receipt.contract_version <> artifact.contract_version
           OR receipt.target_ref <> artifact.target_ref
           OR receipt.authorization_artifact_fingerprint <> artifact.authorization_artifact_fingerprint
           OR receipt.pre_operation_observation_generation_floor <> artifact.pre_operation_observation_generation_floor
        LIMIT 1
        """
    ).fetchone()
    if mismatched_receipt is not None:
        raise LegacyAuthorityStoreError("LEGACY authority v4 receipt lineage mismatch")
