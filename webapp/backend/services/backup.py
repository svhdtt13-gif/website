"""Services for cycle backup metadata and backup creation."""
import json
import threading
import time

from repositories.aitool import UpstreamError, ai_tool


_BACKUP_MIN_SPACING_SECONDS = 1.1
_BACKUP_LOCK = threading.Lock()
_last_upstream_backup_at = 0.0


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
    """POST api/cycle/backup with process-local collision protection.

    The golden helper names artifacts to the second. Serialize calls and wait
    between upstream calls so concurrent requests cannot reuse that filename.
    """
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
