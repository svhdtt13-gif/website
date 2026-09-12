"""Profile-scoped portable domain store.

This database is deliberately separate from the P4 operational SQLite. It stores
portable domain state only; live process and host-runtime state stay elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_VERSION = 3
STORE_KIND = "portable_domain_store"
DB_FILENAME = "portable_domain.sqlite3"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hosts (
    host_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    origin_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_profiles (
    profile_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    verified_identity_ref TEXT,
    status TEXT NOT NULL CHECK (status IN ('VERIFIED', 'ACTIVE', 'OFFLINE', 'NEEDS_LOGIN')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS host_profile_bindings (
    binding_id TEXT PRIMARY KEY,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    binding_generation INTEGER NOT NULL CHECK (binding_generation > 0),
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'OFFLINE', 'RETIRED')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(host_id, profile_id, binding_generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_binding_per_host
    ON host_profile_bindings(host_id) WHERE state = 'ACTIVE';

CREATE UNIQUE INDEX IF NOT EXISTS one_active_host_per_profile
    ON host_profile_bindings(profile_id) WHERE state = 'ACTIVE';

CREATE TABLE IF NOT EXISTS profile_clients (
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    client_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    group_name TEXT NOT NULL,
    status TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    PRIMARY KEY(profile_id, client_id)
);

CREATE TABLE IF NOT EXISTS profile_schedules (
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    schedule_id TEXT NOT NULL,
    group_name TEXT NOT NULL,
    open_time TEXT NOT NULL,
    close_time TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY(profile_id, schedule_id)
);

CREATE TABLE IF NOT EXISTS profile_policies (
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    policy_id TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    policy_value TEXT NOT NULL,
    PRIMARY KEY(profile_id, policy_id),
    UNIQUE(profile_id, policy_key)
);

CREATE TABLE IF NOT EXISTS profile_control_intents (
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    intent_id TEXT NOT NULL,
    intent_type TEXT NOT NULL CHECK (intent_type IN ('cycle_stopped')),
    state TEXT NOT NULL CHECK (state IN ('REQUESTED', 'CLEARED')),
    requested_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY(profile_id, intent_id)
);

CREATE TABLE IF NOT EXISTS profile_observations (
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    observation_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    observation_key TEXT NOT NULL,
    observation_value TEXT NOT NULL,
    PRIMARY KEY(profile_id, observation_id)
);

CREATE TABLE IF NOT EXISTS profile_audit_events (
    profile_id TEXT NOT NULL REFERENCES remote_profiles(profile_id),
    event_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_ref TEXT NOT NULL,
    summary TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    PRIMARY KEY(profile_id, event_id)
);
"""

SCHEMA_CHECKSUM = hashlib.sha256(
    " ".join(line.strip() for line in SCHEMA_SQL.splitlines() if line.strip()).encode()
).hexdigest()


def _migrate_v3(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(remote_profiles)")
    }
    if "verified_identity_ref" not in columns:
        connection.execute(
            "ALTER TABLE remote_profiles ADD COLUMN verified_identity_ref TEXT"
        )
MIGRATIONS: dict[int, str | Callable[[sqlite3.Connection], None]] = {
    1: SCHEMA_SQL,
    2: "CREATE UNIQUE INDEX IF NOT EXISTS one_active_host_per_profile "
       "ON host_profile_bindings(profile_id) WHERE state = 'ACTIVE';",
    3: _migrate_v3,
}


class PortableStoreError(ValueError):
    """Base error for fail-closed portable store operations."""


class ProfileRequiredError(PortableStoreError):
    pass


class BindingError(PortableStoreError):
    pass


class SchemaError(PortableStoreError):
    pass


