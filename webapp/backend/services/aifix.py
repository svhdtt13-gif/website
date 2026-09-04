"""Service đọc hàng chờ AI fix (passthrough, giữ nguyên contract)."""
from repositories.aitool import ai_tool


def get_ai_fix_status():
    """GET api/ai_fix/status — watcher + pending + models."""
    return ai_tool.get("api/ai_fix/status")
