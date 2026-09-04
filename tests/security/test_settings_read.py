#!/usr/bin/env python3
"""Slice 5 security tests for redacted GET /up/api/settings."""
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

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "webapp", "backend")
PASS = FAIL = 0

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

PROXY_PORT, STUB_PORT = free_port(), free_port()
PUBLIC_KEYS = {"tunnel_port", "auto_restart_tunnel", "auto_telegram", "auto_open_browser"}
CANARIES = ("BOT_TOKEN_CANARY", "CHAT_ID_CANARY", "PASSWORD_CANARY", "NESTED_SECRET_CANARY")


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + " " + detail[:350])


class Stub(BaseHTTPRequestHandler):
    hits = []
    mode = "valid"
    valid = {
        "telegram_bot_token": CANARIES[0], "telegram_chat_id": CANARIES[1],
        "effective_telegram_bot_token": CANARIES[0], "effective_telegram_chat_id": CANARIES[1],
        "cloudflared_path": r"C:\secret\cloudflared.exe",
        "default_browser": "chrome", "tunnel_port": 18080,
        "auto_restart_tunnel": True, "auto_telegram": False, "auto_open_browser": True,
        "unknown": {"password": CANARIES[2], "nested": {"secret": CANARIES[3]}},
    }

    def _send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _rec(self, body=b""):
        Stub.hits.append((self.command, self.path, self.headers.get("Authorization", ""), body))

    def do_GET(self):
        self._rec()
        if self.path == "/api/status":
            self._send(b'{"ok":true}')
        elif self.path != "/api/settings":
            self._send(b'{"error":"not found"}', 404)
        elif Stub.mode == "valid":
            self._send(json.dumps(Stub.valid).encode())
        elif Stub.mode == "missing":
            self._send(json.dumps({"auto_telegram": True,
                                   "unknown": {"nested": CANARIES[3]}}).encode())
        elif Stub.mode == "allowlisted_canary":
            payload = dict(Stub.valid)
            payload["tunnel_port"] = CANARIES[0]
            self._send(json.dumps(payload).encode())
        elif Stub.mode == "port_low":
            payload = dict(Stub.valid)
            payload["tunnel_port"] = 0
            self._send(json.dumps(payload).encode())
        elif Stub.mode == "port_high":
            payload = dict(Stub.valid)
            payload["tunnel_port"] = 65536
            self._send(json.dumps(payload).encode())
        elif Stub.mode == "malformed":
            self._send(b'{"telegram_bot_token":"' + CANARIES[0].encode())
        elif Stub.mode == "array":
            self._send(b'[{"telegram_bot_token":"' + CANARIES[0].encode() + b'"}]')
        elif Stub.mode == "http_error":
            self._send(json.dumps({"error": "upstream failed", "telegram_bot_token": CANARIES[0],
                                   "nested": {"password": CANARIES[2]}}).encode(), 503)
        elif Stub.mode == "unauthorized":
            self._send(json.dumps({"error": "unauthorized", "telegram_bot_token": CANARIES[0]}).encode(), 401)

    def _write(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self._rec(self.rfile.read(length) if length else b"")
        self._send(b'{"ok":true}')

    do_POST = _write
    do_PUT = _write
    do_PATCH = _write
    do_DELETE = _write

    def log_message(self, *args):
        pass


def call(base, path, method="GET", data=None, timeout=10):
    req = urllib.request.Request(base + path, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), ""


def body_json(raw):
    try:
        return json.loads(raw.decode())
    except Exception:
        return None


def main():
    stub = HTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env.update(AI_TOOL_API_BASE=f"http://127.0.0.1:{STUB_PORT}", AI_TOOL_USER="tester",
               AI_TOOL_PASS="dummy", WEBAPP_PORT=str(PROXY_PORT))
    proc = subprocess.Popen([sys.executable, os.path.join(BACKEND, "proxy.py")],
                            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    try:
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            status, _, _ = call(proxy, "/up/api/settings")
            if status == 200:
                ready = True
                break
        check("proxy boots", ready)
        if not ready:
            return 1

        Stub.mode = "valid"
        Stub.hits.clear()
        status, raw, ctype = call(proxy, "/up/api/settings")
        data = body_json(raw)
        expected = {"tunnel_port": 18080, "auto_restart_tunnel": True,
                    "auto_telegram": False, "auto_open_browser": True}
        check("valid response is exact positive allowlist JSON", status == 200 and data == expected
              and set(data).issubset(PUBLIC_KEYS) and "application/json" in ctype, str(data))
        check("valid response has no secret canary", not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))
        check("settings path and Basic auth forwarded", len(Stub.hits) == 1
              and Stub.hits[0][1] == "/api/settings" and Stub.hits[0][2].startswith("Basic "))

        Stub.mode = "missing"
        status, raw, _ = call(proxy, "/up/api/settings")
        check("missing keys stay absent", status == 200 and body_json(raw) == {"auto_telegram": True}, raw.decode(errors="replace"))

        for mode, label in (("allowlisted_canary", "allowlisted canary"),
                            ("port_low", "port below range"), ("port_high", "port above range")):
            Stub.mode = mode
            status, raw, _ = call(proxy, "/up/api/settings")
            data = body_json(raw)
            check(label + " -> generic 502", status == 502 and isinstance(data, dict)
                  and data == {"error": "settings upstream response unavailable"}
                  and not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))

        for mode, label in (("malformed", "malformed JSON"), ("array", "non-object JSON")):
            Stub.mode = mode
            status, raw, _ = call(proxy, "/up/api/settings")
            data = body_json(raw)
            check(label + " 200 -> generic 502", status == 502 and isinstance(data, dict)
                  and "error" in data and not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))

        for mode, expected_status in (("http_error", 503), ("unauthorized", 401)):
            Stub.mode = mode
            status, raw, _ = call(proxy, "/up/api/settings")
            check("upstream error status sanitized", status == expected_status
                  and body_json(raw) == {"error": "settings upstream response unavailable"}
                  and not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))

        Stub.mode = "valid"
        check("proxy survives settings failures", call(proxy, "/up/api/settings")[0] == 200)

        Stub.hits.clear()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, raw, _ = call(proxy, "/up/api/settings", method, b"{}")
            check(method + " settings -> 403 JSON", status == 403 and b'"error"' in raw, str(status))
        for path in ("/up/api/settings/test_telegram", "/up/api/settings/open_browser"):
            status, raw, _ = call(proxy, path, "POST", b"{}")
            check("related settings write -> 403 JSON", status == 403 and b'"error"' in raw, str(status))
        check("all settings write attempts cause zero upstream writes",
              not [h for h in Stub.hits if h[0] != "GET"])

        with open(os.path.join(BACKEND, "services", "settings.py"), encoding="utf-8") as f:
            source = f.read()
        check("settings service has no direct file access", "open(" not in source)
        check("settings service uses positive allowlist, not raw return",
              "_PUBLIC_FIELDS" in source and "api/settings" in source and "return ai_tool.get" not in source)

        stub.shutdown()
        stub.server_close()
        time.sleep(0.2)
        status, raw, _ = call(proxy, "/up/api/settings", timeout=10)
        check("unreachable -> generic 502", status == 502
              and body_json(raw) == {"error": "settings upstream response unavailable"}
              and not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))
        check("proxy survives settings outage", proc.poll() is None)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL else 1
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
