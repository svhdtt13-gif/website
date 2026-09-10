"""Configuration-only host registration and inactive binding writes."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from repositories.aitool import UpstreamError
from repositories.portable_store import BindingError, PortableDomainStore, PortableStoreError


_ALLOWED_JSON_CONTENT_TYPES = {"application/json", "application/json; charset=utf-8"}
_HOST_ORIGIN_REF = "host_agent:configured"
_RUNTIME_CONTROLS = {
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
}


def _invalid_request(message: str = "invalid host registration request") -> None:
    raise UpstreamError(400, json.dumps({"error": message}, separators=(",", ":")).encode())


def _conflict(message: str = "host binding conflict") -> None:
    raise UpstreamError(409, json.dumps({"error": message}, separators=(",", ":")).encode())


def _unavailable() -> None:
    raise UpstreamError(503, b'{"error":"portable profile store unavailable"}')


def _decode_object(body: bytes) -> dict[str, object]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _invalid_request()
    if not isinstance(value, dict):
        _invalid_request()
    return value


def _valid_text(value: object, maximum: int = 200) -> bool:
    if type(value) is not str or not 1 <= len(value) <= maximum or not value.strip():
        return False
    return not any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value)


def _configured_host_id(host_id: str) -> str:
    if not _valid_text(host_id) or host_id != host_id.strip():
        raise UpstreamError(503, b'{"error":"configured host identity unavailable"}')
    return host_id


def _request_host_id(value: object, configured_host_id: str) -> None:
    if not _valid_text(value) or value != value.strip():
        _invalid_request("host_id is required and must be exact text")
    if value != configured_host_id:
        _conflict("host_id does not match configured host identity")


def _exact_text(value: object, field: str) -> str:
    if not _valid_text(value) or value != value.strip():
        _invalid_request(f"{field} is required and must be exact text")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _open_store(path: Path | str) -> PortableDomainStore:
    try:
        return PortableDomainStore.open(path)
    except (OSError, sqlite3.Error, PortableStoreError):
        _unavailable()


def _check_content_type(content_type: str | None) -> None:
    if (content_type or "").strip().lower() not in _ALLOWED_JSON_CONTENT_TYPES:
        _invalid_request()


def _registration_body(body: bytes, content_type: str | None, host_id: str) -> str:
    _check_content_type(content_type)
    value = _decode_object(body)
    if set(value) != {"host_id", "display_name"}:
        _invalid_request("request must contain exactly host_id and display_name")
    _request_host_id(value["host_id"], host_id)
    return _exact_text(value["display_name"], "display_name")


def _binding_body(body: bytes, content_type: str | None, host_id: str) -> str:
    _check_content_type(content_type)
    value = _decode_object(body)
    if set(value) != {"host_id", "profile_id"}:
        _invalid_request("request must contain exactly host_id and profile_id")
    _request_host_id(value["host_id"], host_id)
    return _exact_text(value["profile_id"], "profile_id")


def _response(operation: str, result: dict[str, object]) -> tuple[bytes, int, str]:
    payload = {
        "ok": True,
        "operation": operation,
        "result": result,
        "runtime_effect": "NONE",
        "runtime_authority": {
            "mode": "LEGACY",
            "active_runtime_owner": None,
            "reason": "Configuration writes do not establish runtime ownership.",
        },
        "controls": dict(_RUNTIME_CONTROLS),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(), 200, "application/json"


def register_configured_host(
    body: bytes,
    content_type: str | None,
    store_path: Path | str,
    host_id: str,
    now: str | None = None,
) -> tuple[bytes, int, str]:
    """Register or confirm only the explicitly configured local host identity."""
    configured_id = _configured_host_id(host_id)
    display_name = _registration_body(body, content_type, configured_id)
    store = _open_store(store_path)
    try:
        with store.transaction(immediate=True):
            existing = store.connection.execute(
                "SELECT display_name FROM hosts WHERE host_id=?", (configured_id,)
            ).fetchone()
            if existing is not None and existing["display_name"] != display_name:
                _conflict("host display_name does not match existing host")
            result = store.register_host(
                configured_id,
                display_name,
                _HOST_ORIGIN_REF,
                now or _now(),
            )
    except BindingError:
        _conflict("host registration conflict")
    except (OSError, sqlite3.Error, PortableStoreError):
        _unavailable()
    finally:
        store.close()
    return _response("host_registration", result)


def create_offline_binding(
    body: bytes,
    content_type: str | None,
    store_path: Path | str,
    host_id: str,
    now: str | None = None,
) -> tuple[bytes, int, str]:
    """Create or confirm an OFFLINE binding and its profile-scoped audit event."""
    configured_id = _configured_host_id(host_id)
    profile_id = _binding_body(body, content_type, configured_id)
    store = _open_store(store_path)
    timestamp = now or _now()
    try:
        with store.transaction(immediate=True):
            result = store.ensure_offline_binding(configured_id, profile_id, timestamp)
            if result["created"]:
                binding_id = str(result["binding_id"])
                store.add_audit_event(
                    profile_id,
                    f"binding:{binding_id}:created",
                    timestamp,
                    "host_binding_created",
                    "webapp:host_registration",
                    f"Created OFFLINE binding {binding_id}.",
                    "webapp:host_binding",
                )
    except BindingError:
        _conflict()
    except (OSError, sqlite3.Error, PortableStoreError):
        _unavailable()
    finally:
        store.close()
    return _response("offline_binding", result)
