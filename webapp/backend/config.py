import os

AI_TOOL_API_BASE = os.environ.get("AI_TOOL_API_BASE", "http://127.0.0.1:8080").rstrip("/")
AI_TOOL_USER = os.environ.get("AI_TOOL_USER", "admin")
AI_TOOL_PASS = os.environ.get("AI_TOOL_PASS", "")
PORT = int(os.environ.get("WEBAPP_PORT", "8090"))

# Chỉ các endpoint GET này được proxy (read-only). Mọi thứ khác -> 403.
READ_ONLY_ALLOWLIST = {
    "api/cycle/status",
    "api/cycle_status",
    "api/sync_status",
    "api/status",
    "api/ai_fix/status",
    "clients_master.json",
    "client_database.json",
}
