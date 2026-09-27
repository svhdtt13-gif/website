"""Append-only durable ledger for runtime observation publications."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from services.runtime_observation_generation import (
    ObservationGenerationContractError,
    observation_binding_fingerprint,
    require_non_secret_source_identity,
)

LEDGER_FILENAME = "runtime_observation_publications.sqlite3"
LEDGER_SCHEMA_VERSION = 1


class ObservationLedgerError(RuntimeError):
    """Raised when durable publication history cannot prove its integrity."""


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """One immutable generation reservation or publication ledger record."""

    observation_generation: int
    state: str
    generation_id: str
    snapshot_id: str
    run_id: str
    candidate_path: str
    candidate_schema_version: int
    candidate_schema_checksum: str
    source_hash: str
    source_set: str
    source_identity_ref: str
    pre_send_identity: str | None
    canary_run_id: str | None
    fence_identity: str | None
    authority_epoch: str | None
    fence_counter: int | None
    target_ref: str | None
    requested_state: str | None
    captured_at: str
    completed_at: str | None
    observation_boundary_id: str | None
    observation_generation_floor: int | None
    observation_binding_fingerprint: str | None
    materializer_run_id: str | None
    observed_state: str | None
    attestation_fingerprint: str | None
    previous_record_digest: str
    record_digest: str
    failure_reason: str | None


@dataclass(frozen=True, slots=True)
class PublicationReservation:
    """Candidate facts fixed before a generation is reserved."""

    snapshot_id: str
    run_id: str
    candidate_path: str
    candidate_schema_version: int
    candidate_schema_checksum: str
    source_hash: str
    source_set: str
    source_identity_ref: str
    captured_at: str
    observation_boundary_id: str | None
    observation_generation_floor: int | None
    pre_send_identity: str | None = None
    canary_run_id: str | None = None
    fence_identity: str | None = None
    authority_epoch: str | None = None
    fence_counter: int | None = None
    target_ref: str | None = None
    requested_state: str | None = None
    observation_binding_fingerprint: str | None = None
    materializer_run_id: str | None = None
    observed_state: str | None = None
    attestation_fingerprint: str | None = None
    generation_id: str | None = None

    def mapping(self) -> dict[str, str | int | None]:
        return {
            "snapshot_id": self.snapshot_id,
            "run_id": self.run_id,
            "candidate_path": self.candidate_path,
            "candidate_schema_version": self.candidate_schema_version,
            "candidate_schema_checksum": self.candidate_schema_checksum,
            "source_hash": self.source_hash,
            "source_set": self.source_set,
            "source_identity_ref": self.source_identity_ref,
            "captured_at": self.captured_at,
            "observation_boundary_id": self.observation_boundary_id,
            "observation_generation_floor": self.observation_generation_floor,
            "pre_send_identity": self.pre_send_identity,
            "canary_run_id": self.canary_run_id,
            "fence_identity": self.fence_identity,
            "authority_epoch": self.authority_epoch,
            "fence_counter": self.fence_counter,
            "target_ref": self.target_ref,
            "requested_state": self.requested_state,
            "observation_binding_fingerprint": self.observation_binding_fingerprint,
            "materializer_run_id": self.materializer_run_id,
            "observed_state": self.observed_state,
            "attestation_fingerprint": self.attestation_fingerprint,
            "generation_id": self.generation_id,
        }


@dataclass(frozen=True, slots=True)
class PublicationParity:
    """Immutable candidate facts required to finalize a reservation."""

    candidate_path: str
    candidate_schema_version: int
    candidate_schema_checksum: str
    source_identity_ref: str
    pre_send_identity: str | None = None
    canary_run_id: str | None = None
    fence_identity: str | None = None
    authority_epoch: str | None = None
    fence_counter: int | None = None
    target_ref: str | None = None
    requested_state: str | None = None
    observation_binding_fingerprint: str | None = None
    materializer_run_id: str | None = None
    observed_state: str | None = None
    attestation_fingerprint: str | None = None

    def mapping(self) -> dict[str, str | int | None]:
        return {
            "candidate_path": self.candidate_path,
            "candidate_schema_version": self.candidate_schema_version,
            "candidate_schema_checksum": self.candidate_schema_checksum,
            "source_identity_ref": self.source_identity_ref,
            "pre_send_identity": self.pre_send_identity,
            "canary_run_id": self.canary_run_id,
            "fence_identity": self.fence_identity,
            "authority_epoch": self.authority_epoch,
            "fence_counter": self.fence_counter,
            "target_ref": self.target_ref,
            "requested_state": self.requested_state,
            "observation_binding_fingerprint": self.observation_binding_fingerprint,
            "materializer_run_id": self.materializer_run_id,
            "observed_state": self.observed_state,
            "attestation_fingerprint": self.attestation_fingerprint,
        }


def ledger_path(runtime_dir: Path) -> Path:
    """Return the runtime-owned ledger path."""
    return Path(runtime_dir) / LEDGER_FILENAME


class RuntimeObservationPublicationLedger:
    """Serialize and verify append-only runtime publication history."""

    def __init__(self, path: Path, trusted_source_identity: str | None = None) -> None:
        self.path = Path(path)
        self.trusted_source_identity = trusted_source_identity
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._session_lock = threading.RLock()
        self.connection = None
        with self._session() as connection:
            self._create_schema(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                if trusted_source_identity is not None:
                    require_non_secret_source_identity(trusted_source_identity)
                    connection.execute(
                        "INSERT OR IGNORE INTO ledger_meta(key,value) VALUES ('trusted_source_identity',?)",
                        (trusted_source_identity,),
                    )
                stored = connection.execute(
                    "SELECT value FROM ledger_meta WHERE key='trusted_source_identity'"
                ).fetchone()
                self._trusted_identity_mismatch = (
                    trusted_source_identity is not None
                    and stored is not None
                    and trusted_source_identity != stored[0]
                )
                connection.commit()
            except (sqlite3.DatabaseError, ObservationGenerationContractError):
                connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self):
        with self._session_lock:
            connection = self._connect()
            self.connection = connection
            try:
                yield connection
            finally:
                connection.close()
                self.connection = None

    def _create_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS ledger_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observation_generation_sequence (
                key TEXT PRIMARY KEY CHECK (key='global'),
                last_reserved_generation INTEGER NOT NULL,
                last_published_generation INTEGER NOT NULL,
                last_record_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS observation_publications (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_generation INTEGER NOT NULL,
                event_kind TEXT NOT NULL CHECK (event_kind IN ('reserved','finalized','published','authority_acked','aborted')),
                generation_id TEXT NOT NULL,
                snapshot_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                candidate_path TEXT NOT NULL,
                candidate_schema_version INTEGER NOT NULL,
                candidate_schema_checksum TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                source_set TEXT NOT NULL,
                source_identity_ref TEXT NOT NULL,
                pre_send_identity TEXT,
                canary_run_id TEXT,
                fence_identity TEXT,
                authority_epoch TEXT,
                fence_counter INTEGER,
                target_ref TEXT,
                requested_state TEXT,
                captured_at TEXT NOT NULL,
                completed_at TEXT,
                observation_boundary_id TEXT,
                observation_generation_floor INTEGER,
                observation_binding_fingerprint TEXT,
                materializer_run_id TEXT,
                observed_state TEXT,
                attestation_fingerprint TEXT,
                previous_record_digest TEXT NOT NULL,
                record_digest TEXT NOT NULL,
                failure_reason TEXT,
                CHECK ((observation_boundary_id IS NULL) =
                       (observation_generation_floor IS NULL)),
             CHECK (completed_at IS NULL OR event_kind IN ('finalized','published','authority_acked'))
            );
            CREATE INDEX IF NOT EXISTS observation_publications_generation_idx
                ON observation_publications(observation_generation,event_id);
            CREATE TRIGGER IF NOT EXISTS observation_publications_no_update
                BEFORE UPDATE ON observation_publications
                BEGIN SELECT RAISE(ABORT,'publication ledger is append-only'); END;
            CREATE TRIGGER IF NOT EXISTS observation_publications_no_delete
                BEFORE DELETE ON observation_publications
                BEGIN SELECT RAISE(ABORT,'publication ledger is append-only'); END;
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO ledger_meta(key,value) VALUES ('schema_version',?)",
            (str(LEDGER_SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT OR IGNORE INTO observation_generation_sequence "
            "(key,last_reserved_generation,last_published_generation,last_record_digest) "
            "VALUES ('global',0,0,'')"
        )

    def close(self) -> None:
        """Close the ledger connection."""
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    @property
    def _active_connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise ObservationLedgerError("ledger connection is not active")
        return self.connection

    @staticmethod
    def _digest(values: Mapping[str, str | int | None]) -> str:
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _record_values(
        row: sqlite3.Row, state: str | None = None
    ) -> dict[str, str | int | None]:
        columns = row.keys()
        values = {columns[index]: row[index] for index in range(len(columns))}
        if state is not None:
            values["event_kind"] = state
        values.pop("event_id", None)
        values.pop("record_digest", None)
        return values

    @classmethod
    def _parse(cls, row: sqlite3.Row) -> PublicationRecord:
        return PublicationRecord(
            observation_generation=row["observation_generation"],
            state=row["event_kind"],
            generation_id=row["generation_id"],
            snapshot_id=row["snapshot_id"],
            run_id=row["run_id"],
            candidate_path=row["candidate_path"],
            candidate_schema_version=row["candidate_schema_version"],
            candidate_schema_checksum=row["candidate_schema_checksum"],
            source_hash=row["source_hash"],
            source_set=row["source_set"],
            source_identity_ref=row["source_identity_ref"],
            pre_send_identity=row["pre_send_identity"],
            canary_run_id=row["canary_run_id"],
            fence_identity=row["fence_identity"],
            authority_epoch=row["authority_epoch"],
            fence_counter=row["fence_counter"],
            target_ref=row["target_ref"],
            requested_state=row["requested_state"],
            captured_at=row["captured_at"],
            completed_at=row["completed_at"],
            observation_boundary_id=row["observation_boundary_id"],
            observation_generation_floor=row["observation_generation_floor"],
            observation_binding_fingerprint=row["observation_binding_fingerprint"],
            materializer_run_id=row["materializer_run_id"],
            observed_state=row["observed_state"],
            attestation_fingerprint=row["attestation_fingerprint"],
            previous_record_digest=row["previous_record_digest"],
            record_digest=row["record_digest"],
            failure_reason=row["failure_reason"],
        )

    def _verify_locked(self) -> tuple[int, str]:
        if self._trusted_identity_mismatch:
            raise ObservationLedgerError("ledger trusted source identity mismatch")
        if self.trusted_source_identity is not None:
            stored_identity = self._active_connection.execute(
                "SELECT value FROM ledger_meta WHERE key='trusted_source_identity'"
            ).fetchone()
            if (
                stored_identity is None
                or stored_identity[0] != self.trusted_source_identity
            ):
                raise ObservationLedgerError("ledger trusted source identity mismatch")
        schema = self._active_connection.execute(
            "SELECT value FROM ledger_meta WHERE key='schema_version'"
        ).fetchone()
        if schema is None or schema[0] != str(LEDGER_SCHEMA_VERSION):
            raise ObservationLedgerError("ledger schema version is unsupported")
        meta = self._active_connection.execute(
            "SELECT last_reserved_generation,last_published_generation,last_record_digest "
            "FROM observation_generation_sequence WHERE key='global'"
        ).fetchone()
        if meta is None:
            raise ObservationLedgerError("ledger sequence is missing")
        previous_digest = ""
        last_reserved = 0
        last_published = 0
        states: dict[int, str] = {}
        for row in self._active_connection.execute(
            "SELECT * FROM observation_publications ORDER BY event_id"
        ):
            if row["previous_record_digest"] != previous_digest:
                raise ObservationLedgerError("ledger chain predecessor mismatch")
            expected = self._digest(self._record_values(row))
            if row["record_digest"] != expected:
                raise ObservationLedgerError("ledger record digest mismatch")
            if (
                self.trusted_source_identity is not None
                and row["source_identity_ref"] != self.trusted_source_identity
            ):
                raise ObservationLedgerError("ledger source identity drift")
            if row["event_kind"] in ("finalized", "published") and row["observation_boundary_id"] is not None:
                self._require_complete_binding(row, require_attestation=False)
            if row["event_kind"] == "authority_acked" and row["observation_boundary_id"] is not None:
                self._require_complete_binding(row)
            previous_digest = row["record_digest"]
            generation = row["observation_generation"]
            previous_state = states.get(generation)
            event_kind = row["event_kind"]
            valid_transition = (
                previous_state is None and event_kind == "reserved"
            ) or (
                previous_state == "reserved" and event_kind in ("finalized", "aborted")
            ) or (previous_state == "finalized" and event_kind in ("published", "aborted")) or (
                previous_state == "published" and event_kind == "authority_acked"
            )
            if not valid_transition:
                raise ObservationLedgerError("ledger publication transition is invalid")
            states[generation] = event_kind
            last_reserved = max(last_reserved, generation)
            if event_kind == "published":
                last_published = max(last_published, row["observation_generation"])
        if (meta[0], meta[1], meta[2]) != (last_reserved, last_published, previous_digest):
            raise ObservationLedgerError("ledger sequence mismatch")
        return last_reserved, previous_digest

    def _abort_reserved_locked(self, reason: str) -> None:
        rows = self._active_connection.execute(
            "SELECT p.* FROM observation_publications p "
            "JOIN (SELECT observation_generation,MAX(event_id) event_id "
            "FROM observation_publications GROUP BY observation_generation) latest "
            "ON latest.event_id=p.event_id "
            "WHERE p.event_kind IN ('reserved','finalized') ORDER BY p.event_id"
        ).fetchall()
        previous = ""
        for row in rows:
            previous = self._append_event_locked(row, "aborted", failure_reason=reason)
            self._active_connection.execute(
                "UPDATE observation_generation_sequence SET last_record_digest=? WHERE key='global'",
                (previous,),
            )

    def _append_event_locked(
        self,
        row: sqlite3.Row,
        event_kind: str,
        completed_at: str | None = None,
        failure_reason: str | None = None,
        attestation_fingerprint: str | None = None,
    ) -> str:
        values = self._record_values(row, event_kind)
        sequence = self._active_connection.execute(
            "SELECT last_record_digest FROM observation_generation_sequence WHERE key='global'"
        ).fetchone()
        values["previous_record_digest"] = sequence[0]
        values["completed_at"] = completed_at
        values["failure_reason"] = failure_reason
        if attestation_fingerprint is not None:
            values["attestation_fingerprint"] = attestation_fingerprint
        digest = self._digest(values)
        values["record_digest"] = digest
        columns = ",".join(values)
        placeholders = ",".join("?" for _ in values)
        self._active_connection.execute(
            f"INSERT INTO observation_publications({columns}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return digest

    def reserve(self, floor: int | None, values: PublicationReservation) -> PublicationRecord:
        with self._session():
            return self._reserve(floor, values)

    def _reserve(self, floor: int | None, values: PublicationReservation) -> PublicationRecord:
        """Consume a generation durably and return its reserved record."""
        try:
            require_non_secret_source_identity(values.source_identity_ref)
        except ObservationGenerationContractError as error:
            raise ObservationLedgerError(str(error)) from error
        if floor is not None and (type(floor) is not int or floor < 1):
            raise ObservationLedgerError("publication floor is invalid")
        if floor is not None and floor != values.observation_generation_floor:
            raise ObservationLedgerError("publication floor does not match reservation")
        if (
            self.trusted_source_identity is not None
            and values.source_identity_ref != self.trusted_source_identity
        ):
            raise ObservationLedgerError("reserved source identity is untrusted")
        self._active_connection.execute("BEGIN IMMEDIATE")
        try:
            last_reserved, previous = self._verify_locked()
            self._abort_reserved_locked("reserved publication was recovered before reuse")
            sequence = self._active_connection.execute(
                "SELECT last_reserved_generation,last_record_digest "
                "FROM observation_generation_sequence WHERE key='global'"
            ).fetchone()
            last_reserved, previous = sequence[0], sequence[1]
            generation = max(last_reserved, floor or 0) + 1
            record = values.mapping()
            record.update({
                "observation_generation": generation,
                "event_kind": "reserved",
                "generation_id": record["generation_id"] or uuid.uuid4().hex,
                "completed_at": None,
                "failure_reason": None,
                "previous_record_digest": previous,
            })
            record["record_digest"] = self._digest(record)
            columns = ",".join(record)
            placeholders = ",".join("?" for _ in record)
            self._active_connection.execute(
                f"INSERT INTO observation_publications({columns}) VALUES ({placeholders})",
                tuple(record.values()),
            )
            self._active_connection.execute(
                "UPDATE observation_generation_sequence SET last_reserved_generation=?,last_record_digest=? "
                "WHERE key='global'",
                (generation, record["record_digest"]),
            )
            self._active_connection.commit()
            row = self._active_connection.execute(
                "SELECT * FROM observation_publications WHERE observation_generation=? "
                "ORDER BY event_id DESC LIMIT 1",
                (generation,),
            ).fetchone()
            return self._parse(row)
        except (sqlite3.DatabaseError, ObservationLedgerError):
            self._active_connection.rollback()
            raise

    def publish(
        self, generation: int, completed_at: str, values: PublicationParity
    ) -> PublicationRecord:
        with self._session():
            return self._publish(generation, completed_at, values)

    def finalize(
        self, generation: int, completed_at: str, values: PublicationParity
    ) -> PublicationRecord:
        with self._session():
            self._active_connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_locked()
                row = self._latest_row(generation)
                self._check_parity(row, values, allow_pending_attestation=True)
                self._require_complete_binding(row, require_attestation=False)
                if row["event_kind"] != "reserved":
                    raise ObservationLedgerError("publication reservation is not finalizable")
                digest = self._append_event_locked(
                    row,
                    "finalized",
                    completed_at=completed_at,
                    attestation_fingerprint=values.attestation_fingerprint,
                )
                self._active_connection.execute(
                    "UPDATE observation_generation_sequence SET last_record_digest=? WHERE key='global'",
                    (digest,),
                )
                self._require_complete_binding(
                    self._latest_row(generation), require_attestation=False
                )
                self._active_connection.commit()
                return self._parse(self._latest_row(generation))
            except (sqlite3.DatabaseError, ObservationLedgerError):
                self._active_connection.rollback()
                raise

    def acknowledge(
        self, generation: int, attestation_fingerprint: str
    ) -> PublicationRecord:
        """Append the proving event after the authority ACK is durable."""
        if (
            type(attestation_fingerprint) is not str
            or not attestation_fingerprint.strip()
        ):
            raise ObservationLedgerError("authority ACK attestation is invalid")
        with self._session():
            self._active_connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_locked()
                row = self._latest_row(generation)
                if row["event_kind"] == "authority_acked":
                    if row["attestation_fingerprint"] != attestation_fingerprint:
                        raise ObservationLedgerError("authority ACK attestation mismatch")
                    self._active_connection.commit()
                    return self._parse(row)
                if row["event_kind"] != "published":
                    raise ObservationLedgerError("snapshot publication is not acknowledgeable")
                self._require_complete_binding(row, require_attestation=False)
                digest = self._append_event_locked(
                    row,
                    "authority_acked",
                    completed_at=row["completed_at"],
                    attestation_fingerprint=attestation_fingerprint,
                )
                self._require_complete_binding(self._latest_row(generation))
                self._active_connection.execute(
                    "UPDATE observation_generation_sequence SET last_record_digest=? WHERE key='global'",
                    (digest,),
                )
                self._active_connection.commit()
                return self._parse(self._latest_row(generation))
            except (sqlite3.DatabaseError, ObservationLedgerError):
                self._active_connection.rollback()
                raise

    def publish_finalized(self, generation: int) -> PublicationRecord:
        """Complete a finalized snapshot without changing its immutable parity."""
        with self._session():
            self._active_connection.execute("BEGIN IMMEDIATE")
            try:
                self._verify_locked()
                row = self._latest_row(generation)
                if row["event_kind"] != "finalized":
                    raise ObservationLedgerError("finalized snapshot is unavailable")
                self._require_complete_binding(row, require_attestation=False)
                digest = self._append_event_locked(
                    row, "published", completed_at=row["completed_at"]
                )
                self._active_connection.execute(
                    "UPDATE observation_generation_sequence SET last_published_generation=?,last_record_digest=? "
                    "WHERE key='global'",
                    (generation, digest),
                )
                self._active_connection.commit()
                return self._parse(self._latest_row(generation))
            except (sqlite3.DatabaseError, ObservationLedgerError):
                self._active_connection.rollback()
                raise

    def _latest_row(self, generation: int) -> sqlite3.Row:
        row = self._active_connection.execute(
            "SELECT * FROM observation_publications WHERE observation_generation=? "
            "ORDER BY event_id DESC LIMIT 1",
            (generation,),
        ).fetchone()
        if row is None:
            raise ObservationLedgerError("publication reservation is unavailable")
        return row

    @staticmethod
    def _check_parity(
        row: sqlite3.Row,
        values: PublicationParity,
        allow_pending_attestation: bool = False,
    ) -> None:
        for key, value in values.mapping().items():
            if (
                key == "attestation_fingerprint"
                and allow_pending_attestation
                and row[key] is None
                and value is not None
            ):
                continue
            if row[key] != value:
                raise ObservationLedgerError("publication parity mismatch")

    @staticmethod
    def _require_complete_binding(
        row: sqlite3.Row, require_attestation: bool = True
    ) -> None:
        if row["observation_boundary_id"] is None:
            return
        fields = (
            "pre_send_identity", "canary_run_id", "fence_identity", "authority_epoch",
            "fence_counter", "target_ref", "requested_state",
            "observation_binding_fingerprint", "materializer_run_id", "observed_state",
        )
        if any(row[field] is None for field in fields):
            raise ObservationLedgerError("publication authority binding is incomplete")
        expected = observation_binding_fingerprint(
            row["observation_boundary_id"],
            row["pre_send_identity"],
            row["canary_run_id"],
            row["fence_identity"],
            row["authority_epoch"],
            row["fence_counter"],
            row["source_identity_ref"],
            row["target_ref"],
            row["requested_state"],
            row["observation_generation_floor"],
        )
        if row["observation_binding_fingerprint"] != expected:
            raise ObservationLedgerError("publication authority binding fingerprint mismatch")
        if require_attestation and (
            type(row["attestation_fingerprint"]) is not str
            or not row["attestation_fingerprint"].strip()
        ):
            raise ObservationLedgerError("publication attestation is missing")

    def _publish(
        self, generation: int, completed_at: str, values: PublicationParity
    ) -> PublicationRecord:
        """Finalize one exact reservation after candidate parity validation."""
        self._active_connection.execute("BEGIN IMMEDIATE")
        try:
            self._verify_locked()
            row = self._latest_row(generation)
            self._check_parity(row, values)
            if row["event_kind"] == "reserved":
                finalized_digest = self._append_event_locked(
                    row, "finalized", completed_at=completed_at
                )
                self._active_connection.execute(
                    "UPDATE observation_generation_sequence SET last_record_digest=? WHERE key='global'",
                    (finalized_digest,),
                )
                row = self._latest_row(generation)
            if row["event_kind"] != "finalized":
                raise ObservationLedgerError("publication reservation is not publishable")
            if row["observation_boundary_id"] is not None:
                self._require_complete_binding(row, require_attestation=False)
            digest = self._append_event_locked(row, "published", completed_at=completed_at)
            self._active_connection.execute(
                "UPDATE observation_generation_sequence SET last_published_generation=?,last_record_digest=? "
                "WHERE key='global'",
                (generation, digest),
            )
            self._active_connection.commit()
            return self._parse(self._latest_row(generation))
        except (sqlite3.DatabaseError, ObservationLedgerError):
            self._active_connection.rollback()
            raise

    def latest_published(self) -> PublicationRecord | None:
        """Return the latest verified publication after chain verification."""
        with self._session():
            self._verify_locked()
            row = self._active_connection.execute(
                "SELECT * FROM observation_publications WHERE event_kind='published' "
                "ORDER BY observation_generation DESC,event_id DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._parse(row)

    def latest_available(self) -> PublicationRecord | None:
        with self._session():
            self._verify_locked()
            row = self._active_connection.execute(
                """SELECT publication.*
                     FROM observation_publications AS publication
                     JOIN (
                         SELECT observation_generation, MAX(event_id) AS event_id
                           FROM observation_publications
                          GROUP BY observation_generation
                     ) AS latest ON latest.event_id=publication.event_id
                    WHERE publication.event_kind IN ('finalized','published','authority_acked')
                    ORDER BY publication.observation_generation DESC
                    LIMIT 1"""
            ).fetchone()
            return None if row is None else self._parse(row)

    def latest_proving(self) -> PublicationRecord | None:
        """Return only a publication with a durable authority ACK event."""
        with self._session():
            self._verify_locked()
            row = self._active_connection.execute(
                "SELECT * FROM observation_publications WHERE event_kind='authority_acked' "
                "ORDER BY observation_generation DESC,event_id DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._parse(row)

    def latest_finalized(self) -> PublicationRecord | None:
        """Return the latest finalized snapshot awaiting publication."""
        with self._session():
            self._verify_locked()
            row = self._active_connection.execute(
                "SELECT * FROM observation_publications WHERE event_kind='finalized' "
                "ORDER BY observation_generation DESC,event_id DESC LIMIT 1"
            ).fetchone()
            return None if row is None else self._parse(row)

    def latest_unproven(self) -> PublicationRecord | None:
        """Return the newest snapshot event without a proving ACK event."""
        with self._session():
            self._verify_locked()
            row = self._active_connection.execute(
                """SELECT publication.*
                   FROM observation_publications AS publication
                  WHERE publication.event_kind IN ('finalized','published')
                    AND NOT EXISTS (
                        SELECT 1 FROM observation_publications AS ack
                         WHERE ack.observation_generation=publication.observation_generation
                           AND ack.event_kind='authority_acked'
                    )
                  ORDER BY publication.observation_generation DESC, publication.event_id DESC
                  LIMIT 1"""
            ).fetchone()
            return None if row is None else self._parse(row)

    def authority_acknowledged(self, generation: int) -> bool:
        """Return whether the proving event exists for one generation."""
        with self._session():
            self._verify_locked()
            return self._active_connection.execute(
                "SELECT 1 FROM observation_publications "
                "WHERE observation_generation=? AND event_kind='authority_acked'",
                (generation,),
            ).fetchone() is not None

    def has_history(self) -> bool:
        """Return whether any reserved, published, or aborted generation exists."""
        with self._session():
            return self._active_connection.execute(
                "SELECT 1 FROM observation_publications LIMIT 1"
            ).fetchone() is not None
