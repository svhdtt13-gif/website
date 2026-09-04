"""Service ghi log (passthrough, giu nguyen contract ai tool POST /api/log)."""
from repositories.aitool import ai_tool


def append_log(body, content_type="application/json"):
    """POST api/log — forward raw body, giu nguyen semantics ke ca JSON sai.

    Route khong parse/validate JSON; repository la noi duy nhat thuc hien
    HTTP write toi ai tool. Khong ghi file truc tiep o day.
    """
    return ai_tool.post("api/log", body, content_type)
