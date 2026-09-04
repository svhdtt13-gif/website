#!/usr/bin/env python3
"""Slice 5 security/contract tests for redacted GET /up/api/settings.

Uses a local upstream stub and proxy subprocess; never contacts ai tool and
never reads or writes real settings data.
"""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_DIR = os.path.join(REPO_ROOT, "webapp", "backend")
PASS_COUNT = 0
FAIL_COUNT = 0


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PROXY_PORT = free_port()
STUB_PORT = free_port()
PUBLIC_KEYS = {
    "default_browser", "tunnel_port", "auto_restart_tunnel",
    "auto_telegram", "auto_open_browser",
}
CANARIES = (
    "BOT_TOKEN_CANARY",
    "CHAT_ID_CANARY",
    "PASSWORD_CANARY",
    "NESTED_SECRET_CANARY",
)


def check(name, condition, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"PASS: {name}")
    else:
        FAIL_COUNT += 1
        print(f"FAIL: {name} {detail}"[:350])


class Stub(BaseHTTPRequestHandler):
    hits = []
    mode = "valid"

    valid = {
        "telegram_bot_token": CANARIES[0],
        "telegram_chat_id": CANARIES[1],
        "effective_telegram_bot_token": CANARIES[0],
        "effective_telegram_chat_id": CANARIES[1],
        "cloudflared_path": r"C:\secret\cloudflared.exe",
        "default_browser": "chrome",
        "tunnel_port": 18080,
        "auto_restart_tunnel": True,
        "auto_telegram": False,
        "auto_open_browser": True,
        "unknown": {"password": CANARIES[2], "nested": {"secret": CANARIES[3]}},
    }

    def _rec(self, body=b""):
        Stub.hits.append((self.command, self.path,
                          self.headers.get("Authorization", ""), body))

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        self._rec()
        if self.path != "/api/settings":
            self._send(b'{"error":"not found"}', 404)
            return
        if Stub.mode == "valid":
            self._send(json.dumps(Stub.valid).encode())
        elif Stub.mode == "missing":
            self._send(json.dumps({
                "default_browser": "default",
                "auto_telegram": True,
                "unknown": {"nested": CANARIES[3]},
            }).encode())
        elif Stub.mode == "malformed":
            self._send(b'{"telegram_bot_token":"' + CANARIES[0].encode() + b'"')
        elif Stub.mode == "array":
            self._send(b'[{"telegram_bot_token":"' + CANARIES[0].encode() + b'"}]')
        elif Stub.mode == "http_error":
            self._send(json.dumps({
                "error": "upstream failed",
                "telegram_bot_token": CANARIES[0],
                "nested": {"password": CANARIES[2]},
            }).encode(), 503)
        elif Stub.mode == "unauthorized":
            self._send(json.dumps({
                "error": "unauthorized",
                "telegram_bot_token": CANARIES[0],
            }).encode(), 401)

    def _write(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        self._rec(raw)
        self._send(b'{"ok":true}')

    do_POST = _write
    do_PUT = _write
    do_PATCH = _write
    do_DELETE = _write

    def log_message(self, *args):
        pass


def http_call(base, path, method="GET", data=None, headers=None, timeout=10):
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read(), response.headers.get_content_type()
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except Exception:
            body = b""
        return error.code, body, ""


def json_body(body):
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return None


def main():
    stub = HTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever,
                     kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env["AI_TOOL_API_BASE"] = f"http://127.0.0.1:{STUB_PORT}"
    env["AI_TOOL_USER"] = "tester"
    env["AI_TOOL_PASS"] = "dummy"
    env["WEBAPP_PORT"] = str(PROXY_PORT)
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BACKEND_DIR, "proxy.py")],
        cwd=BACKEND_DIR, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                status, _, _ = http_call(proxy, "/up/api/status")
                if status == 200:
                    ready = True
                    break
            except Exception:
                pass
        check("proxy boots", ready)
        if not ready:
            return 1

        # Valid source: exact public allowlist, no secret or unknown field.
        Stub.mode = "valid"
        Stub.hits.clear()
        status, body, ctype = http_call(proxy, "/up/api/settings")
        data = json_body(body)
        expected = {
            "default_browser": "chrome",
            "tunnel_port": 18080,
            "auto_restart_tunnel": True,
            "auto_telegram": False,
            "auto_open_browser": True,
        }
        check("valid settings -> exact redacted object + JSON",
              status == 200 and data == expected
              and set(data).issubset(PUBLIC_KEYS)
              and "application/json" in (ctype or ""),
              f"status={status} body={body[:250]}")
        check("valid response contains no secret canary",
              not any(value.encode() in body for value in CANARIES),
              body[:300])
        check("settings request path and Basic auth forwarded",
              len(Stub.hits) == 1 and Stub.hits[0][1] == "/api/settings"
              and Stub.hits[0][2].startswith("Basic "))

        # Missing source keys stay absent; no defaults/placeholders are made.
        Stub.mode = "missing"
        status, body, _ = http_call(proxy, "/up/api/settings")
        data = json_body(body)
        check("missing source keys remain absent",
              status == 200 and data == {"default_browser": "default", "auto_telegram": True}
              and set(data).issubset(PUBLIC_KEYS), f"got {data}")

        # A successful but unsafe/invalid payload fails closed.
        for mode, label in (("malformed", "malformed JSON"), ("array", "non-object JSON")):
            Stub.mode = mode
            status, body, _ = http_call(proxy, "/up/api/settings")
            data = json_body(body)
            check(f"{label} -> 502 generic JSON",
                  status == 502 and isinstance(data, dict) and "error" in data
                  and not any(value.encode() in body for value in CANARIES),
                  f"status={status} body={body[:200]}")

        # Upstream error status is preserved, but its body is sanitized.
        for mode, expected_status, label in (("http_error", 503, "503"),
                                               ("unauthorized", 401, "401")):
            Stub.mode = mode
            status, body, _ = http_call(proxy, "/up/api/settings")
            data = json_body(body)
            check(f"upstream {label} -> status + sanitized JSON",
                  status == expected_status and isinstance(data, dict)
                  and data == {"error": "settings upstream response unavailable"}
                  and not any(value.encode() in body for value in CANARIES),
                  f"status={status} body={body[:250]}")

        # Restore valid mode to prove the proxy survives all settings failures.
        Stub.mode = "valid"
        status, _, _ = http_call(proxy, "/up/api/settings")
        check("proxy survives settings payload and upstream errors", status == 200)

        # All write methods and related write endpoints remain blocked locally.
        Stub.hits.clear()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, body, _ = http_call(proxy, "/up/api/settings", method=method, data=b"{}")
            check(f"{method} settings -> 403 JSON", status == 403 and b'"error"' in body,
                  f"got {status}")
        for path in ("/up/api/settings/test_telegram", "/up/api/settings/open_browser"):
            status, body, _ = http_call(proxy, path, method="POST", data=b"{}")
            check(f"POST {path} -> 403 JSON", status == 403 and b'"error"' in body,
                  f"got {status}")
        check("settings write attempts cause zero upstream writes",
              not [hit for hit in Stub.hits if hit[0] != "GET"])

        source_path = os.path.join(BACKEND_DIR, "services", "settings.py")
        with open(source_path, encoding="utf-8") as source_file:
            source = source_file.read()
        check("settings service has no direct file access", "open(" not in source)
        check("settings service has positive allowlist and no raw return",
              "_PUBLIC_FIELDS" in source and "api/settings" in source
              and "return ai_tool.get" not in source)

        # Stop upstream and require a generic 502 without leaking repository body.
        stub.shutdown()
        stub.server_close()
        time.sleep(0.2)
        status, body, _ = http_call(proxy, "/up/api/settings", timeout=10)
        data = json_body(body)
        check("upstream unreachable -> 502 generic JSON",
              status == 502 and data == {"error": "settings upstream response unavailable"}
              and not any(value.encode() in body for value in CANARIES),
              f"status={status} body={body[:200]}")
        check("proxy survives settings upstream outage", proc.poll() is None)

        print(f"\nSUMMARY: {PASS_COUNT} passed, {FAIL_COUNT} failed")
        return 0 if FAIL_COUNT == 0 else 1
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            stub.shutdown()
            stub.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
