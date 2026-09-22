"""Durable, provenance-bound post-dispatch observation acknowledgements.

This module deliberately accepts an already-captured observation.  It does not
read a remote source or mutate the LEGACY runtime; the sole reader must provide
the snapshot provenance through an audited integration boundary.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from repositories.legacy_authority_types import Receipt, ReceiptState


class ObservationMaterializerError(ValueError):
    """Raised when an observation cannot be trusted or replayed safely."""


class ObservationMaterializerConflict(ObservationMaterializerError):
    """Raised when an existing ACK is replayed with different provenance."""


@dataclass(frozen=True, slots=True)
class ObservationEvidence:
    boundary_id: str
    generation_floor: int
    generation_id: int
    snapshot_id: str
    captured_at: str
    materializer_run_id: str
    source_hash: str
    source_identity_ref: str
    canary_run_id: str
    fence_identity: str
    fence_counter: int
    target_ref: str
    requested_state: str
    observed_state: str

    def __post_init__(self) -> None:
        for field in (
            "boundary_id",
            "snapshot_id",
            "materializer_run_id",
            "source_hash",
            "source_identity_ref",
            "canary_run_id",
            "fence_identity",
            "target_ref",
            "requested_state",
            "observed_state",
        ):
            _safe_reference(getattr(self, field), field)
        if type(self.captured_at) is not str:
            raise ObservationMaterializerError("captured_at must be an ISO timestamp")
        if type(self.generation_floor) is not int or type(self.generation_id) is not int:
            raise ObservationMaterializerError("generation values must be integers")
        if type(self.fence_counter) is not int:
            raise ObservationMaterializerError("fence_counter must be an integer")
        if self.generation_floor < 0 or self.generation_id <= self.generation_floor:
            raise ObservationMaterializerError(
                "generation_id must be strictly later than generation_floor"
            )
        if self.fence_counter < 1:
            raise ObservationMaterializerError("fence_counter must be positive")
        try:
            datetime.fromisoformat(self.captured_at.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as error:
            raise ObservationMaterializerError("captured_at must be an ISO timestamp") from error


@dataclass(frozen=True, slots=True)
class MaterializerAck:
    receipt_identity: str
    evidence: ObservationEvidence


ACK_SCHEMA_SQL = """
CREATE TABLE observation_materializer_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS observation_materializer_acks (
    receipt_identity TEXT PRIMARY KEY,
    boundary_id TEXT NOT NULL UNIQUE,
    generation_floor INTEGER NOT NULL,
    generation_id INTEGER NOT NULL,
    snapshot_id TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    materializer_run_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    source_identity_ref TEXT NOT NULL,
    canary_run_id TEXT NOT NULL,
    fence_identity TEXT NOT NULL,
    fence_counter INTEGER NOT NULL,
    target_ref TEXT NOT NULL,
    requested_state TEXT NOT NULL,
    observed_state TEXT NOT NULL,
    UNIQUE(generation_id),
    CHECK (generation_id > generation_floor),
    CHECK (fence_counter > 0)
);
CREATE TABLE observation_generation_sequence (
    key TEXT PRIMARY KEY,
    last_generation_id INTEGER NOT NULL CHECK (last_generation_id >= 0)
);
CREATE TRIGGER IF NOT EXISTS observation_ack_immutable_update
BEFORE UPDATE ON observation_materializer_acks
BEGIN SELECT RAISE(ABORT, 'observation ACK immutable'); END;
CREATE TRIGGER IF NOT EXISTS observation_ack_immutable_delete
BEFORE DELETE ON observation_materializer_acks
BEGIN SELECT RAISE(ABORT, 'observation ACK immutable'); END;
"""

ACK_STORE_KIND = "legacy_observation_materializer"
ACK_SCHEMA_VERSION = "1"
_SECRET_MARKERS = ("authorization", "cookie", "credential", "password", "secret", "session", "token", "http://", "https://")


def _safe_reference(value: object, field: str) -> None:
    if type(value) is not str or not value or value != value.strip():
        raise ObservationMaterializerError(f"{field} must be a canonical string")
    if any(marker in value.casefold() for marker in _SECRET_MARKERS):
        raise ObservationMaterializerError(f"{field} must be a non-secret reference")


def _schema_identity(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return json.dumps([tuple(row) for row in rows], separators=(",", ":"))


def _schema_checksum(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()


with sqlite3.connect(":memory:") as _schema_connection:
    _schema_connection.executescript(ACK_SCHEMA_SQL)
    ACK_SCHEMA_IDENTITY = _schema_identity(_schema_connection)
ACK_SCHEMA_CHECKSUM = _schema_checksum(ACK_SCHEMA_IDENTITY)


def initialize_ack_schema(connection: sqlite3.Connection) -> None:
    """Create or validate the append-only ACK relation on its own store."""
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    if not tables:
        connection.executescript(ACK_SCHEMA_SQL)
        connection.execute(
            "INSERT INTO observation_materializer_meta(key,value) VALUES (?,?)",
            ("store_kind", ACK_STORE_KIND),
        )
        connection.execute(
            "INSERT INTO observation_materializer_meta(key,value) VALUES (?,?)",
            ("schema_version", ACK_SCHEMA_VERSION),
        )
        connection.execute(
            "INSERT INTO observation_materializer_meta(key,value) VALUES (?,?)",
            ("schema_checksum", ACK_SCHEMA_CHECKSUM),
        )
        connection.execute(
            "INSERT INTO observation_materializer_meta(key,value) VALUES (?,?)",
            ("schema_identity", ACK_SCHEMA_IDENTITY),
        )
        connection.execute(
            "INSERT INTO observation_generation_sequence(key,last_generation_id) VALUES ('global',0)"
        )
        connection.commit()
        return
    expected_tables = {
        "observation_materializer_meta",
        "observation_materializer_acks",
        "observation_generation_sequence",
        "observation_ack_immutable_update",
        "observation_ack_immutable_delete",
    }
    if tables != expected_tables:
        raise ObservationMaterializerError("observation ACK store schema is not dedicated")
    metadata = dict(
        connection.execute("SELECT key,value FROM observation_materializer_meta")
    )
    if (
        metadata.get("store_kind") != ACK_STORE_KIND
        or metadata.get("schema_version") != ACK_SCHEMA_VERSION
        or metadata.get("schema_checksum") != ACK_SCHEMA_CHECKSUM
        or metadata.get("schema_identity") != ACK_SCHEMA_IDENTITY
        or _schema_identity(connection) != ACK_SCHEMA_IDENTITY
    ):
        raise ObservationMaterializerError("observation ACK store schema mismatch")
    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ObservationMaterializerError("observation ACK store integrity failed")


class ObservationMaterializer:
    """Persist and verify one immutable ACK per terminal receipt."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        initialize_ack_schema(connection)

    def record_ack(
        self,
        receipt: Receipt,
        evidence: ObservationEvidence,
    ) -> MaterializerAck:
        if receipt.state is not ReceiptState.APPLIED:
            raise ObservationMaterializerError(
                "materializer ACK requires a terminal applied receipt"
            )
        if receipt.post_dispatch_observation_boundary_id != evidence.boundary_id:
            raise ObservationMaterializerError("ACK boundary does not bind to receipt")
        if receipt.post_dispatch_observation_generation_floor != evidence.generation_floor:
            raise ObservationMaterializerError("ACK floor does not bind to receipt")
        if (
            receipt.source_identity_ref != evidence.source_identity_ref
            or receipt.target_ref != evidence.target_ref
            or receipt.requested_state != evidence.requested_state
            or receipt.requested_state != "running"
            or evidence.observed_state != evidence.requested_state
            or receipt.canary_run_id != evidence.canary_run_id
            or receipt.fence_identity != evidence.fence_identity
            or receipt.fence_counter != evidence.fence_counter
            or receipt.fence_state is None
            or receipt.fence_state.value
            not in ("receipt_terminal", "observation_pending")
        ):
            raise ObservationMaterializerError("ACK does not bind to receipt lineage")
        identity = receipt.pre_send_identity
        values = (
            identity,
            evidence.boundary_id,
            evidence.generation_floor,
            evidence.generation_id,
            evidence.snapshot_id,
            evidence.captured_at,
            evidence.materializer_run_id,
            evidence.source_hash,
            evidence.source_identity_ref,
            evidence.canary_run_id,
            evidence.fence_identity,
            evidence.fence_counter,
            evidence.target_ref,
            evidence.requested_state,
            evidence.observed_state,
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "SELECT * FROM observation_materializer_acks WHERE receipt_identity=?",
                (identity,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != values:
                    raise ObservationMaterializerConflict("observation ACK conflict")
                self.connection.commit()
                return MaterializerAck(identity, evidence)
            last_generation = self.connection.execute(
                "SELECT last_generation_id FROM observation_generation_sequence WHERE key='global'"
            ).fetchone()[0]
            if evidence.generation_id <= last_generation:
                raise ObservationMaterializerError("generation is not globally monotonic")
            self.connection.execute(
                "INSERT OR IGNORE INTO observation_materializer_acks "
                "(receipt_identity,boundary_id,generation_floor,generation_id,"
                "snapshot_id,captured_at,materializer_run_id,source_hash,"
                "source_identity_ref,canary_run_id,fence_identity,fence_counter,"
                "target_ref,requested_state,observed_state) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            inserted = self.connection.execute(
                "SELECT * FROM observation_materializer_acks WHERE receipt_identity=?",
                (identity,),
            ).fetchone()
            if inserted is None or tuple(inserted) != values:
                raise ObservationMaterializerConflict("observation ACK conflict")
            self.connection.execute(
                "UPDATE observation_generation_sequence SET last_generation_id=? WHERE key='global'",
                (evidence.generation_id,),
            )
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()
        return MaterializerAck(identity, evidence)

    def require_evidence(
        self,
        receipt: Receipt,
    ) -> MaterializerAck:
        """Return evidence only when every receipt binding matches exactly."""
        row = self.connection.execute(
            "SELECT * FROM observation_materializer_acks WHERE receipt_identity=?",
            (receipt.pre_send_identity,),
        ).fetchone()
        if row is None:
            raise ObservationMaterializerError("post-dispatch evidence is missing")
        if receipt.state is not ReceiptState.APPLIED:
            raise ObservationMaterializerError("receipt is not applied")
        if (
            row[1] != receipt.post_dispatch_observation_boundary_id
            or row[2] != receipt.post_dispatch_observation_generation_floor
            or row[8] != receipt.source_identity_ref
            or row[9] != receipt.canary_run_id
            or row[10] != receipt.fence_identity
            or row[11] != receipt.fence_counter
            or row[12] != receipt.target_ref
            or row[13] != receipt.requested_state
            or row[14] != receipt.requested_state
            or row[3] <= row[2]
            or receipt.fence_state is None
            or receipt.fence_state.value not in ("receipt_terminal", "observation_pending")
        ):
            raise ObservationMaterializerError("post-dispatch evidence binding mismatch")
        return MaterializerAck(
            receipt.pre_send_identity,
            ObservationEvidence(
                boundary_id=row[1], generation_floor=row[2], generation_id=row[3],
                snapshot_id=row[4], captured_at=row[5], materializer_run_id=row[6],
                source_hash=row[7], source_identity_ref=row[8], canary_run_id=row[9],
                fence_identity=row[10], fence_counter=row[11], target_ref=row[12],
                requested_state=row[13], observed_state=row[14],
            ),
        )
