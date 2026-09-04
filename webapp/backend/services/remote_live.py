"""Service for the canonical, non-selecting remote live snapshot read."""
import datetime
import json
import threading

from repositories.aitool import UpstreamError, ai_tool


_REMOTE_LIVE_LOCK = threading.Lock()
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
        if not _PUBLIC_TOP_LEVEL_FIELDS.issubset(source) or set(source) - _UPSTREAM_TOP_LEVEL_FIELDS:
            raise RemoteLiveValidationError()
        if source["ok"] is not True:
            raise RemoteLiveValidationError()
        if not _valid_timestamp(source["fetchedAt"]):
            raise RemoteLiveValidationError()
        clients = source["clients"]
        tasks = source["tasks"]
        logs = source["logs"]
        if type(clients) is not list or type(tasks) is not list or type(logs) is not list:
            raise RemoteLiveValidationError()
        client_indexes = set()
        for entry in clients:
            _validate_client(entry)
            if entry["idx"] in client_indexes:
                raise RemoteLiveValidationError()
            client_indexes.add(entry["idx"])
        task_indexes = set()
        for entry in tasks:
            _validate_task(entry)
            if entry["idx"] in task_indexes:
                raise RemoteLiveValidationError()
            task_indexes.add(entry["idx"])
        for entry in logs:
            _validate_log(entry)
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


def get_remote_live():
    """Read and project remote live without passing a client selector."""
    with _REMOTE_LIVE_LOCK:
        try:
            result = ai_tool.get("api/remote_live")
        except UpstreamError:
            _safe_error()
        public = _project_snapshot(*result)
        return json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 200, "application/json"
