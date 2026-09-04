"""Services for cycle backup metadata and backup creation."""
import threading
import time

from repositories.aitool import ai_tool


_BACKUP_MIN_SPACING_SECONDS = 1.1
_BACKUP_LOCK = threading.Lock()
_last_upstream_backup_at = 0.0


def get_cycle_backups():
    """GET api/cycle/backup — chi doc danh sach metadata backup."""
    return ai_tool.get("api/cycle/backup")


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
            return ai_tool.post("api/cycle/backup", body, content_type)
        finally:
            _last_upstream_backup_at = time.monotonic()
