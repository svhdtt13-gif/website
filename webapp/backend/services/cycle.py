"""Service đọc trạng thái cycle (passthrough, giữ nguyên contract)."""
from repositories.aitool import ai_tool


def get_cycle_status():
    """GET api/cycle/status — chi tiết cycle + manual_overrides + qnyh."""
    return ai_tool.get("api/cycle/status")


def get_cycle_simple_status():
    """GET api/cycle_status — running/offline gọn."""
    return ai_tool.get("api/cycle_status")
