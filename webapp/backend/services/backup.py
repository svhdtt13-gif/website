"""Service doc metadata backup (passthrough, giu nguyen contract ai tool)."""
from repositories.aitool import ai_tool


def get_cycle_backups():
    """GET api/cycle/backup — chi doc danh sach metadata backup."""
    return ai_tool.get("api/cycle/backup")
