"""Services for master reads and guarded display-name CAS writes."""
import json
import threading

from repositories.aitool import UpstreamError, ai_tool


_ALLOWED_JSON_CONTENT_TYPES = {"application/json", "application/json; charset=utf-8"}
_CHANGE_FIELDS = frozenset({"client", "expected_name", "name"})
_MASTER_WRITE_LOCK = threading.Lock()


class MasterValidationError(Exception):
    """The master source, request, or response violates the safe contract."""


def _invalid_request():
    raise UpstreamError(400, b'{"error":"invalid master rename request"}')


def _conflict():
    raise UpstreamError(409, b'{"error":"master rename conflict"}')


def _safe_error(status=502):
    raise UpstreamError(status, b'{"error":"master upstream response unavailable"}')


def _safe_upstream_error(_status):
    # Master writes never pass through an upstream body or status.
    _safe_error()


def _decode_object(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise MasterValidationError()
    if not isinstance(value, dict):
        raise MasterValidationError()
    return value


def _valid_text(value):
    if type(value) is not str or not 1 <= len(value) <= 200 or not value.strip():
        return False
    return not any(
        ord(char) < 32 or 0x7F <= ord(char) <= 0x9F
        for char in value
    )


def _load_fresh_master():
    try:
        body, status, content_type = ai_tool.get("api/master")
    except UpstreamError as error:
        _safe_upstream_error(error.status)
    if status != 200 or "application/json" not in (content_type or ""):
        _safe_error()
    try:
        source = _decode_object(body)
        clients = source["clients"]
        schedule = source["schedule"]
        if type(clients) is not list or type(schedule) is not list:
            raise MasterValidationError()
        seen = set()
        clean_clients = []
        for client in clients:
            if not isinstance(client, dict):
                raise MasterValidationError()
            if set(client) - {"client", "name", "group", "selected", "slot", "status", "remote_name"}:
                raise MasterValidationError()
            client_id = client.get("client")
            name = client.get("name")
            group = client.get("group")
            selected = client.get("selected", True)
            if not _valid_text(client_id) or not _valid_text(name):
                raise MasterValidationError()
            if client_id in seen or type(group) is not str or type(selected) is not bool:
                raise MasterValidationError()
            seen.add(client_id)
            clean_clients.append({
                "client": client_id,
                "name": name,
                "group": group,
                "selected": selected,
            })
        clean_schedule = []
        for item in schedule:
            if not isinstance(item, dict) or set(item) - {"group", "time", "close"}:
                raise MasterValidationError()
            group = item.get("group")
            open_time = item.get("time")
            close_time = item.get("close")
            if type(group) is not str or not group or type(open_time) is not str or not open_time:
                raise MasterValidationError()
            if type(close_time) is not str or not close_time:
                raise MasterValidationError()
            clean_schedule.append({"group": group, "time": open_time, "close": close_time})
    except (KeyError, MasterValidationError):
        _safe_error()
    return clean_clients, clean_schedule


def _prepare_changes(body, content_type):
    if (content_type or "").strip().lower() not in _ALLOWED_JSON_CONTENT_TYPES:
        _invalid_request()
    if not body:
        _invalid_request()
    try:
        request_body = _decode_object(body)
        if set(request_body) != {"changes"} or type(request_body["changes"]) is not list:
            raise MasterValidationError()
        if not request_body["changes"]:
            raise MasterValidationError()
        changes = []
        seen = set()
        for change in request_body["changes"]:
            if not isinstance(change, dict) or set(change) != _CHANGE_FIELDS:
                raise MasterValidationError()
            client_id = change["client"]
            expected_name = change["expected_name"]
            name = change["name"]
            if not _valid_text(client_id) or not _valid_text(expected_name) or not _valid_text(name):
                raise MasterValidationError()
            if client_id in seen:
                raise MasterValidationError()
            seen.add(client_id)
            changes.append((client_id, expected_name, name))
    except MasterValidationError:
        _invalid_request()
    return changes


def _canonical_master(clients, schedule, names):
    return {
        "clients": [
            {
                "client": client["client"],
                "name": names.get(client["client"], client["name"]),
                "group": client["group"],
                "selected": client["selected"],
            }
            for client in clients
        ],
        "schedule": schedule,
    }


def _validate_success(body, status, content_type):
    if status != 200 or "application/json" not in (content_type or ""):
        _safe_error()
    try:
        response = _decode_object(body)
        if response.get("status") != "OK":
            raise MasterValidationError()
        clients = response["clients"]
        schedule = response["schedule"]
        if type(clients) is not int or clients < 0:
            raise MasterValidationError()
        if type(schedule) is not int or schedule < 0:
            raise MasterValidationError()
    except (KeyError, MasterValidationError):
        _safe_error()
    safe_response = {"status": "OK", "clients": clients, "schedule": schedule}
    return (
        json.dumps(safe_response, separators=(",", ":")).encode("utf-8"),
        200,
        "application/json",
    )


def get_master():
    """GET clients_master.json - handler legacy, khong doi upstream path."""
    return ai_tool.get("clients_master.json")


def get_api_master():
    """GET api/master - handler rieng cho API master cua ai tool."""
    return ai_tool.get("api/master")


def get_database():
    """GET client_database.json."""
    return ai_tool.get("client_database.json")


def update_master_names(body, content_type="application/json", before_upstream_write=None):
    """Apply name-only per-field CAS changes against a fresh master snapshot."""
    changes = _prepare_changes(body, content_type)
    with _MASTER_WRITE_LOCK:
        clients, schedule = _load_fresh_master()
        by_client = {client["client"]: client for client in clients}
        names = {}
        for client_id, expected_name, name in changes:
            current = by_client.get(client_id)
            if current is None:
                _invalid_request()
            if current["name"] != expected_name:
                _conflict()
            names[client_id] = name
        if not any(names[client_id] != by_client[client_id]["name"] for client_id in names):
            return _validate_success(
                json.dumps({"status": "OK", "clients": len(clients), "schedule": len(schedule)}).encode(),
                200,
                "application/json",
            )
        canonical = json.dumps(
            _canonical_master(clients, schedule, names),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            if before_upstream_write is None:
                result = ai_tool.post("api/master", canonical, "application/json")
            else:
                with before_upstream_write():
                    result = ai_tool.post("api/master", canonical, "application/json")
        except UpstreamError as error:
            _safe_upstream_error(error.status)
        return _validate_success(*result)
