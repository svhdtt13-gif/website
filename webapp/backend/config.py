import os
from pathlib import Path

AI_TOOL_API_BASE = os.environ.get("AI_TOOL_API_BASE", "http://127.0.0.1:8080").rstrip("/")
AI_TOOL_USER = os.environ.get("AI_TOOL_USER", "admin")
AI_TOOL_PASS = os.environ.get("AI_TOOL_PASS", "")
PORT = int(os.environ.get("WEBAPP_PORT", "8090"))
# Required for every write request; never hard-code or commit this value.
WEBAPP_WRITE_TOKEN = os.environ.get("WEBAPP_WRITE_TOKEN", "")

# SQLite remains disabled unless the operator explicitly enables both the global
# gate and the relevant group gate. Runtime eligibility is separate state.
SQLITE_READ_ENABLED = os.environ.get("SQLITE_READ_ENABLED", "false").lower() == "true"
SQLITE_MASTER_DATABASE_READ_ENABLED = os.environ.get(
    "SQLITE_MASTER_DATABASE_READ_ENABLED", "false"
).lower() == "true"
SQLITE_PUBLIC_SETTINGS_READ_ENABLED = os.environ.get(
    "SQLITE_PUBLIC_SETTINGS_READ_ENABLED", "false"
).lower() == "true"
SQLITE_RUNTIME_DIR = Path(os.environ.get(
    "SQLITE_RUNTIME_DIR",
    str(Path(__file__).resolve().parent / ".runtime" / "sqlite"),
))
SQLITE_FRESHNESS_SECONDS = int(os.environ.get("SQLITE_FRESHNESS_SECONDS", "60"))
SQLITE_REFRESH_TIMEOUT_SECONDS = int(os.environ.get("SQLITE_REFRESH_TIMEOUT_SECONDS", "15"))
SQLITE_MUTEX_NAME = r"Local\WebsiteSQLiteGenerationMutex"

# Chi cac endpoint GET nay duoc proxy (read-only). Moi thu khac -> 403.
READ_ONLY_ALLOWLIST = {
    "api/cycle/status",
    "api/cycle_status",
    "api/sync_status",
    "api/status",
    "api/ai_fix/status",
    "api/cycle/backup",
    "api/master",
    "api/remote_live",
    "api/settings",
    "clients_master.json",
    "client_database.json",
}

# Slice 2: POST api/log; Slice 6: POST api/cycle/backup; Slice 7: POST api/settings;
# Slice 8: guarded CAS rename POST api/master.
# Sync, AI answers deletion, watcher control and AI creation are owned by
# dedicated route boundaries or remain deferred; no generic write dispatch exists.
WRITE_ALLOWLIST = {
    "api/log",
    "api/cycle/backup",
    "api/settings",
    "api/master",
}
