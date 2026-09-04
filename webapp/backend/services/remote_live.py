"""Services for canonical and guarded remote live snapshot reads."""
import datetime
import json
import threading
from urllib.parse import quote

from repositories.aitool import UpstreamError, ai_tool


_REMOTE_LIVE_LOCK = threading.Lock()
_REMOTE_SELECTOR_LOCK = threading.Lock()
_PUBLIC_TOP_LEVEL_FIELDS = frozenset({
    "ok", "fetchedAt", "clientCount", "selectedClientIdx", "clients", "tasks", "logs"
})
_UPSTREAM_TOP_LEVEL_FIELDS = _PUBLIC_TOP_LEVEL_FIELDS | {"room", "roster"}
_CLIENT_FIELDS = frozenset({
    "idx", "id", "name", "state", "cap", "capAge", "uiStatus", "level", "resources", "checked"
})
_RESOURCE_FIELDS = frozenset({"nPhieu", "bac", "vang", "ngoc"})
_TASK_FIELDS = frozenset({"idx", "name", "status", "checked"})
_LOG_FIELDS = frozenset({"text", "color"})


class RemoteLiveValidationError(Exception):
    """The remote snapshot violates the public read contract."""


class RemoteSelectorQueryError(Exception):
    """The raw selector query violates the route-level query contract."""


def _safe_error():
    raise UpstreamError(502, b'{"error":"remote live snapshot unavailable"}')


def _decode_object(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RemoteLiveValidationError()
    if not isinstance(value, dict):
        raise RemoteLiveValidationError()
    return value


def _exact_nonnegative_int(value):
    return type(value) is int and value >= 0


def _valid_timestamp(value):
    if type(value) is not str or not value:
        return False
    try:
        datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _validate_client(entry):
    if not isinstance(entry, dict) or set(entry) != _CLIENT_FIELDS:
        raise RemoteLiveValidationError()
    if not _exact_nonnegative_int(entry["idx"]):
        raise RemoteLiveValidationError()
    for field in ("id", "name", "state", "cap", "uiStatus", "level"):
        if type(entry[field]) is not str:
            raise RemoteLiveValidationError()
    cap_age = entry["capAge"]
    if cap_age is not None and not _exact_nonnegative_int(cap_age):
        raise RemoteLiveValidationError()
    resources = entry["resources"]
    if not isinstance(resources, dict) or set(resources) != _RESOURCE_FIELDS:
        raise RemoteLiveValidationError()
    if any(type(resources[field]) is not str for field in _RESOURCE_FIELDS):
        raise RemoteLiveValidationError()
    if type(entry["checked"]) is not bool:
        raise RemoteLiveValidationError()


def _validate_task(entry):
    if not isinstance(entry, dict) or set(entry) != _TASK_FIELDS:
        raise RemoteLiveValidationError()
    if not _exact_nonnegative_int(entry["idx"]):
        raise RemoteLiveValidationError()
    if type(entry["name"]) is not str or type(entry["status"]) is not str:
        raise RemoteLiveValidationError()
    if type(entry["checked"]) is not bool:
        raise RemoteLiveValidationError()


def _validate_log(entry):
    if not isinstance(entry, dict) or set(entry) != _LOG_FIELDS:
        raise RemoteLiveValidationError()
    if type(entry["text"]) is not str or type(entry["color"]) is not str:
        raise RemoteLiveValidationError()


def _project_snapshot(body, status, content_type):
    if status != 200 or "application/json" not in (content_type or ""):
        _safe_error()
    try:
        source = _decode_object(body)
        if set(source) != _UPSTREAM_TOP_LEVEL_FIELDS:
            raise RemoteLiveValidationError()
        if source["ok"] is not True:
            raise RemoteLiveValidationError()
        if type(source["room"]) is not str or not source["room"]:
            raise RemoteLiveValidationError()
        if source["roster"] is not None and not isinstance(source["roster"], dict):
            raise RemoteLiveValidationError()
        if not _valid_timestamp(source["fetchedAt"]):
            raise RemoteLiveValidationError()
        clients = source["clients"]
        tasks = source["tasks"]
        logs = source["logs"]
        if type(clients) is not list or type(tasks) is not list or type(logs) is not list:
            raise RemoteLiveValidationError()
        client_indexes = set()
        client_ids = set()
        for entry in clients:
            _validate_client(entry)
            if entry["idx"] in client_indexes:
                raise RemoteLiveValidationError()
            client_indexes.add(entry["idx"])
            client_ids.add(entry["id"])
        task_indexes = set()
        for entry in tasks:
            _validate_task(entry)
            if entry["idx"] in task_indexes:
                raise RemoteLiveValidationError()
            task_indexes.add(entry["idx"])
        for entry in logs:
            _validate_log(entry)
        if len(client_ids) != len(clients):
            raise RemoteLiveValidationError()
        client_count = source["clientCount"]
        if not _exact_nonnegative_int(client_count) or client_count != len(clients):
            raise RemoteLiveValidationError()
        selected = source["selectedClientIdx"]
        if selected is not None and (not _exact_nonnegative_int(selected) or selected not in client_indexes):
            raise RemoteLiveValidationError()
        return {
            "ok": True,
            "fetchedAt": source["fetchedAt"],
            "clientCount": client_count,
            "selectedClientIdx": selected,
            "clients": clients,
            "tasks": tasks,
            "logs": logs,
        }
    except RemoteLiveValidationError:
        _safe_error()


def _encode_public(snapshot):
    return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 200, "application/json"


def _load_snapshot():
    try:
        return _project_snapshot(*ai_tool.get("api/remote_live"))
    except UpstreamError:
        _safe_error()


def get_remote_live():
    """Read and project remote live without passing a client selector."""
    with _REMOTE_LIVE_LOCK:
        return _encode_public(_load_snapshot())


def _decode_query_component(raw):
    result = bytearray()
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == 0x2B:
            raise RemoteSelectorQueryError()
        if value == 0x25:
            if index + 2 >= len(raw):
                raise RemoteSelectorQueryError()
            try:
                result.append(int(raw[index + 1:index + 3], 16))
            except ValueError:
                raise RemoteSelectorQueryError()
            index += 3
            continue
        result.append(value)
        index += 1
    try:
        return bytes(result).decode("utf-8")
    except UnicodeDecodeError:
        raise RemoteSelectorQueryError()


def _valid_selector_text(value, max_length):
    if type(value) is not str or not 1 <= len(value) <= max_length or not value.strip():
        return False
    return not any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in value)


