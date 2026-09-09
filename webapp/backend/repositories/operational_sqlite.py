"""Isolated operational SQLite foundation for P4.

This module owns only mutable P4 operational state. It is deliberately not
imported by Flask routes, the scheduler, or the Phase 3 immutable generation
reader. The exact operational path is guarded so a P3 generation cannot be
opened for writes accidentally.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3


SCHEMA_VERSION = 1
OPERATIONAL_FILENAME = "p4_operational.sqlite3"
OPERATIONAL_DIRNAME = "operational"
BACKUP_DIRNAME = "backups"

SCHEMA_SQL = """
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
CREATE INDEX IF NOT EXISTS jobs_owner_idx ON jobs(owner_id, lease_until);

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


def _integrity_ok(connection):
    result = connection.execute("PRAGMA integrity_check").fetchone()
    if not result or result[0] != "ok":
        return False
    return not connection.execute("PRAGMA foreign_key_check").fetchone()


class OperationalSQLiteRepository:
    """Connection and transaction primitives for the mutable P4 database."""

    def __init__(self, connection, runtime_dir):
        self.connection = connection
        self.runtime_dir = _resolved(runtime_dir)
        self.path = operational_path(runtime_dir)

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
        try:
            connection.executescript(SCHEMA_SQL)
            with cls(connection, runtime_dir).transaction():
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
                connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                    ("schema_checksum", SCHEMA_CHECKSUM),
                )
        except Exception:
            connection.close()
            try:
                path.unlink()
            except OSError:
                pass
            raise
        repository = cls(connection, runtime_dir)
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
        if not _integrity_ok(self.connection):
            raise OperationalIntegrityError("operational SQLite integrity check failed")

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
        """Atomically claim one queued job and its lease, or return False."""
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
                """UPDATE jobs SET status='claimed', owner_id=?, claimed_at=?,
                   lease_until=?, attempt=attempt + 1
                   WHERE job_id=? AND status='queued'""",
                (owner_id, now, lease_until, job_id),
            ).rowcount
            if updated != 1:
                raise OperationalTransactionError("job claim lost its transaction fence")
        return True

    def heartbeat_lease(self, lease_name, owner_id, heartbeat_at, expires_at):
        """Atomically extend a lease and all jobs owned by that lease owner."""
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
                   WHERE owner_id=? AND status IN ('claimed', 'running')""",
                (expires_at, owner_id),
            )
        return True

    def backup_to(self, backup_path=None):
        """Create a verified SQLite backup and sidecar manifest."""
        self.connection.commit()
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
            if not _integrity_ok(source):
                raise OperationalIntegrityError("backup integrity check failed")
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
            restored = sqlite3.connect(str(temporary), isolation_level=None)
            try:
                if not _integrity_ok(restored):
                    raise OperationalIntegrityError("restored database integrity failed")
            finally:
                restored.close()
            os.replace(temporary, operational_path(runtime_dir))
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        return cls.open(runtime_dir)

    def rows(self, query, args=()):
        return self.connection.execute(query, args).fetchall()

    def close(self):
        self.connection.close()
