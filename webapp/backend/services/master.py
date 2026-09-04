"""Service đọc master data (passthrough, giữ nguyên contract)."""
from repositories.aitool import ai_tool


def get_master():
    """GET clients_master.json."""
    return ai_tool.get("clients_master.json")


def get_database():
    """GET client_database.json."""
    return ai_tool.get("client_database.json")
