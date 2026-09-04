"""Service doc master data (passthrough, giu nguyen contract)."""
from repositories.aitool import ai_tool


def get_master():
    """GET clients_master.json — handler legacy, khong doi upstream path."""
    return ai_tool.get("clients_master.json")


def get_api_master():
    """GET api/master — handler rieng cho API master cua ai tool."""
    return ai_tool.get("api/master")


def get_database():
    """GET client_database.json."""
    return ai_tool.get("client_database.json")
