"""Service for the redacted read-only settings contract."""
import json

from repositories.aitool import UpstreamError, ai_tool


_PUBLIC_FIELDS = (
    ("tunnel_port", int),
    ("auto_restart_tunnel", bool),
    ("auto_telegram", bool),
    ("auto_open_browser", bool),
)


def _safe_error(status=502):
    """Never expose settings upstream bodies, which may contain secrets."""
    raise UpstreamError(status, b'{"error":"settings upstream response unavailable"}')


def _is_exact_type(value, expected):
    # bool is an int subclass; settings fields require their exact JSON types.
    return type(value) is expected


def get_settings():
    """GET api/settings and return only the approved public allowlist."""
    try:
        body, status, _content_type = ai_tool.get("api/settings")
    except UpstreamError as error:
        if 400 <= error.status <= 599:
            _safe_error(error.status)
        _safe_error()

    if status != 200:
        _safe_error(status if 400 <= status <= 599 else 502)

    try:
        settings = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _safe_error()
    if not isinstance(settings, dict):
        _safe_error()

    public = {}
    for field, expected in _PUBLIC_FIELDS:
        if field not in settings:
            continue
        value = settings[field]
        if not _is_exact_type(value, expected):
            _safe_error()
        if field == "tunnel_port" and not 1 <= value <= 65535:
            _safe_error()
        public[field] = value

    return (
        json.dumps(public, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        200,
        "application/json",
    )
