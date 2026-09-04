"""Repository: nguon du lieu ai tool qua HTTP (read + write han che).

Slice 1: get() passthrough cho 7 path GET (giuyen tu upstream.py cu).
Slice 2: post() chi dung cho POST api/log — noi duy nhat thuc hien HTTP write
toi ai tool o giai doan nay. Route/service khong duoc ghi file truc tiep.
"""
import urllib.request
import urllib.error

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
        """POST raw body toi ai tool. Chi goi cho path trong WRITE_ALLOWLIST.

        Tra ve (body_bytes, status, content_type). Raise UpstreamError.
        Bao gom Basic Auth + X-DB-Editor: 1 (ai tool yeu cau cho moi POST).
        """
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


ai_tool = AiToolRepository()