def parse_selector_query(raw_query):
    """Parse raw query bytes and return (t, client, canonical_query)."""
    if not isinstance(raw_query, bytes) or not raw_query:
        raise RemoteSelectorQueryError()
    values = {}
    for component in raw_query.split(b"&"):
        if component.count(b"=") != 1:
            raise RemoteSelectorQueryError()
        raw_key, raw_value = component.split(b"=", 1)
        key = _decode_query_component(raw_key)
        value = _decode_query_component(raw_value)
        if not key or not value or key in values:
            raise RemoteSelectorQueryError()
        values[key] = value
    if set(values) != {"t", "client"}:
        raise RemoteSelectorQueryError()
    timestamp = values["t"]
    client = values["client"]
    if (not 1 <= len(timestamp) <= 32 or not all("0" <= char <= "9" for char in timestamp)
            or not _valid_selector_text(client, 200)):
        raise RemoteSelectorQueryError()
    canonical = "t=" + quote(timestamp, safe="") + "&client=" + quote(client, safe="")
    return timestamp, client, canonical


def _normalize_client_id(value):
    return value[2:] if value.startswith("0:") else value


def _selected_client_id(snapshot):
    selected = snapshot["selectedClientIdx"]
    if selected is None:
        return None
    for client in snapshot["clients"]:
        if client["idx"] == selected:
            return client["id"]
    return None


def _find_target(snapshot, requested_client):
    normalized = _normalize_client_id(requested_client)
    matches = [client for client in snapshot["clients"]
               if _normalize_client_id(client["id"]) == normalized]
    if len(matches) != 1:
        raise UpstreamError(409, b'{"error":"remote selector target unavailable"}')
    return matches[0]["id"]


def get_remote_live_selector(selector):
    """Select a fresh remote client under one read/selector transaction lock."""
    timestamp, requested_client, _canonical = selector
    with _REMOTE_SELECTOR_LOCK:
        fresh = _load_snapshot()
        target_client = _find_target(fresh, requested_client)
        if _normalize_client_id(_selected_client_id(fresh) or "") == _normalize_client_id(target_client):
            return _encode_public(fresh)
        canonical_query = "t=" + quote(timestamp, safe="") + "&client=" + quote(target_client, safe="")
        try:
            selected = _project_snapshot(*ai_tool.get("api/remote_live?" + canonical_query))
        except UpstreamError:
            _safe_error()
        if _normalize_client_id(_selected_client_id(selected) or "") != _normalize_client_id(target_client):
            _safe_error()
        return _encode_public(selected)