def _required(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PortableStoreError(f"{label} is required")
    return value.strip()


def _safe_reference(value: str, label: str) -> str:
    value = _required(value, label)
    lowered = value.lower()
    if "://" in value or any(marker in lowered for marker in (
        "password", "token", "cookie", "session", "secret", "telegram_token",
    )):
        raise PortableStoreError(f"{label} must be a non-secret logical reference")
    return value


def _profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str) or not profile_id.strip():
        raise ProfileRequiredError("profile_id is required")
    return profile_id.strip()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class PortableDomainStore:
    """Small repository with profile-scoped signatures for every domain read."""

    def __init__(self, connection: sqlite3.Connection, path: Path):
        self.connection = connection
        self.path = path
        self._transaction_depth = 0

    @classmethod
    def create(cls, path: Path | str) -> PortableDomainStore:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size:
            raise PortableStoreError("portable store path must be new")
        connection = sqlite3.connect(target)
        store = cls(connection, target)
        store._configure()
        store.migrate()
        return store

    @classmethod
    def open(cls, path: Path | str) -> PortableDomainStore:
        target = Path(path)
        if not target.is_file():
            raise PortableStoreError("portable store does not exist")
        connection = sqlite3.connect(target)
        store = cls(connection, target)
        store._configure()
        store.migrate()
        store._verify()
        return store

    def _configure(self) -> None:
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")

    def migrate(self) -> None:
        current = 0
        if self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone():
            raw_version = self.connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            current = int(raw_version[0]) if raw_version else 0
        if current > SCHEMA_VERSION:
            raise SchemaError("portable schema version is newer than this code")
        for target in range(current + 1, SCHEMA_VERSION + 1):
            with self.transaction():
                migration = MIGRATIONS[target]
                if callable(migration):
                    migration(self.connection)
                else:
                    self.connection.executescript(migration)
                self.connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('store_kind', ?)",
                    (STORE_KIND,),
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)",
                    (str(target),),
                )
                self.connection.execute(
                    "INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_checksum', ?)",
                    (SCHEMA_CHECKSUM,),
                )
        if current == SCHEMA_VERSION:
            with self.transaction():
                self.connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('store_kind', ?)",
                    (STORE_KIND,),
                )
                self.connection.execute(
                    "INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('schema_checksum', ?)",
                    (SCHEMA_CHECKSUM,),
                )
        self._verify()

    def _verify(self) -> None:
        meta = dict(self.connection.execute("SELECT key, value FROM schema_meta"))
        if meta.get("store_kind") != STORE_KIND:
            raise SchemaError("not a portable domain store")
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise SchemaError("unsupported portable schema version")
        if meta.get("schema_checksum") != SCHEMA_CHECKSUM:
            raise SchemaError("portable store schema checksum mismatch")
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise SchemaError("portable store integrity check failed")

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        outer = self._transaction_depth == 0
        self._transaction_depth += 1
        try:
            if outer:
                if immediate:
                    self.connection.execute("BEGIN IMMEDIATE")
                with self.connection:
                    yield self.connection
            else:
                yield self.connection
        except sqlite3.IntegrityError as exc:
            raise BindingError(str(exc)) from exc
        finally:
            self._transaction_depth -= 1

    def close(self) -> None:
        self.connection.close()

    def add_host(self, host_id: str, display_name: str, origin_ref: str, now: str) -> None:
        host_id, display_name, origin_ref, now = (
            _required(host_id, "host_id"), _required(display_name, "display_name"),
            _safe_reference(origin_ref, "origin_ref"), _required(now, "now")
        )
        with self.transaction():
            self.connection.execute(
                "INSERT INTO hosts VALUES (?, ?, ?, ?, ?)",
                (host_id, display_name, origin_ref, now, now),
            )

    def ensure_host(self, host_id: str, display_name: str, origin_ref: str, now: str) -> None:
        host_id, display_name, origin_ref, now = (
            _required(host_id, "host_id"), _required(display_name, "display_name"),
            _safe_reference(origin_ref, "origin_ref"), _required(now, "now")
        )
        existing = self.connection.execute(
            "SELECT origin_ref FROM hosts WHERE host_id=?", (host_id,)
        ).fetchone()
        if existing is None:
            return self.add_host(host_id, display_name, origin_ref, now)
        if existing[0] != origin_ref:
            raise BindingError("host origin_ref does not match existing host")
        with self.transaction():
            self.connection.execute(
                "UPDATE hosts SET display_name=?, updated_at=? WHERE host_id=?",
                (display_name, now, host_id),
            )

    def register_host(self, host_id: str, display_name: str, origin_ref: str, now: str) -> dict[str, object]:
        """Create or confirm a host without changing an existing identity."""
        host_id, display_name, origin_ref, now = (
            _required(host_id, "host_id"), _required(display_name, "display_name"),
            _safe_reference(origin_ref, "origin_ref"), _required(now, "now")
        )
        with self.transaction(immediate=True):
            existing = self.connection.execute(
                "SELECT host_id, display_name, origin_ref FROM hosts WHERE host_id=?",
                (host_id,),
            ).fetchone()
            if existing is None:
                self.connection.execute(
                    "INSERT INTO hosts VALUES (?, ?, ?, ?, ?)",
                    (host_id, display_name, origin_ref, now, now),
                )
                return {"host_id": host_id, "display_name": display_name, "created": True}
            if existing["display_name"] != display_name:
                raise BindingError("host display_name does not match existing host")
            return {
                "host_id": existing["host_id"],
                "display_name": existing["display_name"],
                "created": False,
            }

    def add_profile(self, profile_id: str, display_name: str, account_ref: str,
                    status: str, now: str) -> None:
        profile_id, display_name, account_ref, status, now = (
            _profile_id(profile_id), _required(display_name, "display_name"),
            _safe_reference(account_ref, "account_ref"), _required(status, "status"),
            _required(now, "now")
        )
        if status not in {"VERIFIED", "ACTIVE", "OFFLINE", "NEEDS_LOGIN"}:
            raise PortableStoreError("invalid profile status")
        with self.transaction():
            self.connection.execute(
                "INSERT INTO remote_profiles VALUES (?, ?, ?, ?, ?, ?, ?)",
                (profile_id, display_name, account_ref, None, status, now, now),
            )

    def record_verified_identity(
        self, profile_id: str, verified_identity_ref: str, now: str
    ) -> None:
        """Persist only an explicit non-secret remote identity proof reference."""
        profile_id = _profile_id(profile_id)
        verified_identity_ref = _safe_reference(
            verified_identity_ref, "verified_identity_ref"
        )
        now = _required(now, "now")
        with self.transaction(immediate=True):
            profile = self.connection.execute(
                "SELECT account_ref FROM remote_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if profile is None:
                raise BindingError("unknown profile_id")
            if profile[0] == verified_identity_ref:
                raise BindingError("verified identity must not reuse account_ref")
            updated = self.connection.execute(
                "UPDATE remote_profiles SET verified_identity_ref=?, updated_at=? "
                "WHERE profile_id=?",
                (verified_identity_ref, now, profile_id),
            ).rowcount
            if updated != 1:
                raise BindingError("verified identity could not be recorded")

    def ensure_profile(self, profile_id: str, display_name: str, account_ref: str,
                       status: str, now: str) -> None:
        profile_id, display_name, account_ref, status, now = (
            _profile_id(profile_id), _required(display_name, "display_name"),
            _safe_reference(account_ref, "account_ref"), _required(status, "status"),
            _required(now, "now")
        )
        existing = self.connection.execute(
            "SELECT account_ref FROM remote_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if existing is None:
            return self.add_profile(profile_id, display_name, account_ref, status, now)
        if existing[0] != account_ref:
            raise BindingError("profile account_ref does not match existing profile")
        with self.transaction():
            self.connection.execute(
                "UPDATE remote_profiles SET display_name=?, status=?, updated_at=? WHERE profile_id=?",
                (display_name, status, now, profile_id),
            )

    def bind_profile(self, binding_id: str, host_id: str, profile_id: str,
                     account_ref: str, binding_generation: int, state: str, now: str) -> None:
        binding_id, host_id, profile_id, account_ref, state, now = (
            _required(binding_id, "binding_id"), _required(host_id, "host_id"),
            _profile_id(profile_id), _safe_reference(account_ref, "account_ref"),
            _required(state, "state"), _required(now, "now")
        )
        if not isinstance(binding_generation, int) or binding_generation < 1:
            raise BindingError("binding_generation must be positive")
        if state not in {"ACTIVE", "OFFLINE", "RETIRED"}:
            raise BindingError("invalid binding state")
        profile = self.connection.execute(
            "SELECT account_ref FROM remote_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if profile is None or profile[0] != account_ref:
            raise BindingError("binding account_ref does not match profile")
        with self.transaction():
            self.connection.execute(
                "INSERT INTO host_profile_bindings VALUES (?, ?, ?, ?, ?, ?, ?)",
                (binding_id, host_id, profile_id, binding_generation, state, now, now),
            )

    def binding_snapshot(self, host_id: str, profile_id: str,
                         binding_generation: int) -> dict[str, object]:
        host_id, profile_id = _required(host_id, "host_id"), _profile_id(profile_id)
        if not isinstance(binding_generation, int) or binding_generation < 1:
            raise BindingError("binding_generation must be positive")
        rows = self.connection.execute(
            "SELECT b.host_id, b.profile_id, b.binding_generation, b.state, "
            "h.origin_ref, p.account_ref, p.verified_identity_ref, p.status "
            "FROM host_profile_bindings AS b "
            "JOIN hosts AS h ON h.host_id=b.host_id "
            "JOIN remote_profiles AS p ON p.profile_id=b.profile_id "
            "WHERE b.host_id=? AND b.profile_id=? AND b.binding_generation=?",
            (host_id, profile_id, binding_generation),
        ).fetchall()
        if len(rows) != 1:
            raise BindingError("binding snapshot is not unique")
        return dict(rows[0])

    def ensure_offline_binding(self, host_id: str, profile_id: str, now: str) -> dict[str, object]:
        """Create or confirm only an OFFLINE binding for an existing host/profile."""
        host_id, profile_id, now = (
            _required(host_id, "host_id"), _profile_id(profile_id), _required(now, "now")
        )
        with self.transaction(immediate=True):
            if self.connection.execute(
                "SELECT 1 FROM hosts WHERE host_id=?", (host_id,)
            ).fetchone() is None:
                raise BindingError("unknown host")
            if self.connection.execute(
                "SELECT 1 FROM remote_profiles WHERE profile_id=?", (profile_id,)
            ).fetchone() is None:
                raise BindingError("unknown profile")
            rows = self.connection.execute(
                "SELECT binding_id, host_id, profile_id, binding_generation, state, updated_at "
                "FROM host_profile_bindings WHERE host_id=? AND profile_id=? "
                "ORDER BY binding_generation DESC, binding_id",
                (host_id, profile_id),
            ).fetchall()
            if any(row["state"] == "ACTIVE" for row in rows):
                raise BindingError("active binding cannot be changed")
            offline = next((row for row in rows if row["state"] == "OFFLINE"), None)
            if offline is not None:
                return {
                    "binding_id": offline["binding_id"],
                    "host_id": offline["host_id"],
                    "profile_id": offline["profile_id"],
                    "binding_generation": offline["binding_generation"],
                    "state": offline["state"],
                    "updated_at": offline["updated_at"],
                    "created": False,
                }

            generation = max((int(row["binding_generation"]) for row in rows), default=0) + 1
            binding_id = "binding-" + hashlib.sha256(
                f"{host_id}\0{profile_id}\0{generation}".encode()
            ).hexdigest()[:24]
            self.connection.execute(
                "INSERT OR IGNORE INTO host_profile_bindings "
                "(binding_id, host_id, profile_id, binding_generation, state, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 'OFFLINE', ?, ?)",
                (binding_id, host_id, profile_id, generation, now, now),
            )
            created = self.connection.execute(
                "SELECT binding_id, host_id, profile_id, binding_generation, state, updated_at "
                "FROM host_profile_bindings WHERE binding_id=?",
                (binding_id,),
            ).fetchone()
            if created is None:
                raise BindingError("offline binding could not be confirmed")
            return {
                "binding_id": created["binding_id"],
                "host_id": created["host_id"],
                "profile_id": created["profile_id"],
                "binding_generation": created["binding_generation"],
                "state": created["state"],
                "updated_at": created["updated_at"],
                "created": True,
            }

    def ensure_binding(self, binding_id: str, host_id: str, profile_id: str,
                       account_ref: str, binding_generation: int, state: str, now: str) -> None:
        binding_id, host_id, profile_id, account_ref, state, now = (
            _required(binding_id, "binding_id"), _required(host_id, "host_id"),
            _profile_id(profile_id), _safe_reference(account_ref, "account_ref"),
            _required(state, "state"), _required(now, "now")
        )
        existing = self.connection.execute(
            "SELECT host_id, profile_id, binding_generation, state "
            "FROM host_profile_bindings WHERE binding_id=?", (binding_id,)
        ).fetchone()
        if existing is None:
            return self.bind_profile(binding_id, host_id, profile_id, account_ref,
                                     binding_generation, state, now)
        if tuple(existing) != (host_id, profile_id, binding_generation, state):
            raise BindingError("binding does not match existing explicit mapping")
        profile = self.connection.execute(
            "SELECT account_ref FROM remote_profiles WHERE profile_id=?", (profile_id,)
        ).fetchone()
        if profile is None or profile[0] != account_ref:
            raise BindingError("binding account_ref does not match profile")
        with self.transaction():
            self.connection.execute(
                "UPDATE host_profile_bindings SET updated_at=? WHERE binding_id=?",
                (now, binding_id),
            )

    def upsert_client(self, profile_id: str, client_id: str, display_name: str,
                      group_name: str, status: str, source_ref: str) -> None:
        profile_id, client_id, display_name, group_name, status, source_ref = (
            _profile_id(profile_id), _required(client_id, "client_id"),
            _required(display_name, "display_name"), _required(group_name, "group_name"),
            _required(status, "status"), _safe_reference(source_ref, "source_ref")
        )
        with self.transaction():
            self.connection.execute(
                "INSERT INTO profile_clients VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, client_id) DO UPDATE SET display_name=excluded.display_name, "
                "group_name=excluded.group_name, status=excluded.status, source_ref=excluded.source_ref",
                (profile_id, client_id, display_name, group_name, status, source_ref),
            )

    def list_clients(self, profile_id: str) -> list[dict[str, object]]:
        profile_id = _profile_id(profile_id)
        rows = self.connection.execute(
            "SELECT client_id, display_name, group_name, status, source_ref "
            "FROM profile_clients WHERE profile_id=? ORDER BY client_id", (profile_id,)
        )
        return [dict(row) for row in rows]

    def reconcile_clients(self, profile_id: str, client_ids: set[str]) -> None:
        self._delete_unlisted("profile_clients", "client_id", profile_id, client_ids)

    def upsert_schedule(self, profile_id: str, schedule_id: str, group_name: str,
                        open_time: str, close_time: str, enabled: bool) -> None:
        values = [_profile_id(profile_id), _required(schedule_id, "schedule_id"),
                  _required(group_name, "group_name"), _required(open_time, "open_time"),
                  _required(close_time, "close_time"), int(bool(enabled))]
        with self.transaction():
            self.connection.execute(
                "INSERT INTO profile_schedules VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, schedule_id) DO UPDATE SET group_name=excluded.group_name, "
                "open_time=excluded.open_time, close_time=excluded.close_time, enabled=excluded.enabled",
                values,
            )

    def list_schedules(self, profile_id: str) -> list[dict[str, object]]:
        profile_id = _profile_id(profile_id)
        return [dict(row) for row in self.connection.execute(
            "SELECT schedule_id, group_name, open_time, close_time, enabled "
            "FROM profile_schedules WHERE profile_id=? ORDER BY schedule_id", (profile_id,)
        )]

    def reconcile_schedules(self, profile_id: str, schedule_ids: set[str]) -> None:
        self._delete_unlisted("profile_schedules", "schedule_id", profile_id, schedule_ids)

    def upsert_policy(self, profile_id: str, policy_id: str, policy_key: str,
                      policy_value: str) -> None:
        values = [_profile_id(profile_id), _required(policy_id, "policy_id"),
                  _required(policy_key, "policy_key"), _safe_reference(policy_value, "policy_value")]
        with self.transaction():
            self.connection.execute(
                "INSERT INTO profile_policies VALUES (?, ?, ?, ?) "
                "ON CONFLICT(profile_id, policy_id) DO UPDATE SET policy_key=excluded.policy_key, "
                "policy_value=excluded.policy_value",
                values,
            )

    def list_policies(self, profile_id: str) -> list[dict[str, object]]:
        profile_id = _profile_id(profile_id)
        return [dict(row) for row in self.connection.execute(
            "SELECT policy_id, policy_key, policy_value FROM profile_policies "
            "WHERE profile_id=? ORDER BY policy_id", (profile_id,)
        )]

    def reconcile_policies(self, profile_id: str, policy_ids: set[str]) -> None:
        self._delete_unlisted("profile_policies", "policy_id", profile_id, policy_ids)

    def _delete_unlisted(self, table: str, key_column: str, profile_id: str,
                         keep_ids: set[str]) -> None:
        profile_id = _profile_id(profile_id)
        keep_ids = {_required(value, key_column) for value in keep_ids}
        query = f"DELETE FROM {table} WHERE profile_id=?"
        values: list[str] = [profile_id]
        if keep_ids:
            placeholders = ", ".join("?" for _ in keep_ids)
            query += f" AND {key_column} NOT IN ({placeholders})"
            values.extend(sorted(keep_ids))
        with self.transaction():
            self.connection.execute(query, values)

    def upsert_cycle_stopped(self, profile_id: str, intent_id: str, state: str,
                             requested_at: str, source_ref: str, reason: str) -> None:
        values = [_profile_id(profile_id), _required(intent_id, "intent_id"),
                  _required(state, "state"), _required(requested_at, "requested_at"),
                  _safe_reference(source_ref, "source_ref"), _required(reason, "reason")]
        if state not in {"REQUESTED", "CLEARED"}:
            raise PortableStoreError("invalid cycle_stopped state")
        with self.transaction():
            self.connection.execute(
                "INSERT INTO profile_control_intents VALUES (?, ?, 'cycle_stopped', ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, intent_id) DO UPDATE SET state=excluded.state, "
                "requested_at=excluded.requested_at, source_ref=excluded.source_ref, reason=excluded.reason",
                values,
            )

    def list_control_intents(self, profile_id: str) -> list[dict[str, object]]:
        profile_id = _profile_id(profile_id)
        return [dict(row) for row in self.connection.execute(
            "SELECT intent_id, intent_type, state, requested_at, source_ref, reason "
            "FROM profile_control_intents WHERE profile_id=? ORDER BY intent_id", (profile_id,)
        )]

    def add_observation(self, profile_id: str, observation_id: str, observed_at: str,
                        source_ref: str, observation_key: str, observation_value: str) -> None:
        values = [_profile_id(profile_id), _required(observation_id, "observation_id"),
                  _required(observed_at, "observed_at"), _safe_reference(source_ref, "source_ref"),
                  _required(observation_key, "observation_key"), _safe_reference(observation_value, "observation_value")]
        with self.transaction():
            self.connection.execute("INSERT INTO profile_observations VALUES (?, ?, ?, ?, ?, ?)", values)

    def upsert_observation(self, profile_id: str, observation_id: str, observed_at: str,
                           source_ref: str, observation_key: str, observation_value: str) -> None:
        values = [_profile_id(profile_id), _required(observation_id, "observation_id"),
                  _required(observed_at, "observed_at"), _safe_reference(source_ref, "source_ref"),
                  _required(observation_key, "observation_key"), _safe_reference(observation_value, "observation_value")]
        with self.transaction():
            self.connection.execute(
                "INSERT INTO profile_observations VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, observation_id) DO UPDATE SET observed_at=excluded.observed_at, "
                "source_ref=excluded.source_ref, observation_key=excluded.observation_key, "
                "observation_value=excluded.observation_value",
                values,
            )

    def list_observations(self, profile_id: str) -> list[dict[str, object]]:
        profile_id = _profile_id(profile_id)
        return [dict(row) for row in self.connection.execute(
            "SELECT observation_id, observed_at, source_ref, observation_key, observation_value "
            "FROM profile_observations WHERE profile_id=? ORDER BY observation_id", (profile_id,)
        )]

    def add_audit_event(self, profile_id: str, event_id: str, occurred_at: str,
                        event_type: str, actor_ref: str, summary: str, source_ref: str) -> None:
        values = [_profile_id(profile_id), _required(event_id, "event_id"),
                  _required(occurred_at, "occurred_at"), _required(event_type, "event_type"),
                  _safe_reference(actor_ref, "actor_ref"), _required(summary, "summary"),
                  _safe_reference(source_ref, "source_ref")]
        with self.transaction():
            self.connection.execute("INSERT INTO profile_audit_events VALUES (?, ?, ?, ?, ?, ?, ?)", values)

    def upsert_audit_event(self, profile_id: str, event_id: str, occurred_at: str,
                           event_type: str, actor_ref: str, summary: str, source_ref: str) -> None:
        values = [_profile_id(profile_id), _required(event_id, "event_id"),
                  _required(occurred_at, "occurred_at"), _required(event_type, "event_type"),
                  _safe_reference(actor_ref, "actor_ref"), _required(summary, "summary"),
                  _safe_reference(source_ref, "source_ref")]
        with self.transaction():
            self.connection.execute(
                "INSERT INTO profile_audit_events VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(profile_id, event_id) DO UPDATE SET occurred_at=excluded.occurred_at, "
                "event_type=excluded.event_type, actor_ref=excluded.actor_ref, summary=excluded.summary, "
                "source_ref=excluded.source_ref",
                values,
            )

    def clear_legacy_cycle_stopped(self, profile_id: str, requested_at: str) -> bool:
        profile_id = _profile_id(profile_id)
        requested_at = _required(requested_at, "requested_at")
        with self.transaction():
            cursor = self.connection.execute(
                "UPDATE profile_control_intents SET state='CLEARED', requested_at=? "
                "WHERE profile_id=? AND intent_id='legacy-cycle-stopped' "
                "AND intent_type='cycle_stopped' AND source_ref='legacy:cycle_stopped.flag'",
                (requested_at, profile_id),
            )
        return cursor.rowcount == 1

    def list_audit_events(self, profile_id: str) -> list[dict[str, object]]:
        profile_id = _profile_id(profile_id)
        return [dict(row) for row in self.connection.execute(
            "SELECT event_id, occurred_at, event_type, actor_ref, summary, source_ref "
            "FROM profile_audit_events WHERE profile_id=? ORDER BY event_id", (profile_id,)
        )]

    def export_profile(self, profile_id: str) -> dict[str, object]:
        profile_id = _profile_id(profile_id)
        profile = self.connection.execute(
            "SELECT profile_id, display_name, account_ref, verified_identity_ref, status "
            "FROM remote_profiles WHERE profile_id=?",
            (profile_id,),
        ).fetchone()
        if profile is None:
            raise PortableStoreError("unknown profile_id")
        result = {
            "manifest": {"store_kind": STORE_KIND, "schema_version": SCHEMA_VERSION,
                         "profile_id": profile_id},
            "profile": dict(profile),
            "clients": self.list_clients(profile_id),
            "schedules": self.list_schedules(profile_id),
            "policies": self.list_policies(profile_id),
            "control_intents": self.list_control_intents(profile_id),
            "observations": self.list_observations(profile_id),
            "audit_events": self.list_audit_events(profile_id),
        }
        return result

    def backup_to(self, destination: Path | str) -> tuple[Path, dict[str, object]]:
        self.connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.connection.commit()
        shutil.copy2(self.path, target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest = {"store_kind": STORE_KIND, "schema_version": SCHEMA_VERSION,
                    "filename": target.name, "sha256": digest, "size": target.stat().st_size}
        target.with_suffix(target.suffix + ".manifest.json").write_text(
            _json(manifest) + "\n", encoding="utf-8"
        )
        return target, manifest
