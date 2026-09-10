"""Read-only Host Agent Discovery Foundation.

Configured host identity, portable binding, and local prerequisite observation
are separate projections. None of them is allowed to establish runtime
ownership or trigger a process/scheduler/remote action.
"""
from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

from repositories.portable_store import SCHEMA_VERSION, STORE_KIND


class HostDiscoveryUnavailable(Exception):
    """The discovery source cannot satisfy the read contract."""


PREREQUISITE_ALLOWLIST = (
    ("autocycle_script", "tools/AutoCycle.ps1"),
    ("remote_sync_script", "continuous_sync_remote.ps1"),
    ("cycle_state", "tools/cache/cycle_state.json"),
    ("client_database", "tools/client_database.json"),
    ("cycle_stopped_flag", "tools/cache/cycle_stopped.flag"),
)
_BINDING_STATES = {"ACTIVE", "OFFLINE", "RETIRED"}


def _error(message: str) -> tuple[bytes, int, str]:
    return json.dumps({"error": message}, separators=(",", ":")).encode(), 503, "application/json"


def _required_host_id(host_id: str) -> str:
    if type(host_id) is not str or not host_id.strip():
        raise HostDiscoveryUnavailable("configured host identity unavailable")
    if any(ord(char) < 32 or ord(char) == 127 for char in host_id):
        raise HostDiscoveryUnavailable("invalid configured host identity")
    return host_id.strip()


def _connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise HostDiscoveryUnavailable("portable profile store unavailable")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (OSError, sqlite3.Error) as error:
        raise HostDiscoveryUnavailable("portable profile store unavailable") from error


def _validate_store(connection: sqlite3.Connection) -> None:
    try:
        meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as error:
        raise HostDiscoveryUnavailable("portable profile store unavailable") from error
    if (
        meta.get("store_kind") != STORE_KIND
        or meta.get("schema_version") != str(SCHEMA_VERSION)
        or integrity != "ok"
    ):
        raise HostDiscoveryUnavailable("portable profile store unavailable")


def _read_bindings(store_path: Path, host_id: str) -> tuple[dict[str, object] | None, list[dict[str, object]]]:
    connection = _connection(store_path)
    try:
        # Host metadata and all bindings must come from one SQLite snapshot.
        connection.execute("BEGIN")
        _validate_store(connection)
        host = connection.execute(
            "SELECT host_id, display_name FROM hosts WHERE host_id=?",
            (host_id,),
        ).fetchone()
        rows = connection.execute(
            "SELECT b.binding_id, b.host_id, b.profile_id, b.binding_generation, b.state, "
            "b.updated_at, p.display_name AS profile_display_name, p.status AS profile_status "
            "FROM host_profile_bindings AS b "
            "JOIN remote_profiles AS p ON p.profile_id = b.profile_id "
            "WHERE b.host_id=? ORDER BY b.binding_generation DESC, b.binding_id",
            (host_id,),
        ).fetchall()
    except sqlite3.Error as error:
        raise HostDiscoveryUnavailable("portable binding unavailable") from error
    finally:
        connection.close()

    host_projection = None
    if host is not None:
        host_projection = {
            "host_id": host["host_id"],
            "display_name": host["display_name"],
        }
    bindings: list[dict[str, object]] = []
    active_count = 0
    for row in rows:
        if row["state"] not in _BINDING_STATES:
            raise HostDiscoveryUnavailable("portable binding unavailable")
        if row["state"] == "ACTIVE":
            active_count += 1
        bindings.append({
            "binding_id": row["binding_id"],
            "host_id": row["host_id"],
            "profile_id": row["profile_id"],
            "profile_display_name": row["profile_display_name"],
            "profile_status": row["profile_status"],
            "binding_generation": row["binding_generation"],
            "state": row["state"],
            "updated_at": row["updated_at"],
        })
    if active_count > 1:
        raise HostDiscoveryUnavailable("portable binding unavailable")
    return host_projection, bindings


def _probe_prerequisites(root: Path) -> list[dict[str, object]]:
    try:
        resolved_root = root.resolve()
    except OSError as error:
        raise HostDiscoveryUnavailable("host prerequisite root unavailable") from error
    result = []
    for name, relative in PREREQUISITE_ALLOWLIST:
        try:
            path = (resolved_root / relative).resolve()
        except OSError as error:
            raise HostDiscoveryUnavailable("host prerequisite probe unavailable") from error
        try:
            path.relative_to(resolved_root)
        except ValueError as error:
            raise HostDiscoveryUnavailable("host prerequisite allowlist escaped root") from error
        try:
            metadata = path.stat()
        except FileNotFoundError:
            result.append({
                "name": name,
                "path": relative,
                "state": "ABSENT",
                "read_only_probe": True,
            })
        except OSError:
            result.append({
                "name": name,
                "path": relative,
                "state": "UNKNOWN",
                "error": "probe_error",
                "read_only_probe": True,
            })
        else:
            is_regular_file = stat.S_ISREG(metadata.st_mode)
            result.append({
                "name": name,
                "path": relative,
                "state": "PRESENT" if is_regular_file else "UNKNOWN",
                "error": None if is_regular_file else "not_regular_file",
                "read_only_probe": True,
            })
    return result


def _read_model(store_path: Path, host_id: str, prerequisite_root: Path) -> dict[str, object]:
    explicit_host_id = _required_host_id(host_id)
    host, bindings = _read_bindings(store_path, explicit_host_id)
    current_binding = next((binding for binding in bindings if binding["state"] == "ACTIVE"), None)
    if current_binding is None and bindings:
        current_binding = bindings[0]
    return {
        "ok": True,
        "read_only": True,
        "configured_host_identity": {
            "host_id": explicit_host_id,
            "configured": True,
            "source": "explicit_config",
        },
        "portable_binding": {
            "status": "BOUND" if current_binding else "UNBOUND",
            "host_lookup": "FOUND" if host else "UNKNOWN",
            "host": host,
            "current_binding": current_binding,
            "bindings": bindings,
            "source": "portable_domain_store",
        },
        "local_runtime_observation": {
            "mode": "OBSERVATION_ONLY",
            "process_observation": "NOT_PERFORMED",
            "runtime_owner": None,
            "prerequisites": _probe_prerequisites(prerequisite_root),
        },
        "runtime_authority": {
            "mode": "LEGACY",
            "active_runtime_owner": None,
            "reason": "Configured identity, portable binding, and local observation do not establish runtime ownership.",
        },
        "controls": {
            "can_install": False,
            "can_register": False,
            "can_start": False,
            "can_stop": False,
            "can_reconnect": False,
            "can_login": False,
            "can_rebind": False,
            "can_activate": False,
            "can_switch": False,
            "can_control": False,
            "can_bind": False,
            "reason": "Host Agent Discovery Foundation is read-only; runtime control is deferred.",
        },
    }


def get_host_discovery(
    store_path: Path | str, host_id: str, prerequisite_root: Path | str
) -> tuple[bytes, int, str]:
    """Return discovery state without creating hosts/bindings or probing owners."""
    try:
        payload = _read_model(Path(store_path), host_id, Path(prerequisite_root))
    except HostDiscoveryUnavailable as error:
        return _error(str(error))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), 200, "application/json"
