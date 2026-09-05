"""Services for cycle backup metadata, creation and guarded deletion."""
import json
import re
import threading
import time
from urllib.parse import quote

from repositories.aitool import UpstreamError, ai_tool


_BACKUP_MIN_SPACING_SECONDS = 1.1
_BACKUP_LOCK = threading.Lock()
_last_upstream_backup_at = 0.0
_BACKUP_NAME_RE = re.compile(r"[A-Za-z0-9_.-]+")
_BACKUP_PREFIX = "/up/api/cycle/backup/"
_GENERIC_DELETE_ERROR = b'{"error":"backup deletion unavailable"}'


class BackupNameError(Exception):
    """The raw route path is not one canonical backup name segment."""


def _decode_raw_segment(raw):
    data = bytearray()
    index = 0
    while index < len(raw):
        value = raw[index]
        if value == "%":
            if index + 2 >= len(raw):
                raise BackupNameError()
            try:
                data.append(int(raw[index + 1:index + 3], 16))
            except ValueError:
                raise BackupNameError()
            index += 3
        else:
            data.extend(value.encode("ascii", "strict"))
            index += 1
    try:
        return bytes(data).decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        raise BackupNameError()


def canonical_backup_name(raw_path, route_name):
    """Decode one raw path segment and return the canonical golden-compatible name."""
    if not isinstance(raw_path, str) or not isinstance(route_name, str):
        raise BackupNameError()
    raw_path = raw_path.split("?", 1)[0]
    if not raw_path.startswith(_BACKUP_PREFIX):
        raise BackupNameError()
    raw_segment = raw_path[len(_BACKUP_PREFIX):]
    if not raw_segment or "/" in raw_segment or "\\" in raw_segment:
        raise BackupNameError()
    name = _decode_raw_segment(raw_segment)
    if name != route_name or name in (".", "..") or "%" in name:
        raise BackupNameError()
    if not _BACKUP_NAME_RE.fullmatch(name):
        raise BackupNameError()
    canonical = quote(name, safe="ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-")
    if canonical != name:
        raise BackupNameError()
    return name


def get_cycle_backups():
    """GET api/cycle/backup — chi doc danh sach metadata backup."""
    return ai_tool.get("api/cycle/backup")


def _invalid_success():
    """Never expose malformed success bodies or retry a backup creation."""
    raise UpstreamError(502, b'{"error":"invalid backup response"}')


def _validate_success(body, status, content_type):
    if status != 200 or "application/json" not in (content_type or ""):
        _invalid_success()
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _invalid_success()
    if not isinstance(response, dict):
        _invalid_success()
    if response.get("status") != "OK" or not isinstance(response.get("backup"), str):
        _invalid_success()
    manifest = response.get("manifest")
    if not isinstance(manifest, dict):
        _invalid_success()
    if not isinstance(manifest.get("created_at"), str):
        _invalid_success()
    if not isinstance(manifest.get("label"), str):
        _invalid_success()
    if not isinstance(manifest.get("files"), list):
        _invalid_success()
    if not isinstance(manifest.get("script_files"), list):
        _invalid_success()
    return body, status, content_type


def create_cycle_backup(body, content_type="application/json"):
    """POST api/cycle/backup with process-local collision protection."""
    global _last_upstream_backup_at
    with _BACKUP_LOCK:
        elapsed = time.monotonic() - _last_upstream_backup_at
        delay = _BACKUP_MIN_SPACING_SECONDS - elapsed
        if delay > 0:
            time.sleep(delay)
        try:
            result = ai_tool.post("api/cycle/backup", body, content_type)
            return _validate_success(*result)
        finally:
            _last_upstream_backup_at = time.monotonic()


def _delete_error():
    raise UpstreamError(502, _GENERIC_DELETE_ERROR)


def _fresh_backup_names():
    try:
        body, status, content_type = ai_tool.get("api/cycle/backup")
    except UpstreamError:
        _delete_error()
    if status != 200 or "application/json" not in (content_type or ""):
        _delete_error()
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _delete_error()
    if not isinstance(response, dict) or set(response) != {"backups"} or type(response["backups"]) is not list:
        _delete_error()
    names = []
    for backup in response["backups"]:
        if not isinstance(backup, dict) or not isinstance(backup.get("name"), str):
            _delete_error()
        names.append(backup["name"])
    return names


def _validate_delete_success(body, status, content_type, name):
    if status != 200 or "application/json" not in (content_type or ""):
        _delete_error()
    try:
        response = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _delete_error()
    if (not isinstance(response, dict) or set(response) != {"status", "deleted"}
            or response.get("status") != "OK" or response.get("deleted") != name):
        _delete_error()
    return json.dumps({"status": "OK", "deleted": name}, separators=(",", ":")).encode(), 200, "application/json"


def delete_cycle_backup(name):
    """Fresh-validate one backup, then delete it under the complete transaction lock."""
    with _BACKUP_LOCK:
        names = _fresh_backup_names()
        if names.count(name) != 1:
            raise UpstreamError(409, b'{"error":"backup target unavailable"}')
        try:
            result = ai_tool.delete_backup(name)
        except UpstreamError:
            _delete_error()
        except Exception:
            _delete_error()
        return _validate_delete_success(*result, name)
