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


def _unavailable() -> None:
    raise UpstreamError(503, b'{"error":"portable profile store unavailable"}')


def _conflict() -> None:
    raise UpstreamError(409, b'{"error":"host binding conflict"}')


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
    if not _valid_text(host_id):
        raise UpstreamError(503, b'{"error":"configured host identity unavailable"}')
    return host_id.strip()


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
    if not body:
        return host_id
    value = _decode_object(body)
    if set(value) - {"display_name"}:
        _invalid_request()
    display_name = value.get("display_name", host_id)
    if not _valid_text(display_name):
        _invalid_request()
    return display_name.strip()


def _binding_body(body: bytes, content_type: str | None) -> str:
    _check_content_type(content_type)
    if not body:
        _invalid_request()
    value = _decode_object(body)
    if set(value) != {"profile_id"} or not _valid_text(value["profile_id"]):
        _invalid_request()
    return value["profile_id"].strip()


def _response(operation: str, result: dict[str, object]) -> tuple[bytes, int, str]:
    payload = {
        "ok": True,
        "operation": operation,
        "result": result,
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
        result = store.register_host(
            configured_id,
            display_name,
            _HOST_ORIGIN_REF,
            now or _now(),
        )
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
    """Create or confirm an OFFLINE binding without touching ACTIVE state."""
    configured_id = _configured_host_id(host_id)
    profile_id = _binding_body(body, content_type)
    store = _open_store(store_path)
    try:
        result = store.ensure_offline_binding(configured_id, profile_id, now or _now())
    except BindingError:
        _conflict()
    except (OSError, sqlite3.Error, PortableStoreError):
        _unavailable()
    finally:
        store.close()
    return _response("offline_binding", result)
