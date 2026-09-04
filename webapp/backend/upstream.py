"""Chuyển tiếp GET read-only sang ai tool. Không bao giờ gửi method ghi."""
import urllib.request
import urllib.error

import config


class UpstreamError(Exception):
    """Lỗi upstream mang theo body + status để route trả đúng contract."""

    def __init__(self, status, body):
        super().__init__(body[:300] if isinstance(body, str) else "upstream error")
        self.status = status
        self.body = body


def forward_get(subpath, timeout=20):
    """Trả về (body_bytes, status, content_type). Raise UpstreamError."""
    import base64
    raw = f"{config.AI_TOOL_USER}:{config.AI_TOOL_PASS}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(raw).decode()}
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
