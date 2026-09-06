"""Versioned SQLite candidate store for Phase 3 shadow imports.

This module is deliberately not wired into Flask routes. It owns only a new
candidate database and never reads or writes ai tool files or processes.
"""
from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3


SCHEMA_VERSION = 1
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_runs (
    run_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'verified', 'failed')),
    checks_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT
);

CREATE TABLE IF NOT EXISTS source_snapshots (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    content_type TEXT NOT NULL,
    raw_bytes BLOB NOT NULL,
    raw_sha256 TEXT NOT NULL,
    canonical_sha256 TEXT NOT NULL,
    PRIMARY KEY (run_id, endpoint),
    UNIQUE (run_id, position)
);

CREATE TABLE IF NOT EXISTS master_clients (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    remote_name TEXT,
    group_name TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    status TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, client_id),
    UNIQUE (run_id, position)
);

CREATE TABLE IF NOT EXISTS schedule_slots (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    group_name TEXT NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, position)
);

CREATE TABLE IF NOT EXISTS database_meta (
    run_id TEXT PRIMARY KEY REFERENCES import_runs(run_id),
    last_updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS database_clients (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    remote_idx INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    group_name TEXT NOT NULL,
    selected INTEGER NOT NULL CHECK (selected IN (0, 1)),
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, position),
    UNIQUE (run_id, client_id),
    FOREIGN KEY (run_id, client_id) REFERENCES master_clients(run_id, client_id)
);

CREATE TABLE IF NOT EXISTS database_schedule (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, position)
);

CREATE TABLE IF NOT EXISTS public_settings (
    run_id TEXT PRIMARY KEY REFERENCES import_runs(run_id),
    tunnel_port INTEGER CHECK (tunnel_port BETWEEN 1 AND 65535),
    auto_restart_tunnel INTEGER CHECK (auto_restart_tunnel IN (0, 1)),
    auto_telegram INTEGER CHECK (auto_telegram IN (0, 1)),
    auto_open_browser INTEGER CHECK (auto_open_browser IN (0, 1)),
    observed_at TEXT
);

CREATE TABLE IF NOT EXISTS cycle_state (
    run_id TEXT PRIMARY KEY REFERENCES import_runs(run_id),
    today TEXT NOT NULL,
    state_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cycle_slot_state (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    today TEXT NOT NULL,
    position INTEGER NOT NULL,
    slot_key TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (run_id, today, slot_key),
    UNIQUE (run_id, today, position)
);

CREATE TABLE IF NOT EXISTS manual_overrides (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    client_id TEXT NOT NULL,
    until_at TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, client_id),
    UNIQUE (run_id, position)
);

CREATE TABLE IF NOT EXISTS audit_events (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    stream TEXT NOT NULL CHECK (stream IN ('activity', 'change', 'action')),
    stream_seq INTEGER NOT NULL,
    source_offset INTEGER NOT NULL,
    raw_line BLOB NOT NULL,
    parsed_json TEXT,
    PRIMARY KEY (run_id, stream, stream_seq)
);

CREATE TABLE IF NOT EXISTS ai_fix_requests (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    file_name TEXT NOT NULL,
    lifecycle_status TEXT NOT NULL,
    kind TEXT NOT NULL,
    command TEXT,
    user_text TEXT,
    result_json TEXT,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, position),
    UNIQUE (run_id, file_name)
);

CREATE TABLE IF NOT EXISTS backup_metadata (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime TEXT NOT NULL,
    label TEXT NOT NULL,
    created_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (run_id, position),
    UNIQUE (run_id, name)
);

