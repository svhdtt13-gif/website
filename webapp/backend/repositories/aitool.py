"""Repository: nguon du lieu ai tool qua HTTP (read + write han che).

Slice 1: get() passthrough cho 7 path GET (giuyen tu upstream.py cu).
Slice 2: post() chi dung cho POST api/log — noi duy nhat thuc hien HTTP write
toi ai tool o giai doan nay. Route/service khong duoc ghi file truc tiep.
Slice 11: delete_backup() is the only dynamic DELETE repository operation.
Bundle 1: typed settings action methods own their POST boundaries.
"""
import json
import re
import urllib.error
import urllib.request
from urllib.parse import quote

import config


class UpstreamError(Exception):
    """Loi upstream mang theo body + status de route tra dung contract."""

    def __init__(self, status, body):
        super().__init__(body[:300] if isinstance(body, str) else "upstream error")
        self.status = status
        self.body = body


class AiToolRepository:
    """Doc/ghi du lieu tu ai tool. Moi response di qua day de sau nay
    thay nguon (SQLite) ma service/route khong phai sua."""

    def _auth_headers(self):
        import base64
        raw = f"{config.AI_TOOL_USER}:{config.AI_TOOL_PASS}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}

    def get(self, subpath, timeout=20):
        """Tra ve (body_bytes, status, content_type). Raise UpstreamError."""
        headers = self._auth_headers()
        url = f"{config.AI_TOOL_API_BASE}/{subpath}"
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status, r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b'{"error":"upstream error"}'
            raise UpstreamError(e.code, body)
        except Exception as e:
            raise UpstreamError(502, ('{"error":"upstream unreachable: %s"}' % e)[:300].encode())

    def post(self, subpath, body, content_type="application/json", timeout=20):
        """POST raw body toi ai tool. Chi dung cho path trong WRITE_ALLOWLIST."""
        if subpath not in config.WRITE_ALLOWLIST:
            raise UpstreamError(403, b'{"error":"write endpoint not allowed"}')
        headers = self._auth_headers()
        headers["Content-Type"] = content_type or "application/json"
        headers["X-DB-Editor"] = "1"
        url = f"{config.AI_TOOL_API_BASE}/{subpath}"
        req = urllib.request.Request(
            url, data=body if body is not None else b"", headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status, r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            try:
                body_out = e.read()
            except Exception:
                body_out = b'{"error":"upstream error"}'
            raise UpstreamError(e.code, body_out)
        except Exception as e:
            raise UpstreamError(502, ('{"error":"upstream unreachable: %s"}' % e)[:300].encode())

    def _post_action(self, subpath, body=b"", content_type=None, timeout=20):
        """POST a fixed action path with repository-owned auth and marker."""
        headers = self._auth_headers()
        headers["X-DB-Editor"] = "1"
        if content_type:
            headers["Content-Type"] = content_type
        url = f"{config.AI_TOOL_API_BASE}/{subpath}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status, r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            try:
                body_out = e.read()
            except Exception:
                body_out = b'{"error":"upstream error"}'
            raise UpstreamError(e.code, body_out)
        except Exception as e:
            raise UpstreamError(502, ('{"error":"upstream unreachable: %s"}' % e)[:300].encode())

    def test_telegram(self, timeout=20):
        """POST exactly the fixed Telegram test action with an empty body."""
        return self._post_action("api/settings/test_telegram", timeout=timeout)

    def open_browser(self, url, timeout=20):
        """POST one validated browser URL to the fixed action endpoint."""
        body = json.dumps({"url": url}, separators=(",", ":")).encode("utf-8")
        return self._post_action(
            "api/settings/open_browser", body, "application/json", timeout
        )

    def delete_backup(self, name, timeout=20):
        """DELETE exactly one canonical backup name; no generic DELETE primitive."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            raise UpstreamError(400, b'{"error":"invalid backup name"}')
        headers = self._auth_headers()
        headers["X-DB-Editor"] = "1"
        url = f"{config.AI_TOOL_API_BASE}/api/cycle/backup/{quote(name, safe='ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.-')}"
        req = urllib.request.Request(url, headers=headers, method="DELETE")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read(), r.status, r.headers.get_content_type()
        except urllib.error.HTTPError as e:
            try:
                body = e.read()
            except Exception:
                body = b'{"error":"upstream error"}'
            raise UpstreamError(e.code, body)
        except Exception as e:
            raise UpstreamError(502, ('{"error":"upstream unreachable: %s"}' % e)[:300].encode())


ai_tool = AiToolRepository()
