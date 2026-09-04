"""Service đọc trạng thái sync (passthrough, giữ nguyên contract)."""
from repositories.aitool import ai_tool


def get_sync_status():
    """GET api/sync_status."""
    return ai_tool.get("api/sync_status")


def get_general_status():
    """GET api/status."""
    return ai_tool.get("api/status")
