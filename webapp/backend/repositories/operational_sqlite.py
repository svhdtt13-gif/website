"""Isolated operational SQLite foundation for P4.

This module owns only mutable P4 operational state. It is deliberately not
imported by Flask routes, the scheduler, or the Phase 3 immutable generation
reader. The exact operational path is guarded so a P3 generation cannot be
opened for writes accidentally.
"""
import hashlib
import json
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 8
OPERATIONAL_FILENAME = "p4_operational.sqlite3"
OPERATIONAL_DIRNAME = "operational"
BACKUP_DIRNAME = "backups"

BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  target_json TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  parent_job_id TEXT REFERENCES jobs(job_id),
  status TEXT NOT NULL CHECK (status IN
    ('queued','claimed','running','dispatched','succeeded','failed','cancelled','unknown')),
  attempt INTEGER NOT NULL DEFAULT 0,
  owner_id TEXT,
  lease_name TEXT,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  started_at TEXT,
  dispatched_at TEXT,
  finished_at TEXT,
  lease_until TEXT,
  remote_action_id TEXT,
  result_json TEXT,
  error_json TEXT
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_id, lease_name, lease_until);

CREATE TABLE IF NOT EXISTS leases (
  lease_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  release_reason TEXT
);

CREATE TABLE IF NOT EXISTS authority_state (
  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter >= 0),
  quarantined INTEGER NOT NULL CHECK (quarantined IN (0, 1))
);

