"""Guarded AI-fix queue actions; filesystem ownership stays in ai tool."""
import json
import re
import unicodedata

from repositories.aitool import UpstreamError, ai_tool


_COMMANDS = {
    "cycle": "Trong dự án ai tool, hãy kiểm tra và fix lỗi cycle: đọc tools/AutoCycle.ps1, tools/cache/cycle.log, tools/cache/cycle_state.json và tools/client_database.json; kiểm tra trạng thái AutoCycle/sync workers qua /api/cycle/status; sửa lỗi rồi restart worker và xác minh lại.",
    "web": "Trong dự án ai tool, hãy kiểm tra và fix trang web db.html: đọc tools/db.html và WebAppControl/flask/app_public.py; kiểm tra lỗi console trình duyệt và các API /api/settings, /api/cycle/fix; sửa rồi xác minh lại trên trình duyệt.",
}
_USERIMPORT_PREFIX = "[Userimport - dự án ai tool] "
_FILE_RE = re.compile(r"^ai_fix_(?:cycle|web|userimport)_\d{8}_\d{6}\.json$")
_UNAVAILABLE = b'{"error":"ai fix unavailable"}'
_INVALID = b'{"error":"invalid ai fix request"}'


def _safe_error():
    raise UpstreamError(502, _UNAVAILABLE)


def _has_control_chars(value):
    return any(unicodedata.category(char).startswith("C") for char in value)


def _prepare_request(body):
    try:
        posted = json.loads(body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        raise UpstreamError(400, _INVALID)
    if not isinstance(posted, dict) or "kind" not in posted:
        raise UpstreamError(400, _INVALID)
    kind = posted["kind"]
    if type(kind) is not str or _has_control_chars(kind):
        raise UpstreamError(400, _INVALID)
    kind = kind.strip().lower()
    if kind not in {"cycle", "web", "userimport"}:
        raise UpstreamError(400, _INVALID)
    expected_keys = {"kind", "text"} if kind == "userimport" else {"kind"}
    if set(posted) != expected_keys:
        raise UpstreamError(400, _INVALID)
    if kind == "userimport":
        text = posted["text"]
        if type(text) is not str or _has_control_chars(text):
            raise UpstreamError(400, _INVALID)
        text = text.strip()
        if not text or len(text) > 2000:
            raise UpstreamError(400, _INVALID)
        return kind, text, _USERIMPORT_PREFIX + text
    return kind, None, _COMMANDS[kind]


def _decode_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError()


def _validate_response(body, status, content_type, kind, command):
    if status != 200 or "application/json" not in (content_type or ""):
        _safe_error()
    try:
        response = _decode_json(body)
    except ValueError:
        _safe_error()
    if not isinstance(response, dict) or set(response) != {"status", "kind", "project", "file", "command"}:
        _safe_error()
    if response["status"] != "OK" or response["kind"] != kind or response["project"] != "ai tool":
        _safe_error()
    if type(response["file"]) is not str or not _FILE_RE.fullmatch(response["file"]):
        _safe_error()
    if response["command"] != command:
        _safe_error()
    return (
        json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        200,
        "application/json",
    )


def create_ai_fix(body):
    """Validate the frozen request, then create it through the typed repository."""
    kind, text, command = _prepare_request(body)
    try:
        result = ai_tool.create_ai_fix(kind, text)
    except Exception:
        _safe_error()
    return _validate_response(*result, kind, command)


def get_ai_fix_status():
    """GET api/ai_fix/status — watcher + pending + models."""
    return ai_tool.get("api/ai_fix/status")
