import os

AI_TOOL_API_BASE = os.environ.get("AI_TOOL_API_BASE", "http://127.0.0.1:8080").rstrip("/")
AI_TOOL_USER = os.environ.get("AI_TOOL_USER", "admin")
AI_TOOL_PASS = os.environ.get("AI_TOOL_PASS", "")
PORT = int(os.environ.get("WEBAPP_PORT", "8090"))
# Required for every write request; never hard-code or commit this value.
WEBAPP_WRITE_TOKEN = os.environ.get("WEBAPP_WRITE_TOKEN", "")

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
# Slice 8: guarded CAS rename POST api/master; Bundle 2: typed POST api/ai_fix.
# Sync, AI answers deletion and watcher control remain deferred until ownership
# exclusion is proven; no generic write dispatch is enabled for them.
WRITE_ALLOWLIST = {
    "api/log",
    "api/cycle/backup",
    "api/settings",
    "api/master",
    "api/ai_fix",
}
