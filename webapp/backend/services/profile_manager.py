"""Read-only Remote Profile Manager foundation.

This service reads the Portable Domain Store without running migrations or
opening a write-capable SQLite connection. Runtime ownership remains legacy
authority and is never inferred from a portable profile status or binding.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from repositories.portable_store import SCHEMA_VERSION, STORE_KIND


class ProfileManagerUnavailable(Exception):
    """The portable store cannot satisfy the read contract."""


_PROFILE_STATUSES = {"VERIFIED", "ACTIVE", "OFFLINE", "NEEDS_LOGIN"}
_BINDING_STATES = {"ACTIVE", "OFFLINE", "RETIRED"}


def _error(message: str) -> tuple[bytes, int, str]:
    return json.dumps({"error": message}, separators=(",", ":")).encode(), 503, "application/json"


def _read_only_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ProfileManagerUnavailable("portable profile store unavailable")
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection
    except (OSError, sqlite3.Error) as error:
        raise ProfileManagerUnavailable("portable profile store unavailable") from error


def _validate_meta(connection: sqlite3.Connection) -> None:
    try:
        meta = dict(connection.execute("SELECT key, value FROM schema_meta"))
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.Error as error:
        raise ProfileManagerUnavailable("portable profile store unavailable") from error
    if (
        meta.get("store_kind") != STORE_KIND
        or meta.get("schema_version") != str(SCHEMA_VERSION)
        or integrity != "ok"
    ):
        raise ProfileManagerUnavailable("portable profile store unavailable")


def _safe_reference(value: object) -> str:
    if type(value) is not str or not value.strip() or "://" in value or any(
        marker in value.lower()
        for marker in ("password", "token", "cookie", "session", "secret")
    ):
        raise ProfileManagerUnavailable("portable profile store unavailable")
    return value


def _binding(row: sqlite3.Row) -> dict[str, object]:
    state = row["state"]
    if state not in _BINDING_STATES:
        raise ProfileManagerUnavailable("portable profile store unavailable")
    return {
        "binding_id": row["binding_id"],
        "host_id": row["host_id"],
        "host_display_name": row["host_display_name"],
        "profile_id": row["profile_id"],
        "binding_generation": row["binding_generation"],
        "state": state,
        "updated_at": row["updated_at"],
    }


def _read_model(path: Path) -> dict[str, object]:
    connection = _read_only_connection(path)
    try:
        # Keep metadata and domain rows on one SQLite snapshot.
        connection.execute("BEGIN")
        _validate_meta(connection)
        profiles = connection.execute(
            "SELECT profile_id, display_name, account_ref, status, updated_at "
            "FROM remote_profiles ORDER BY profile_id"
        ).fetchall()
        bindings = connection.execute(
            "SELECT b.binding_id, b.host_id, b.profile_id, b.binding_generation, b.state, "
            "b.updated_at, h.display_name AS host_display_name "
            "FROM host_profile_bindings AS b JOIN hosts AS h ON h.host_id = b.host_id "
            "ORDER BY b.profile_id, b.binding_generation DESC, b.binding_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise ProfileManagerUnavailable("portable profile store unavailable") from error
    finally:
        connection.close()

    binding_by_profile: dict[str, dict[str, object]] = {}
    active_bindings: list[dict[str, object]] = []
    active_profiles: set[str] = set()
    active_hosts: set[str] = set()
    for row in bindings:
        current = _binding(row)
        if row["state"] == "ACTIVE":
            if row["profile_id"] in active_profiles or row["host_id"] in active_hosts:
                raise ProfileManagerUnavailable("portable profile store unavailable")
            active_profiles.add(row["profile_id"])
            active_hosts.add(row["host_id"])
            active_bindings.append(current)
        previous = binding_by_profile.get(row["profile_id"])
        if previous is None or (
            current["state"] == "ACTIVE" and previous["state"] != "ACTIVE"
        ) or (
            current["state"] == previous["state"]
            and int(str(current["binding_generation"])) > int(str(previous["binding_generation"]))
        ):
            binding_by_profile[row["profile_id"]] = current

    result_profiles = []
    for row in profiles:
        status = row["status"]
        if status not in _PROFILE_STATUSES:
            raise ProfileManagerUnavailable("portable profile store unavailable")
        result_profiles.append({
            "profile_id": row["profile_id"],
            "display_name": row["display_name"],
            "account_ref": _safe_reference(row["account_ref"]),
            "status": status,
            "updated_at": row["updated_at"],
            "binding": binding_by_profile.get(row["profile_id"]),
        })

    return {
        "ok": True,
        "read_only": True,
        "store": {"kind": STORE_KIND, "schema_version": SCHEMA_VERSION},
        "viewed_profile_id": None,
        "viewed_profile_source": "browser_local_only",
        "runtime_authority": {
            "mode": "LEGACY",
            "active_runtime_owner": None,
            "reason": "Runtime ownership is not inferred from portable profile status or binding.",
        },
        "controls": {
            "can_activate": False,
            "can_switch": False,
            "can_bind": False,
            "reason": "Remote Profile Manager foundation is read-only; live switching is deferred.",
        },
        "active_bindings": active_bindings,
        "profiles": result_profiles,
    }


def get_profile_manager(path: Path | str) -> tuple[bytes, int, str]:
    """Return the safe profile manager projection, never mutating the store."""
    try:
        payload = _read_model(Path(path))
    except ProfileManagerUnavailable as error:
        return _error(str(error))
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), 200, "application/json"
