"""Guarded settings-adjacent actions with stable public contracts."""
import json
from urllib.parse import urlsplit

from repositories.aitool import UpstreamError, ai_tool


_DEFAULT_BROWSER_URL = "http://127.0.0.1:8080/db"
_TELEGRAM_FAILURE = "Telegram token/chat id chưa cấu hình hoặc gửi thất bại"
_TELEGRAM_UNAVAILABLE = b'{"error":"telegram test unavailable"}'
_BROWSER_UNAVAILABLE = b'{"error":"browser open unavailable"}'
_INVALID_BROWSER_REQUEST = b'{"error":"invalid browser request"}'


def _safe_error(body):
    raise UpstreamError(502, body)


def _decode_json(body):
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError()


def _valid_browser_url(url):
    if type(url) is not str or not url or len(url.encode("utf-8")) > 2048:
        return False
    lowered = url.lower()
    if any(marker in lowered for marker in ("%00", "%0a", "%0d")):
        return False
    if any(ord(char) < 32 or 0x7F <= ord(char) <= 0x9F for char in url):
        return False
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return False
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    return not any(char.isspace() for char in hostname)


def _prepare_browser_url(body):
    if body is None or not body.strip():
        return _DEFAULT_BROWSER_URL
    try:
        posted = _decode_json(body)
    except ValueError:
        raise UpstreamError(400, _INVALID_BROWSER_REQUEST)
    if not isinstance(posted, dict):
        raise UpstreamError(400, _INVALID_BROWSER_REQUEST)
    value = posted.get("url", "")
    if not value:
        return _DEFAULT_BROWSER_URL
    if not _valid_browser_url(value):
        raise UpstreamError(400, _INVALID_BROWSER_REQUEST)
    return value


def _validate_telegram(body, status, content_type):
    if "application/json" not in (content_type or ""):
        _safe_error(_TELEGRAM_UNAVAILABLE)
    try:
        response = _decode_json(body)
    except ValueError:
        _safe_error(_TELEGRAM_UNAVAILABLE)
    if status == 400 and response == {"error": _TELEGRAM_FAILURE}:
        return (
            json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            400,
            "application/json",
        )
    if status != 200 or response != {"status": "OK", "sent": True}:
        _safe_error(_TELEGRAM_UNAVAILABLE)
    return b'{"status":"OK","sent":true}', 200, "application/json"


def test_telegram():
    """Run the fixed upstream notifier test without forwarding client input."""
    try:
        result = ai_tool.test_telegram()
    except UpstreamError as error:
        if error.status == 400:
            return _validate_telegram(error.body, error.status, "application/json")
        _safe_error(_TELEGRAM_UNAVAILABLE)
    except Exception:
        _safe_error(_TELEGRAM_UNAVAILABLE)
    return _validate_telegram(*result)


def _validate_browser(body, status, content_type, url):
    if status != 200 or "application/json" not in (content_type or ""):
        _safe_error(_BROWSER_UNAVAILABLE)
    try:
        response = _decode_json(body)
    except ValueError:
        _safe_error(_BROWSER_UNAVAILABLE)
    if not isinstance(response, dict) or set(response) != {"status", "opened", "url"}:
        _safe_error(_BROWSER_UNAVAILABLE)
    if response["status"] not in {"OK", "error"} or type(response["opened"]) is not bool:
        _safe_error(_BROWSER_UNAVAILABLE)
    if ((response["status"] == "OK") != response["opened"]
            or response["url"] != url or type(response["url"]) is not str):
        _safe_error(_BROWSER_UNAVAILABLE)
    return (
        json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        200,
        "application/json",
    )


def open_browser(body):
    """Validate one URL, then call only the fixed upstream browser action."""
    url = _prepare_browser_url(body)
    try:
        result = ai_tool.open_browser(url)
    except UpstreamError:
        _safe_error(_BROWSER_UNAVAILABLE)
    except Exception:
        _safe_error(_BROWSER_UNAVAILABLE)
    return _validate_browser(*result, url)