CREATE TABLE IF NOT EXISTS fenced_leases (
  lease_id TEXT PRIMARY KEY,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  owner_id TEXT NOT NULL,
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  idempotency_key TEXT NOT NULL UNIQUE,
  state TEXT NOT NULL CHECK
    (state IN ('ACQUIRED','HEARTBEATING','RELEASED','EXPIRED','QUARANTINED')),
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  release_reason TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS fenced_live_scope_idx
  ON fenced_leases(host_id, profile_id, binding_generation)
  WHERE state IN ('ACQUIRED', 'HEARTBEATING');

CREATE UNIQUE INDEX IF NOT EXISTS fenced_identity_idx
  ON fenced_leases(authority_epoch, fence_counter);

CREATE TABLE IF NOT EXISTS checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  p3_generation_id TEXT,
  p3_source_hash TEXT,
  active_group TEXT,
  live_snapshot_hash TEXT,
  pending_jobs_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_overrides (
  override_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  source TEXT NOT NULL,
  requested_state TEXT,
  observed_state TEXT,
  precedence INTEGER NOT NULL,
  request_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  resolved_at TEXT,
  resolution TEXT,
  evidence_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS manual_override_active_idx
  ON manual_overrides(target_id, resolved_at, expires_at, precedence);

CREATE TABLE IF NOT EXISTS worker_events (
  event_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  job_id TEXT REFERENCES jobs(job_id),
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data_json TEXT NOT NULL
);
"""
LEGACY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  target_json TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  parent_job_id TEXT REFERENCES jobs(job_id),
  status TEXT NOT NULL CHECK (status IN
    ('queued','claimed','running','dispatched','succeeded','failed','cancelled','unknown')),
  attempt INTEGER NOT NULL DEFAULT 0,
  owner_id TEXT,
  lease_name TEXT,
  created_at TEXT NOT NULL,
  claimed_at TEXT,
  started_at TEXT,
  dispatched_at TEXT,
  finished_at TEXT,
  lease_until TEXT,
  remote_action_id TEXT,
  result_json TEXT,
  error_json TEXT
);

CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_id, lease_name, lease_until);

CREATE TABLE IF NOT EXISTS leases (
  lease_name TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  acquired_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  release_reason TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
  checkpoint_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  p3_generation_id TEXT,
  p3_source_hash TEXT,
  active_group TEXT,
  live_snapshot_hash TEXT,
  pending_jobs_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manual_overrides (
  override_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL,
  source TEXT NOT NULL,
  requested_state TEXT,
  observed_state TEXT,
  precedence INTEGER NOT NULL,
  request_id TEXT,
  created_at TEXT NOT NULL,
  expires_at TEXT,
  resolved_at TEXT,
  resolution TEXT,
  evidence_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS manual_override_active_idx
  ON manual_overrides(target_id, resolved_at, expires_at, precedence);

CREATE TABLE IF NOT EXISTS worker_events (
  event_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  job_id TEXT REFERENCES jobs(job_id),
  event_type TEXT NOT NULL,
  created_at TEXT NOT NULL,
  data_json TEXT NOT NULL
);
"""
SCHEMA_V3_SQL = BASE_SCHEMA_SQL
SCHEMA_V2_SQL = SCHEMA_V3_SQL.replace(
    "  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),\n",
    "",
).replace(
    "  ON fenced_leases(host_id, profile_id, binding_generation)\n",
    "  ON fenced_leases(host_id, profile_id, binding_generation, verified_identity_ref)\n",
)
SCHEMA_V3_CHECKSUM = hashlib.sha256(SCHEMA_V3_SQL.encode("utf-8")).hexdigest()
SCHEMA_V2_CHECKSUM = hashlib.sha256(SCHEMA_V2_SQL.encode("utf-8")).hexdigest()
LEGACY_SCHEMA_CHECKSUM = hashlib.sha256(LEGACY_SCHEMA_SQL.encode("utf-8")).hexdigest()

AUTHORITY_TARGETS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS authority_bound_targets (
  target_id TEXT PRIMARY KEY,
  job_id TEXT NOT NULL UNIQUE,
  operation_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  context_fingerprint TEXT NOT NULL,
  source_snapshot_digest TEXT NOT NULL,
  provenance_json TEXT NOT NULL,
  status TEXT NOT NULL CHECK
    (status IN ('requested','claimable','claimed','quarantined')),
  created_at TEXT NOT NULL,
  recorded_at TEXT,
  claimed_at TEXT,
  claimed_by TEXT,
  claim_lease_id TEXT,
  quarantine_reason TEXT
);
"""
AUTHORITY_TARGETS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS authority_targets_claim_idx
  ON authority_bound_targets(status, created_at);
CREATE INDEX IF NOT EXISTS authority_targets_scope_idx
  ON authority_bound_targets(host_id, profile_id, binding_generation,
                              verified_identity_ref, verified_identity_revision);
"""
SCHEMA_V4_SQL = BASE_SCHEMA_SQL + AUTHORITY_TARGETS_TABLE_SQL + AUTHORITY_TARGETS_INDEX_SQL
SCHEMA_V4_CHECKSUM = hashlib.sha256(SCHEMA_V4_SQL.encode("utf-8")).hexdigest()

EXECUTION_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS authority_bound_executions (
  execution_id TEXT PRIMARY KEY,
  target_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_targets(target_id),
  job_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  owner_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK
    (status IN ('claimed','running','dispatched','succeeded','failed','unknown','quarantined')),
  attempt_count INTEGER NOT NULL CHECK (attempt_count = 1),
  created_at TEXT NOT NULL,
  started_at TEXT,
  terminal_at TEXT,
  quarantine_reason TEXT
);

CREATE TABLE IF NOT EXISTS authority_bound_attempts (
  attempt_id TEXT PRIMARY KEY,
  execution_id TEXT NOT NULL REFERENCES authority_bound_executions(execution_id),
  target_id TEXT NOT NULL REFERENCES authority_bound_targets(target_id),
  attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
  idempotency_key TEXT NOT NULL,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  owner_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK
    (status IN ('claimed','running','dispatched','succeeded','failed','unknown','quarantined')),
  created_at TEXT NOT NULL,
  started_at TEXT,
  terminal_at TEXT,
  quarantine_reason TEXT,
  UNIQUE(execution_id, attempt_number)
);

CREATE INDEX IF NOT EXISTS authority_executions_status_idx
  ON authority_bound_executions(status, created_at);
CREATE INDEX IF NOT EXISTS authority_executions_scope_idx
  ON authority_bound_executions(host_id, profile_id, binding_generation,
                                verified_identity_ref, verified_identity_revision,
                                authority_epoch, fence_counter);
CREATE INDEX IF NOT EXISTS authority_attempts_execution_idx
  ON authority_bound_attempts(execution_id, attempt_number);
"""
SCHEMA_V5_SQL = SCHEMA_V4_SQL + EXECUTION_TABLES_SQL
SCHEMA_V5_CHECKSUM = hashlib.sha256(SCHEMA_V5_SQL.encode("utf-8")).hexdigest()

DISPATCH_INTENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS authority_bound_dispatch_intents (
  intent_id TEXT PRIMARY KEY,
  execution_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_executions(execution_id),
  attempt_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_attempts(attempt_id),
  target_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_targets(target_id),
  job_id TEXT NOT NULL UNIQUE,
  idempotency_key TEXT NOT NULL UNIQUE,
  operation_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  request_fingerprint TEXT NOT NULL,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  owner_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  status TEXT NOT NULL CHECK
    (status IN ('prepared','blocked','unknown','reconciled','quarantined')),
  blocked_reason TEXT,
  unknown_reason TEXT,
  evidence_ref TEXT,
  reconciliation_evidence_ref TEXT,
  reconciliation_result TEXT CHECK
    (reconciliation_result IN ('succeeded','failed') OR reconciliation_result IS NULL),
  created_at TEXT NOT NULL,
  prepared_at TEXT,
  unknown_at TEXT,
  reconciled_at TEXT,
  quarantine_reason TEXT
);

CREATE INDEX IF NOT EXISTS dispatch_intents_status_idx
  ON authority_bound_dispatch_intents(status, created_at);
CREATE INDEX IF NOT EXISTS dispatch_intents_scope_idx
  ON authority_bound_dispatch_intents(host_id, profile_id, binding_generation,
                                      verified_identity_ref, verified_identity_revision,
                                      authority_epoch, fence_counter);
"""
SCHEMA_V6_SQL = SCHEMA_V5_SQL + DISPATCH_INTENTS_TABLE_SQL
SCHEMA_V6_CHECKSUM = hashlib.sha256(SCHEMA_V6_SQL.encode("utf-8")).hexdigest()

SHADOW_EVALUATIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS authority_bound_shadow_evaluations (
  shadow_evaluation_id TEXT PRIMARY KEY,
  intent_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_dispatch_intents(intent_id),
  execution_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_executions(execution_id),
  attempt_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_attempts(attempt_id),
  target_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_targets(target_id),
  job_id TEXT NOT NULL UNIQUE,
  intent_idempotency_key TEXT NOT NULL UNIQUE,
  request_idempotency_key TEXT NOT NULL UNIQUE,
  operation_kind TEXT NOT NULL,
  target_ref TEXT NOT NULL,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  owner_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  envelope_version INTEGER NOT NULL CHECK (envelope_version = 1),
  transport_kind TEXT NOT NULL CHECK (transport_kind IN ('null','recording')),
  portable_snapshot_fingerprint TEXT,
  prepared_request_fingerprint TEXT,
  envelope_json TEXT,
  envelope_fingerprint TEXT,
  transport_fingerprint TEXT,
  outcome TEXT NOT NULL CHECK
    (outcome IN ('evaluated','matched','mismatched','blocked','quarantined')),
  reason_class TEXT,
  created_at TEXT NOT NULL,
  evaluated_at TEXT,
  quarantined_at TEXT,
  quarantine_reason TEXT
);

CREATE INDEX IF NOT EXISTS shadow_evaluations_outcome_idx
  ON authority_bound_shadow_evaluations(outcome, created_at);
"""
SCHEMA_V7_SQL = SCHEMA_V6_SQL + SHADOW_EVALUATIONS_TABLE_SQL
SCHEMA_V7_CHECKSUM = hashlib.sha256(SCHEMA_V7_SQL.encode("utf-8")).hexdigest()

CANARY_CANDIDATES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS authority_bound_canary_candidates (
  canary_candidate_id TEXT PRIMARY KEY,
  shadow_evaluation_id TEXT NOT NULL UNIQUE
    REFERENCES authority_bound_shadow_evaluations(shadow_evaluation_id),
  intent_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_dispatch_intents(intent_id),
  execution_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_executions(execution_id),
  attempt_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_attempts(attempt_id),
  target_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_targets(target_id),
  job_id TEXT NOT NULL UNIQUE REFERENCES authority_bound_targets(job_id),
  intent_idempotency_key TEXT NOT NULL UNIQUE,
  shadow_idempotency_key TEXT NOT NULL UNIQUE,
  canary_idempotency_key TEXT NOT NULL UNIQUE,
  operation_kind TEXT NOT NULL,
  destination_ref TEXT NOT NULL,
  host_id TEXT NOT NULL,
  profile_id TEXT NOT NULL,
  binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
  verified_identity_ref TEXT NOT NULL,
  verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0),
  authority_epoch TEXT NOT NULL,
  fence_counter INTEGER NOT NULL CHECK (fence_counter > 0),
  owner_id TEXT NOT NULL,
  lease_id TEXT NOT NULL,
  portable_snapshot_fingerprint TEXT NOT NULL,
  shadow_envelope_fingerprint TEXT NOT NULL,
  contract_version TEXT NOT NULL,
  transport_kind TEXT NOT NULL CHECK
    (transport_kind IN ('null','recording','contract_probe')),
  transport_contract_version TEXT NOT NULL,
  pre_send_identity TEXT NOT NULL UNIQUE,
  envelope_json TEXT NOT NULL,
  envelope_fingerprint TEXT NOT NULL,
  state TEXT NOT NULL CHECK
    (state IN ('armed','blocked','unknown','reconciled','quarantined')),
  reason_class TEXT,
  created_at TEXT NOT NULL,
  armed_at TEXT,
  ambiguity_evidence_ref TEXT,
  ambiguity_reason_class TEXT,
  unknown_at TEXT,
  reconciliation_evidence_ref TEXT,
  reconciliation_result TEXT CHECK
    (reconciliation_result IN ('succeeded','failed') OR reconciliation_result IS NULL),
  reconciled_at TEXT,
  quarantined_at TEXT,
  quarantine_reason TEXT
);

CREATE INDEX IF NOT EXISTS canary_candidates_state_idx
  ON authority_bound_canary_candidates(state, created_at);
"""
SCHEMA_SQL = SCHEMA_V7_SQL + CANARY_CANDIDATES_TABLE_SQL
SCHEMA_CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


class OperationalPathError(ValueError):
    """The path is not the dedicated P4 operational database boundary."""


class OperationalSchemaError(RuntimeError):
    """The operational database schema cannot be trusted."""


class OperationalIntegrityError(RuntimeError):
    """SQLite integrity checks failed."""


class OperationalTransactionError(RuntimeError):
    """A transaction was started while another transaction was active."""


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _new_authority_epoch():
    return secrets.token_hex(16)


def _resolved(path):
    return Path(path).expanduser().resolve()


def operational_path(runtime_dir):
    """Return the only writable P4 operational database path."""
    return _resolved(runtime_dir) / OPERATIONAL_DIRNAME / OPERATIONAL_FILENAME


def backup_directory(runtime_dir):
    return operational_path(runtime_dir).parent / BACKUP_DIRNAME


def _require_operational_path(path, runtime_dir):
    requested = _resolved(path)
    expected = operational_path(runtime_dir)
    if requested != expected:
        raise OperationalPathError(
            "only the dedicated operational database path is writable"
        )
    p3_root = (_resolved(runtime_dir) / "sqlite").resolve()
    try:
        requested.relative_to(p3_root)
    except ValueError:
        return requested
    raise OperationalPathError("P3 immutable generation path is not operational state")


def _require_backup_path(path, runtime_dir, allow_existing=True):
    requested = _resolved(path)
    root = backup_directory(runtime_dir)
    try:
        requested.relative_to(root)
    except ValueError as error:
        raise OperationalPathError("backup path is outside the operational backup directory") from error
    if requested.suffix != ".sqlite3":
        raise OperationalPathError("backup path must be a SQLite database")
    if not allow_existing and requested.exists():
        raise OperationalPathError("backup destination already exists")
    return requested


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _deny_cross_database(action, _arg1, _arg2, _db_name, _source):
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


def _pragma_rows(connection, pragma, table_name):
    escaped = table_name.replace("'", "''")
    return connection.execute(
        "PRAGMA " + pragma + "('" + escaped + "')"
    ).fetchall()


def _schema_identity(connection):
    """Canonicalize actual SQLite objects, columns, indexes and foreign keys."""
    objects = []
    rows = connection.execute(
        """SELECT type, name, tbl_name, sql
           FROM sqlite_master
           WHERE name NOT LIKE 'sqlite_%'
           ORDER BY type, name"""
    ).fetchall()
    for object_type, name, table_name, sql in rows:
        item = {
            "type": object_type,
            "name": name,
            "table": table_name,
            "sql": " ".join((sql or "").split()).lower(),
        }
        if object_type == "table":
            item["columns"] = [
                tuple(row)
                for row in _pragma_rows(connection, "table_info", name)
            ]
            item["foreign_keys"] = [
                tuple(row)
                for row in _pragma_rows(connection, "foreign_key_list", name)
            ]
            item["indexes"] = [
                {
                    "name": row[1],
                    "unique": row[2],
                    "origin": row[3],
                    "partial": row[4],
                    "columns": [
                        tuple(index_row)
                        for index_row in _pragma_rows(
                            connection, "index_info", row[1]
                        )
                    ],
                }
                for row in _pragma_rows(connection, "index_list", name)
                if not row[1].startswith("sqlite_")
            ]
        elif object_type == "index":
            item["columns"] = [
                tuple(row)
                for row in _pragma_rows(connection, "index_info", name)
            ]
        objects.append(item)
    return json.dumps(objects, sort_keys=True, separators=(",", ":"))


def _expected_schema_identity(schema_sql=SCHEMA_SQL):
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(schema_sql)
        connection.execute("PRAGMA foreign_keys = ON")
        return _schema_identity(connection)
    finally:
        connection.close()


EXPECTED_SCHEMA_IDENTITY = _expected_schema_identity()
EXPECTED_V7_SCHEMA_IDENTITY = _expected_schema_identity(SCHEMA_V7_SQL)
EXPECTED_V6_SCHEMA_IDENTITY = _expected_schema_identity(SCHEMA_V6_SQL)
EXPECTED_V3_SCHEMA_IDENTITY = _expected_schema_identity(SCHEMA_V3_SQL)
EXPECTED_V2_SCHEMA_IDENTITY = _expected_schema_identity(SCHEMA_V2_SQL)


def _expected_migrated_v3_identity():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(SCHEMA_V2_SQL)
        connection.execute(
            "ALTER TABLE fenced_leases ADD COLUMN "
            "verified_identity_revision INTEGER NOT NULL DEFAULT 1 "
            "CHECK (verified_identity_revision > 0)"
        )
        connection.execute("PRAGMA foreign_keys = ON")
        return _schema_identity(connection)
    finally:
        connection.close()


EXPECTED_MIGRATED_V3_IDENTITY = _expected_migrated_v3_identity()


def _expected_legacy_schema_identity():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(LEGACY_SCHEMA_SQL)
        connection.execute("PRAGMA foreign_keys = ON")
        return _schema_identity(connection)
    finally:
        connection.close()


EXPECTED_LEGACY_SCHEMA_IDENTITY = _expected_legacy_schema_identity()
_ACTIVE_CONNECTIONS = {}
_BOOTSTRAP_REQUIRED = {}


def _integrity_ok(connection):
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        return False
    return not connection.execute("PRAGMA foreign_key_check").fetchone()


class OperationalSQLiteRepository:
    """Connection and transaction primitives for the mutable P4 database."""

    def __init__(self, connection, runtime_dir, track_lifecycle=True):
        self.connection = connection
        self.runtime_dir = _resolved(runtime_dir)
        self.path = operational_path(runtime_dir)
        self._track_lifecycle = track_lifecycle
        self._closed = False
        if track_lifecycle:
            _BOOTSTRAP_REQUIRED.setdefault(self.path, True)
            _ACTIVE_CONNECTIONS[self.path] = _ACTIVE_CONNECTIONS.get(self.path, 0) + 1
        self._bootstrap_required = _BOOTSTRAP_REQUIRED.get(self.path, False)

    @property
    def requires_fresh_bootstrap(self):
        return _BOOTSTRAP_REQUIRED.get(self.path, self._bootstrap_required)

    @requires_fresh_bootstrap.setter
    def requires_fresh_bootstrap(self, value):
        self._bootstrap_required = bool(value)
        if self._track_lifecycle:
            _BOOTSTRAP_REQUIRED[self.path] = self._bootstrap_required

    @staticmethod
    def _connect(path):
        connection = sqlite3.connect(
            str(path), timeout=5, isolation_level=None
        )
        connection.row_factory = sqlite3.Row
        connection.set_authorizer(_deny_cross_database)
        connection.execute("PRAGMA foreign_keys = ON")
        journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            connection.close()
            raise OperationalSchemaError("operational database did not enable WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @classmethod
    def create(cls, runtime_dir):
        runtime_dir = _resolved(runtime_dir)
        path = operational_path(runtime_dir)
        if path.exists():
            raise OperationalPathError("operational database already exists")
        path.parent.mkdir(parents=True, exist_ok=True)
        backup_directory(runtime_dir).mkdir(parents=True, exist_ok=True)
        connection = cls._connect(path)
        repository = cls(connection, runtime_dir)
        try:
            connection.executescript(SCHEMA_SQL)
            with repository.transaction():
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    ("schema_checksum", SCHEMA_CHECKSUM),
                )
                connection.execute(
                    "INSERT INTO authority_state "
                    "(singleton, authority_epoch, fence_counter, quarantined) "
                    "VALUES (1, ?, 0, 0)",
                    (_new_authority_epoch(),),
                )
        except Exception:
            repository.close()
            try:
                path.unlink()
            except OSError:
                pass
            raise
        repository.requires_fresh_bootstrap = False
        repository._validate_schema()
        return repository

    @classmethod
    def open(cls, runtime_dir):
        runtime_dir = _resolved(runtime_dir)
        path = _require_operational_path(operational_path(runtime_dir), runtime_dir)
        if not path.is_file():
            raise OperationalPathError("operational database does not exist")
        repository = cls(cls._connect(path), runtime_dir)
        try:
            repository._migrate_schema()
            repository._validate_schema()
        except Exception:
            repository.close()
            raise
        return repository

    @classmethod
    def open_path(cls, path, runtime_dir):
        """Open only the exact operational path; reject P3 or arbitrary paths."""
        runtime_dir = _resolved(runtime_dir)
        checked = _require_operational_path(path, runtime_dir)
        if not checked.is_file():
            raise OperationalPathError("operational database does not exist")
        repository = cls(cls._connect(checked), runtime_dir)
        try:
            repository._migrate_schema()
            repository._validate_schema()
        except Exception:
            repository.close()
            raise
        return repository

    def _validate_schema(self):
        try:
            values = dict(
                self.connection.execute(
                    "SELECT key, value FROM schema_meta"
                ).fetchall()
            )
        except sqlite3.DatabaseError as error:
            raise OperationalSchemaError("schema metadata is missing") from error
        if values.get("schema_version") != str(SCHEMA_VERSION):
            raise OperationalSchemaError("unsupported operational schema version")
        if values.get("schema_checksum") != SCHEMA_CHECKSUM:
            raise OperationalSchemaError("operational schema checksum mismatch")
        if _schema_identity(self.connection) != EXPECTED_SCHEMA_IDENTITY:
            raise OperationalSchemaError("actual operational schema does not match expected schema")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational SQLite integrity check failed")

    def _migrate_schema(self):
        try:
            values = dict(
                self.connection.execute(
                    "SELECT key, value FROM schema_meta"
                ).fetchall()
            )
        except sqlite3.DatabaseError as error:
            raise OperationalSchemaError("schema metadata is missing") from error
        version = values.get("schema_version")
        if version == "2":
            with self.transaction():
                values = dict(
                    self.connection.execute(
                        "SELECT key, value FROM schema_meta"
                    ).fetchall()
                )
                if values.get("schema_version") != "2":
                    return
                self._verify_v2_before_migration(values)
                self._rebuild_v2_fenced_leases()
                self._install_authority_target_schema()
                self._install_execution_schema()
                self._install_dispatch_schema()
                self._install_shadow_schema()
                self._install_canary_schema()
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                    (SCHEMA_CHECKSUM,),
                )
            return
        if version == "3":
            with self.transaction():
                values = dict(
                    self.connection.execute(
                        "SELECT key, value FROM schema_meta"
                    ).fetchall()
                )
                if values.get("schema_version") != "3":
                    return
                self._verify_v3_before_migration(values)
                self._install_authority_target_schema()
                self._install_execution_schema()
                self._install_dispatch_schema()
                self._install_shadow_schema()
                self._install_canary_schema()
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                    (SCHEMA_CHECKSUM,),
                )
            return
        if version == "4":
            with self.transaction():
                values = dict(
                    self.connection.execute(
                        "SELECT key, value FROM schema_meta"
                    ).fetchall()
                )
                if values.get("schema_version") != "4":
                    return
                self._verify_v4_before_migration(values)
                self._install_execution_schema()
                self._install_dispatch_schema()
                self._install_shadow_schema()
                self._install_canary_schema()
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                    (SCHEMA_CHECKSUM,),
                )
            return
        if version == "5":
            with self.transaction():
                values = dict(
                    self.connection.execute(
                        "SELECT key, value FROM schema_meta"
                    ).fetchall()
                )
                if values.get("schema_version") != "5":
                    return
                self._verify_v5_before_migration(values)
                self._install_dispatch_schema()
                self._install_shadow_schema()
                self._install_canary_schema()
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                    (SCHEMA_CHECKSUM,),
                )
            return
        if version == "6":
            with self.transaction():
                values = dict(
                    self.connection.execute(
                        "SELECT key, value FROM schema_meta"
                    ).fetchall()
                )
                if values.get("schema_version") != "6":
                    return
                self._verify_v6_before_migration(values)
                self._install_shadow_schema()
                self._install_canary_schema()
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                    (SCHEMA_CHECKSUM,),
                )
            return
        if version == "7":
            with self.transaction():
                values = dict(
                    self.connection.execute(
                        "SELECT key, value FROM schema_meta"
                    ).fetchall()
                )
                if values.get("schema_version") != "7":
                    return
                self._verify_v7_before_migration(values)
                self._install_canary_schema()
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                self.connection.execute(
                    "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                    (SCHEMA_CHECKSUM,),
                )
            return
        if version != "1":
            return
        if values.get("schema_checksum") != LEGACY_SCHEMA_CHECKSUM:
            raise OperationalSchemaError("legacy operational schema checksum mismatch")
        if _schema_identity(self.connection) != EXPECTED_LEGACY_SCHEMA_IDENTITY:
            raise OperationalSchemaError("legacy operational schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("legacy operational SQLite integrity check failed")
        with self.transaction():
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS authority_state ( "
                "singleton INTEGER PRIMARY KEY CHECK (singleton = 1), "
                "authority_epoch TEXT NOT NULL, "
                "fence_counter INTEGER NOT NULL CHECK (fence_counter >= 0), "
                "quarantined INTEGER NOT NULL CHECK (quarantined IN (0, 1)) )"
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS fenced_leases ( "
                "lease_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, "
                "profile_id TEXT NOT NULL, "
                "binding_generation INTEGER NOT NULL CHECK (binding_generation > 0), "
                "verified_identity_ref TEXT NOT NULL, "
                "verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0), "
                "owner_id TEXT NOT NULL, "
                "authority_epoch TEXT NOT NULL, "
                "fence_counter INTEGER NOT NULL CHECK (fence_counter > 0), "
                "idempotency_key TEXT NOT NULL UNIQUE, "
                "state TEXT NOT NULL CHECK (state IN "
                "('ACQUIRED','HEARTBEATING','RELEASED','EXPIRED','QUARANTINED')), "
                "acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, "
                "expires_at TEXT NOT NULL, release_reason TEXT )"
            )
            self.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS fenced_live_scope_idx ON fenced_leases("
                "host_id, profile_id, binding_generation) "
                "WHERE state IN ('ACQUIRED', 'HEARTBEATING')"
            )
            self.connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS fenced_identity_idx ON fenced_leases("
                "authority_epoch, fence_counter)"
            )
            self._install_authority_target_schema()
            self._install_execution_schema()
            self._install_dispatch_schema()
            self._install_shadow_schema()
            self._install_canary_schema()
            self.connection.execute(
                "INSERT INTO authority_state "
                "(singleton, authority_epoch, fence_counter, quarantined) "
                "VALUES (1, ?, 0, 0)",
                (_new_authority_epoch(),),
            )
            self.connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='schema_version'",
                (str(SCHEMA_VERSION),),
            )
            self.connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='schema_checksum'",
                (SCHEMA_CHECKSUM,),
            )

    def _install_authority_target_schema(self):
        self.connection.execute(AUTHORITY_TARGETS_TABLE_SQL)
        for statement in AUTHORITY_TARGETS_INDEX_SQL.split(";"):
            statement = statement.strip()
            if statement:
                self.connection.execute(statement)

    def _install_execution_schema(self):
        for statement in EXECUTION_TABLES_SQL.split(";"):
            statement = statement.strip()
            if statement:
                self.connection.execute(statement)

    def _install_dispatch_schema(self):
        for statement in DISPATCH_INTENTS_TABLE_SQL.split(";"):
            statement = statement.strip()
            if statement:
                self.connection.execute(statement)

    def _install_shadow_schema(self):
        for statement in SHADOW_EVALUATIONS_TABLE_SQL.split(";"):
            statement = statement.strip()
            if statement:
                self.connection.execute(statement)

    def _install_canary_schema(self):
        for statement in CANARY_CANDIDATES_TABLE_SQL.split(";"):
            statement = statement.strip()
            if statement:
                self.connection.execute(statement)

    def _verify_v2_before_migration(self, values):
        if values.get("schema_checksum") != SCHEMA_V2_CHECKSUM:
            raise OperationalSchemaError("operational v2 schema checksum mismatch")
        if _schema_identity(self.connection) != EXPECTED_V2_SCHEMA_IDENTITY:
            raise OperationalSchemaError("operational v2 schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational v2 SQLite integrity check failed")

    def _verify_v3_before_migration(self, values):
        if values.get("schema_checksum") != SCHEMA_V3_CHECKSUM:
            raise OperationalSchemaError("operational v3 schema checksum mismatch")
        if _schema_identity(self.connection) != EXPECTED_V3_SCHEMA_IDENTITY:
            raise OperationalSchemaError("operational v3 schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational v3 SQLite integrity check failed")

    def _verify_v4_before_migration(self, values):
        if values.get("schema_checksum") != SCHEMA_V4_CHECKSUM:
            raise OperationalSchemaError("operational v4 schema checksum mismatch")
        if _schema_identity(self.connection) != _expected_schema_identity(SCHEMA_V4_SQL):
            raise OperationalSchemaError("operational v4 schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational v4 SQLite integrity check failed")

    def _verify_v5_before_migration(self, values):
        if values.get("schema_checksum") != SCHEMA_V5_CHECKSUM:
            raise OperationalSchemaError("operational v5 schema checksum mismatch")
        if _schema_identity(self.connection) != _expected_schema_identity(SCHEMA_V5_SQL):
            raise OperationalSchemaError("operational v5 schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational v5 SQLite integrity check failed")

    def _verify_v6_before_migration(self, values):
        if values.get("schema_checksum") != SCHEMA_V6_CHECKSUM:
            raise OperationalSchemaError("operational v6 schema checksum mismatch")
        if _schema_identity(self.connection) != EXPECTED_V6_SCHEMA_IDENTITY:
            raise OperationalSchemaError("operational v6 schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational v6 SQLite integrity check failed")

    def _verify_v7_before_migration(self, values):
        if values.get("schema_checksum") != SCHEMA_V7_CHECKSUM:
            raise OperationalSchemaError("operational v7 schema checksum mismatch")
        if _schema_identity(self.connection) != EXPECTED_V7_SCHEMA_IDENTITY:
            raise OperationalSchemaError("operational v7 schema is not trusted")
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational v7 SQLite integrity check failed")

    def _rebuild_v2_fenced_leases(self):
        self.connection.execute("DROP INDEX IF EXISTS fenced_live_scope_idx")
        self.connection.execute("DROP INDEX IF EXISTS fenced_identity_idx")
        self.connection.execute(
            "CREATE TABLE fenced_leases_staged AS SELECT lease_id, host_id, "
            "profile_id, binding_generation, verified_identity_ref, 1 AS "
            "verified_identity_revision, owner_id, authority_epoch, fence_counter, "
            "idempotency_key, CASE WHEN state IN ('ACQUIRED','HEARTBEATING') "
            "THEN 'QUARANTINED' ELSE state END AS state, acquired_at, "
            "heartbeat_at, expires_at, CASE WHEN state IN ('ACQUIRED','HEARTBEATING') "
            "THEN 'identity_revision_migration' ELSE release_reason END AS release_reason "
            "FROM fenced_leases"
        )
        self.connection.execute("DROP TABLE fenced_leases")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS fenced_leases ( "
            "lease_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, "
            "profile_id TEXT NOT NULL, "
            "binding_generation INTEGER NOT NULL CHECK (binding_generation > 0), "
            "verified_identity_ref TEXT NOT NULL, "
            "verified_identity_revision INTEGER NOT NULL CHECK (verified_identity_revision > 0), "
            "owner_id TEXT NOT NULL, authority_epoch TEXT NOT NULL, "
            "fence_counter INTEGER NOT NULL CHECK (fence_counter > 0), "
            "idempotency_key TEXT NOT NULL UNIQUE, "
            "state TEXT NOT NULL CHECK (state IN "
            "('ACQUIRED','HEARTBEATING','RELEASED','EXPIRED','QUARANTINED')), "
            "acquired_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, release_reason TEXT )"
        )
        self.connection.execute(
            "INSERT INTO fenced_leases "
            "(lease_id, host_id, profile_id, binding_generation, "
            "verified_identity_ref, verified_identity_revision, owner_id, "
            "authority_epoch, fence_counter, idempotency_key, state, "
            "acquired_at, heartbeat_at, expires_at, release_reason) "
            "SELECT lease_id, host_id, profile_id, binding_generation, "
            "verified_identity_ref, verified_identity_revision, owner_id, "
            "authority_epoch, fence_counter, idempotency_key, state, acquired_at, "
            "heartbeat_at, expires_at, release_reason "
            "FROM fenced_leases_staged"
        )
        self.connection.execute("DROP TABLE fenced_leases_staged")
        self.connection.execute(
            "CREATE UNIQUE INDEX fenced_live_scope_idx ON fenced_leases("
            "host_id, profile_id, binding_generation) "
            "WHERE state IN ('ACQUIRED', 'HEARTBEATING')"
        )
        self.connection.execute(
            "CREATE UNIQUE INDEX fenced_identity_idx ON fenced_leases("
            "authority_epoch, fence_counter)"
        )

    @contextmanager
    def transaction(self, immediate=True):
        if self.connection.in_transaction:
            raise OperationalTransactionError("nested operational transaction")
        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def insert_job(
        self, job_id, kind, target_json, requested_by, idempotency_key,
        created_at=None, parent_job_id=None,
    ):
        with self.transaction():
            self.connection.execute(
                """INSERT INTO jobs
                (job_id, kind, target_json, requested_by, idempotency_key,
                 parent_job_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (
                    job_id, kind, target_json, requested_by, idempotency_key,
                    parent_job_id, created_at or _utc_now(),
                ),
            )

    def claim_job(self, job_id, owner_id, lease_name, lease_until, now=None):
        """Atomically claim one queued job and its exact lease, or return False."""
        now = now or _utc_now()
        with self.transaction():
            job = self.connection.execute(
                "SELECT status FROM jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if not job or job[0] != "queued":
                return False
            lease = self.connection.execute(
                "SELECT owner_id, expires_at FROM leases WHERE lease_name=?",
                (lease_name,),
            ).fetchone()
            if lease and lease[0] != owner_id and lease[1] > now:
                return False
            self.connection.execute(
                """INSERT INTO leases
                (lease_name, owner_id, acquired_at, heartbeat_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(lease_name) DO UPDATE SET
                  owner_id=excluded.owner_id,
                  acquired_at=excluded.acquired_at,
                  heartbeat_at=excluded.heartbeat_at,
                  expires_at=excluded.expires_at,
                  release_reason=NULL""",
                (lease_name, owner_id, now, now, lease_until),
            )
            updated = self.connection.execute(
                """UPDATE jobs SET status='claimed', owner_id=?, lease_name=?,
                   claimed_at=?, lease_until=?, attempt=attempt + 1
                   WHERE job_id=? AND status='queued'""",
                (owner_id, lease_name, now, lease_until, job_id),
            ).rowcount
            if updated != 1:
                raise OperationalTransactionError("job claim lost its transaction fence")
        return True

    def heartbeat_lease(self, lease_name, owner_id, heartbeat_at, expires_at):
        """Atomically extend one lease and only jobs bound to that lease."""
        with self.transaction():
            updated = self.connection.execute(
                """UPDATE leases SET heartbeat_at=?, expires_at=?
                   WHERE lease_name=? AND owner_id=?""",
                (heartbeat_at, expires_at, lease_name, owner_id),
            ).rowcount
            if updated != 1:
                return False
            self.connection.execute(
                """UPDATE jobs SET lease_until=?
                   WHERE owner_id=? AND lease_name=?
                   AND status IN ('claimed', 'running')""",
                (expires_at, owner_id, lease_name),
            )
        return True

    def backup_to(self, backup_path=None):
        """Create a verified SQLite backup and sidecar manifest."""
        self.connection.commit()
        self._validate_schema()
        checkpoint = self.connection.execute(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).fetchone()
        if not checkpoint or checkpoint[0] != 0:
            raise OperationalIntegrityError("operational WAL checkpoint failed")
        destination = backup_path or (
            backup_directory(self.runtime_dir)
            / ("p4_operational_" + secrets.token_hex(8) + ".sqlite3")
        )
        destination = _require_backup_path(
            destination, self.runtime_dir, allow_existing=False
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        target = sqlite3.connect(str(destination), isolation_level=None)
        try:
            self.connection.backup(target)
        finally:
            target.close()
        digest = _sha256_file(destination)
        manifest = destination.with_suffix(".manifest.json")
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": SCHEMA_VERSION,
                    "schema_checksum": SCHEMA_CHECKSUM,
                    "created_at": _utc_now(),
                    "size": destination.stat().st_size,
                    "sha256": digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ) + "\n",
            encoding="utf-8",
        )
        return destination, manifest

    @classmethod
    def restore_from(cls, runtime_dir, backup_path, manifest_path=None):
        """Restore only the operational DB, after verifying the backup."""
        runtime_dir = _resolved(runtime_dir)
        backup = _require_backup_path(backup_path, runtime_dir)
        manifest = _resolved(manifest_path or backup.with_suffix(".manifest.json"))
        try:
            manifest.relative_to(backup_directory(runtime_dir))
        except ValueError as error:
            raise OperationalPathError("manifest is outside the operational backup directory") from error
        if not manifest.is_file():
            raise OperationalIntegrityError("backup manifest is missing")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        if metadata.get("schema_version") != SCHEMA_VERSION:
            raise OperationalSchemaError("backup schema version mismatch")
        if metadata.get("schema_checksum") != SCHEMA_CHECKSUM:
            raise OperationalSchemaError("backup schema checksum mismatch")
        if metadata.get("size") != backup.stat().st_size:
            raise OperationalIntegrityError("backup size does not match manifest")
        if metadata.get("sha256") != _sha256_file(backup):
            raise OperationalIntegrityError("backup hash does not match manifest")

        source = sqlite3.connect(
            "file:" + backup.as_posix() + "?mode=ro", uri=True
        )
        temporary = operational_path(runtime_dir).with_suffix(".restore.tmp")
        temporary.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_repository = cls(source, runtime_dir, track_lifecycle=False)
            source_repository._validate_schema()
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            target = sqlite3.connect(str(temporary), isolation_level=None)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        try:
            candidate = cls._connect(temporary)
            try:
                candidate_repository = cls(candidate, runtime_dir, track_lifecycle=False)
                candidate_repository._validate_schema()
                candidate_repository.quarantine_restored_authority()
            finally:
                candidate.close()
            os.replace(temporary, operational_path(runtime_dir))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        repository = cls.open(runtime_dir)
        repository.quarantine_restored_authority()
        return repository

    def quarantine_restored_authority(self):
        """Invalidate restored authority rows and require coordinator activation."""
        with self.transaction():
            self.connection.execute(
                "UPDATE authority_state SET authority_epoch=?, fence_counter=0, "
                "quarantined=1 WHERE singleton=1",
                (_new_authority_epoch(),),
            )
            self.connection.execute(
                "UPDATE fenced_leases SET state='QUARANTINED', "
                "release_reason='operational_restore' "
                "WHERE state IN ('ACQUIRED', 'HEARTBEATING')"
            )
            self.connection.execute(
                "UPDATE authority_bound_targets SET status='quarantined', "
                "claimed_at=NULL, claimed_by=NULL, claim_lease_id=NULL, "
                "quarantine_reason='operational_restore' "
                "WHERE status IN ('requested', 'claimable', 'claimed')"
            )
            self.quarantine_execution_state("operational_restore")
            self.connection.execute(
                "UPDATE authority_bound_dispatch_intents SET status='quarantined', "
                "quarantine_reason='operational_restore' WHERE status IN "
                "('prepared', 'unknown')"
            )
            self.connection.execute(
                "UPDATE authority_bound_shadow_evaluations SET outcome='quarantined', "
                "quarantine_reason='operational_restore', quarantined_at=? "
                "WHERE outcome IN ('evaluated', 'matched')",
                (_utc_now(),),
            )
            self.connection.execute(
                "UPDATE authority_bound_canary_candidates SET state='quarantined', "
                "quarantine_reason='operational_restore', quarantined_at=? "
                "WHERE state='armed'",
                (_utc_now(),),
            )

    def quarantine_execution_state(self, reason):
        self.connection.execute(
            "UPDATE authority_bound_attempts SET status='quarantined', "
            "terminal_at=NULL, quarantine_reason=? "
            "WHERE status IN ('claimed', 'running')",
            (reason,),
        )
        self.connection.execute(
            "UPDATE authority_bound_executions SET status='quarantined', "
            "terminal_at=NULL, quarantine_reason=? "
            "WHERE status IN ('claimed', 'running')",
            (reason,),
        )

    def rows(self, query, args=()):
        return self.connection.execute(query, args).fetchall()

    def close(self):
        if self._closed:
            return
        self.connection.close()
        self._closed = True
        if self._track_lifecycle:
            count = _ACTIVE_CONNECTIONS[self.path] - 1
            if count:
                _ACTIVE_CONNECTIONS[self.path] = count
            else:
                del _ACTIVE_CONNECTIONS[self.path]
                _BOOTSTRAP_REQUIRED[self.path] = True
