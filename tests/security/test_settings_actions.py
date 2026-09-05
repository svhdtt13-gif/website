#!/usr/bin/env python3
"""Bundle 1 contract/security tests for the two settings action routes."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "webapp", "backend")
TOKEN = "write-secret"
PASS = FAIL = 0


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PROXY_PORT, STUB_PORT = free_port(), free_port()


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + " " + detail[:300])


class Stub(BaseHTTPRequestHandler):
    hits = []
    telegram_mode = "success"
    browser_mode = "success"

    def _send(self, body, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _record(self, body):
        Stub.hits.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "marker": self.headers.get("X-DB-Editor", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
        })

    def do_GET(self):
        self._record(b"")
        if self.path == "/api/status":
            self._send(b'{"ok":true}')
        else:
            self._send(b'{"error":"not found"}', 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self._record(body)
        if self.path == "/api/settings/test_telegram":
            if Stub.telegram_mode == "failure":
                self._send(
                    json.dumps({"error": "Telegram token/chat id chưa cấu hình hoặc gửi thất bại"}, ensure_ascii=False).encode(),
                    400,
                )
            elif Stub.telegram_mode == "malformed":
                self._send(b"not-json")
            elif Stub.telegram_mode == "error":
                self._send(b'{"error":"secret upstream detail"}', 500)
            else:
                self._send(b'{"status":"OK","sent":true}')
            return
        if self.path == "/api/settings/open_browser":
            if Stub.browser_mode == "error":
                self._send(b'{"error":"secret upstream detail"}', 500)
            elif Stub.browser_mode == "malformed":
                self._send(b'{"status":"OK"}')
            elif Stub.browser_mode == "rejected":
                self._send(b'{"status":"error","opened":false,"url":"https://example.com"}')
            elif Stub.browser_mode == "mismatch_ok_false":
                self._send(b'{"status":"OK","opened":false,"url":"https://example.com"}')
            elif Stub.browser_mode == "mismatch_error_true":
                self._send(b'{"status":"error","opened":true,"url":"https://example.com"}')
            else:
                try:
                    url = json.loads(body.decode())["url"]
                except Exception:
                    url = ""
                self._send(json.dumps({"status": "OK", "opened": True, "url": url}).encode())
            return
        self._send(b'{"error":"not found"}', 404)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, *args):
        pass


def call(base, path, method="GET", data=None, headers=None):
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except Exception:
            body = b""
        return error.code, body, dict(error.headers)


def body_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env.update(
        AI_TOOL_API_BASE=f"http://127.0.0.1:{STUB_PORT}",
        AI_TOOL_USER="tester",
        AI_TOOL_PASS="dummy",
        WEBAPP_WRITE_TOKEN=TOKEN,
        WEBAPP_PORT=str(PROXY_PORT),
    )
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND, "proxy.py")],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    bearer = {"Authorization": "Bearer " + TOKEN}
    json_headers = dict(bearer, **{"Content-Type": "application/json"})
    try:
        ready = False
        for _ in range(40):
            time.sleep(0.25)
            if call(proxy, "/up/api/status")[0] == 200:
                ready = True
                break
        check("proxy boots", ready)
        if not ready:
            return 1

        for path in ("/up/api/settings/test_telegram", "/up/api/settings/open_browser"):
            Stub.hits.clear()
            status, body, headers = call(proxy, path, "POST", b"{}")
            challenge = headers.get("WWW-Authenticate") or headers.get("Www-Authenticate")
            check("missing Bearer -> 401 + challenge + zero upstream", status == 401
                  and challenge == "Bearer" and body_json(body) == {"error": "write authentication required"}
                  and not [hit for hit in Stub.hits if hit["method"] != "GET"])

            Stub.hits.clear()
            status, body, _ = call(proxy, path, "POST", b"{}", {"Authorization": "Bearer wrong"})
            check("wrong Bearer -> 401 + zero upstream", status == 401
                  and body_json(body) == {"error": "write authentication required"}
                  and not [hit for hit in Stub.hits if hit["method"] != "GET"])

            Stub.hits.clear()
            status, body, _ = call(proxy, path + "?unexpected=1", "POST", b"{}", bearer)
            check("query -> deterministic 400 + zero upstream", status == 400
                  and body_json(body) == {"error": "query parameters not allowed"}
                  and not [hit for hit in Stub.hits if hit["method"] != "GET"])

            for method in ("GET", "PUT", "PATCH", "DELETE"):
                Stub.hits.clear()
                status, body, _ = call(proxy, path, method, b"{}", bearer)
                check(method + " action -> 403 JSON + zero upstream", status == 403
                      and body_json(body) == {"error": "read-only proxy: write methods blocked"}
                      and not [hit for hit in Stub.hits if hit["method"] != "GET"])

        Stub.hits.clear()
        Stub.telegram_mode = "success"
        status, raw, headers = call(
            proxy,
            "/up/api/settings/test_telegram",
            "POST",
            b'{"token":"CLIENT_SECRET","message":"CLIENT_MESSAGE"}',
            dict(json_headers, **{"X-DB-Editor": "forged"}),
        )
        posts = [hit for hit in Stub.hits if hit["method"] == "POST"]
        check("Telegram success exact response", status == 200
              and body_json(raw) == {"status": "OK", "sent": True}
              and "application/json" in headers.get("Content-Type", "")
              and len(posts) == 1)
        check("Telegram typed request owns auth/marker and sends empty body",
              len(posts) == 1 and posts[0]["path"] == "/api/settings/test_telegram"
              and posts[0]["body"] == b"" and posts[0]["auth"].startswith("Basic ")
              and posts[0]["marker"] == "1"
              and b"CLIENT_SECRET" not in raw and b"CLIENT_MESSAGE" not in raw)

        Stub.telegram_mode = "failure"
        Stub.hits.clear()
        status, raw, _ = call(proxy, "/up/api/settings/test_telegram", "POST", b"", bearer)
        check("Telegram known failure preserves 400 contract", status == 400
              and body_json(raw) == {"error": "Telegram token/chat id chưa cấu hình hoặc gửi thất bại"}
              and len([hit for hit in Stub.hits if hit["method"] == "POST"]) == 1)

        for mode in ("error", "malformed"):
            Stub.telegram_mode = mode
            Stub.hits.clear()
            status, raw, _ = call(proxy, "/up/api/settings/test_telegram", "POST", b"", bearer)
            posts = [hit for hit in Stub.hits if hit["method"] == "POST"]
            check("Telegram " + mode + " -> exact 502/no echo/no retry", status == 502
                  and body_json(raw) == {"error": "telegram test unavailable"}
                  and len(posts) == 1 and b"secret" not in raw)

        Stub.telegram_mode = "success"
        Stub.hits.clear()
        status, raw, _ = call(proxy, "/up/api/settings/test_telegram", "POST", b"{}", bearer)
        check("Telegram route recovers after failures", status == 200 and body_json(raw) == {"status": "OK", "sent": True})

        Stub.hits.clear()
        status, raw, _ = call(proxy, "/up/api/settings/open_browser", "POST", b"", json_headers)
        posts = [hit for hit in Stub.hits if hit["method"] == "POST"]
        check("Browser empty body uses canonical default", status == 200
              and body_json(raw) == {"status": "OK", "opened": True, "url": "http://127.0.0.1:8080/db"}
              and len(posts) == 1 and json.loads(posts[0]["body"]) == {"url": "http://127.0.0.1:8080/db"})

        url = "https://example.com/path?q=1"
        Stub.hits.clear()
        status, raw, _ = call(proxy, "/up/api/settings/open_browser", "POST",
                              json.dumps({"url": url, "ignored": "secret"}).encode(), json_headers)
        posts = [hit for hit in Stub.hits if hit["method"] == "POST"]
        check("Browser valid URL exact response", status == 200
              and body_json(raw) == {"status": "OK", "opened": True, "url": url}
              and len(posts) == 1 and json.loads(posts[0]["body"]) == {"url": url})

        Stub.browser_mode = "rejected"
        status, raw, _ = call(proxy, "/up/api/settings/open_browser", "POST",
                              json.dumps({"url": "https://example.com"}).encode(), json_headers)
        check("Browser launch rejection preserves 200 error shape", status == 200
              and body_json(raw) == {"status": "error", "opened": False, "url": "https://example.com"})

        for mode, label in (("mismatch_ok_false", "OK with opened=false"),
                            ("mismatch_error_true", "error with opened=true")):
            Stub.browser_mode = mode
            Stub.hits.clear()
            status, raw, _ = call(proxy, "/up/api/settings/open_browser", "POST",
                                  b'{"url":"https://example.com"}', json_headers)
            posts = [hit for hit in Stub.hits if hit["method"] == "POST"]
            check("Browser " + label + " -> exact 502/no retry", status == 502
                  and body_json(raw) == {"error": "browser open unavailable"}
                  and len(posts) == 1)

        invalid = [
            ("malformed", b"{not-json"),
            ("array", b"[]"),
            ("null", b"null"),
            ("ftp scheme", b'{"url":"ftp://example.com"}'),
            ("missing host", b'{"url":"https:///path"}'),
            ("userinfo", b'{"url":"https://user:pass@example.com"}'),
            ("control", b'{"url":"https://example.com/\r\nX"}'),
            ("encoded control", b'{"url":"https://example.com/%0d%0aX"}'),
            ("too long", json.dumps({"url": "https://" + "a" * 2048 + ".com"}).encode()),
        ]
        Stub.browser_mode = "success"
        for label, payload in invalid:
            Stub.hits.clear()
            status, raw, _ = call(proxy, "/up/api/settings/open_browser", "POST", payload, json_headers)
            check("Browser " + label + " -> deterministic 400/zero upstream", status == 400
                  and body_json(raw) == {"error": "invalid browser request"}
                  and not [hit for hit in Stub.hits if hit["method"] != "GET"])

        for mode in ("error", "malformed"):
            Stub.browser_mode = mode
            Stub.hits.clear()
            status, raw, _ = call(proxy, "/up/api/settings/open_browser", "POST",
                                  b'{"url":"https://example.com"}', json_headers)
            posts = [hit for hit in Stub.hits if hit["method"] == "POST"]
            check("Browser " + mode + " -> exact 502/no echo/no retry", status == 502
                  and body_json(raw) == {"error": "browser open unavailable"}
                  and len(posts) == 1 and b"secret" not in raw)

        Stub.browser_mode = "success"
        check("proxy survives Bundle 1 failures", call(proxy, "/up/api/status")[0] == 200)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            stub.shutdown()
            stub.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
