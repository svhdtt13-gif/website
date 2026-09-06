"""Services for the shared redacted settings contract."""
import json
import threading

from repositories.aitool import UpstreamError, ai_tool


_PUBLIC_FIELDS = (
    ("tunnel_port", int),
    ("auto_restart_tunnel", bool),
    ("auto_telegram", bool),
    ("auto_open_browser", bool),
)
_PUBLIC_FIELD_NAMES = frozenset(field for field, _expected in _PUBLIC_FIELDS)
_SETTINGS_WRITE_LOCK = threading.Lock()
_ALLOWED_JSON_CONTENT_TYPES = {"application/json", "application/json; charset=utf-8"}


class SettingsValidationError(Exception):
    """The source or request does not satisfy the settings contract."""


def _safe_error(status=502):
    """Never expose settings upstream bodies, which may contain secrets."""
    raise UpstreamError(status, b'{"error":"settings upstream response unavailable"}')


def _invalid_request():
    raise UpstreamError(400, b'{"error":"invalid settings request"}')


def _safe_upstream_error(status):
    _safe_error(status if 400 <= status <= 599 else 502)


def _is_exact_type(value, expected):
    # bool is an int subclass; settings fields require their exact JSON types.
    return type(value) is expected


def _project_public_settings(settings):
    """Validate and project the one public settings allowlist."""
    if not isinstance(settings, dict):
        raise SettingsValidationError()
    public = {}
    for field, expected in _PUBLIC_FIELDS:
        if field not in settings:
            continue
        value = settings[field]
        if not _is_exact_type(value, expected):
            raise SettingsValidationError()
        if field == "tunnel_port" and not 1 <= value <= 65535:
            raise SettingsValidationError()
        public[field] = value
    return public


def _decode_object(body):
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SettingsValidationError()
    if not isinstance(value, dict):
        raise SettingsValidationError()
    return value


def get_settings(timeout=20):
    """GET api/settings and return only the shared public allowlist."""
    try:
        body, status, _content_type = ai_tool.get("api/settings", timeout=timeout)
    except UpstreamError as error:
        _safe_upstream_error(error.status)

    if status != 200:
        _safe_upstream_error(status)
    try:
        public = _project_public_settings(_decode_object(body))
    except SettingsValidationError:
        _safe_error()
    return (
        json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        200,
        "application/json",
    )


def _prepare_update(body, content_type):
    if (content_type or "").strip().lower() not in _ALLOWED_JSON_CONTENT_TYPES:
        _invalid_request()
    if not body:
        _invalid_request()
    try:
        request_settings = _decode_object(body)
        if not request_settings:
            raise SettingsValidationError()
        if set(request_settings) - _PUBLIC_FIELD_NAMES:
            raise SettingsValidationError()
        public = _project_public_settings(request_settings)
        if set(public) != set(request_settings):
            raise SettingsValidationError()
    except SettingsValidationError:
        _invalid_request()
    return json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _project_success(body, status, content_type):
    if status != 200 or "application/json" not in (content_type or ""):
        _safe_error()
    try:
        response = _decode_object(body)
        if response.get("status") != "OK":
            raise SettingsValidationError()
        public = _project_public_settings(response.get("settings"))
    except SettingsValidationError:
        _safe_error()
    safe_response = {"status": "OK", "settings": public}
    return (
        json.dumps(safe_response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        200,
        "application/json",
    )


def update_settings(body, content_type="application/json", before_upstream_write=None):
    """Validate safe partial settings, write once, then redact the response."""
    request_body = _prepare_update(body, content_type)
    with _SETTINGS_WRITE_LOCK:
        try:
            if before_upstream_write is None:
                result = ai_tool.post("api/settings", request_body, "application/json")
                return _project_success(*result)
            with before_upstream_write():
                result = ai_tool.post("api/settings", request_body, "application/json")
                return _project_success(*result)
        except UpstreamError as error:
            _safe_upstream_error(error.status)