CREATE TABLE IF NOT EXISTS source_observations (
    run_id TEXT NOT NULL REFERENCES import_runs(run_id),
    endpoint TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, endpoint)
);
"""
SCHEMA_CHECKSUM = hashlib.sha256(SCHEMA_SQL.encode("utf-8")).hexdigest()


class CandidatePathError(ValueError):
    """The requested candidate path is not disposable and new."""


class SQLiteCandidateRepository:
    """Typed repository for one new candidate database."""

    def __init__(self, connection, path):
        self.connection = connection
        self.path = Path(path)

    @staticmethod
    def _connect(path):
        connection = sqlite3.connect(str(path))
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @classmethod
    def create(cls, path):
        candidate = Path(path)
        if candidate.exists():
            raise CandidatePathError("candidate database already exists")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        connection = cls._connect(candidate)
        connection.executescript(SCHEMA_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version, checksum, applied_at) VALUES (?, ?, datetime('now'))",
            (SCHEMA_VERSION, SCHEMA_CHECKSUM),
        )
        connection.commit()
        return cls(connection, candidate)

    @classmethod
    def open_existing(cls, path):
        candidate = Path(path)
        if not candidate.is_file():
            raise CandidatePathError("candidate database does not exist")
        return cls(cls._connect(candidate), candidate)

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def close(self):
        self.connection.close()

    def begin_import(self, run_id, snapshot_id, source_hash, started_at):
        self.connection.execute(
            "INSERT INTO import_runs(run_id, snapshot_id, source_hash, started_at, status) VALUES (?, ?, ?, ?, 'running')",
            (run_id, snapshot_id, source_hash, started_at),
        )

    def add_source_snapshot(self, run_id, position, value):
        self.connection.execute(
            """INSERT INTO source_snapshots
            (run_id, position, endpoint, content_type, raw_bytes, raw_sha256, canonical_sha256)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (run_id, position, value.endpoint, value.content_type, value.body,
             value.raw_sha256, value.canonical_sha256),
        )

    def add_master_client(self, run_id, position, client):
        self.connection.execute(
            """INSERT INTO master_clients
            (run_id, position, client_id, display_name, remote_name, group_name,
             selected, status, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, position, client["client_id"], client["display_name"],
             client.get("remote_name"), client["group_name"],
             int(client["selected"]), client.get("status"), client["raw_json"]),
        )

    def add_schedule_slot(self, run_id, position, slot):
        self.connection.execute(
            """INSERT INTO schedule_slots
            (run_id, position, group_name, open_time, close_time, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, position, slot["group_name"], slot["open_time"],
             slot["close_time"], slot["raw_json"]),
        )

    def set_database_meta(self, run_id, last_updated):
        self.connection.execute(
            "INSERT INTO database_meta(run_id, last_updated) VALUES (?, ?)",
            (run_id, last_updated),
        )

    def add_database_client(self, run_id, position, client):
        self.connection.execute(
            """INSERT INTO database_clients
            (run_id, position, remote_idx, client_id, display_name, status,
             group_name, selected, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, position, client["remote_idx"], client["client_id"],
             client["display_name"], client["status"], client["group_name"],
             int(client["selected"]), client["raw_json"]),
        )

    def add_database_schedule(self, run_id, position, raw_json):
        self.connection.execute(
            "INSERT INTO database_schedule(run_id, position, raw_json) VALUES (?, ?, ?)",
            (run_id, position, raw_json),
        )

    def set_public_settings(self, run_id, settings, observed_at):
        self.connection.execute(
            """INSERT INTO public_settings
            (run_id, tunnel_port, auto_restart_tunnel, auto_telegram,
             auto_open_browser, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, settings.get("tunnel_port"),
             None if settings.get("auto_restart_tunnel") is None else int(settings["auto_restart_tunnel"]),
             None if settings.get("auto_telegram") is None else int(settings["auto_telegram"]),
             None if settings.get("auto_open_browser") is None else int(settings["auto_open_browser"]),
             observed_at),
        )

    def set_cycle_state(self, run_id, today, state_json):
        self.connection.execute(
            "INSERT INTO cycle_state(run_id, today, state_json) VALUES (?, ?, ?)",
            (run_id, today, state_json),
        )

    def add_cycle_slot(self, run_id, today, position, slot_key, result):
        self.connection.execute(
            "INSERT INTO cycle_slot_state(run_id, today, position, slot_key, result) VALUES (?, ?, ?, ?, ?)"",
            (run_id, today, position, slot_key, result),
        )

    def add_manual_override(self, run_id, position, override):
        self.connection.execute(
            """INSERT INTO manual_overrides
            (run_id, position, client_id, until_at, detected_at, from_state, to_state, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, position, override["client_id"], override["until_at"],
             override["detected_at"], override["from_state"],
             override["to_state"], override["raw_json"]),
        )

    def add_audit_event(self, run_id, event):
        self.connection.execute(
            """INSERT INTO audit_events
            (run_id, stream, stream_seq, source_offset, raw_line, parsed_json)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, event["stream"], event["stream_seq"],
             event["source_offset"], event["raw_line"], event.get("parsed_json")),
        )

    def add_ai_fix_request(self, run_id, position, item):
        self.connection.execute(
            """INSERT INTO ai_fix_requests
            (run_id, position, file_name, lifecycle_status, kind, command,
             user_text, result_json, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, position, item["file_name"], item["lifecycle_status"],
             item["kind"], item.get("command"), item.get("user_text"),
             item.get("result_json"), item["raw_json"]),
        )

    def add_backup(self, run_id, position, backup):
        self.connection.execute(
            """INSERT INTO backup_metadata
            (run_id, position, name, size, mtime, label, created_at, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (run_id, position, backup["name"], backup["size"],
             backup["mtime"], backup["label"], backup["created_at"],
             backup["raw_json"]),
        )

    def add_observation(self, run_id, endpoint, payload_json):
        self.connection.execute(
            "INSERT INTO source_observations(run_id, endpoint, payload_json) VALUES (?, ?, ?)",
            (run_id, endpoint, payload_json),
        )

    def finish_import(self, run_id, completed_at, checks_json, status="verified", error_code=None):
        self.connection.execute(
            """UPDATE import_runs SET completed_at=?, status=?, checks_json=?, error_code=?
            WHERE run_id=?""",
            (completed_at, status, checks_json, error_code, run_id),
        )

    def rows(self, query, args=()):
        return self.connection.execute(query, args).fetchall()

    def integrity_check(self):
        result = self.connection.execute("PRAGMA integrity_check").fetchone()
        return result[0] if result else ""
