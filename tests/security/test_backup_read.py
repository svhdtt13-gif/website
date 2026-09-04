#!/usr/bin/env python3
"""Regression/security tests for GET /up/api/cycle/backup after Slice 6."""
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
    with socket.socket(socket.AF_INET, SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PROXY_PORT = free_port()
STUB_PORT = free_port()


def check(name, condition, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"PASS: {name}")
    else:
        FAIL_COUNT += 1
        print(f"FAIL: {name} {detail}"[:300])


class Stub(BaseHTTPRequestHandler):
    hits = []

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
        if self.path == "/api/cycle/backup":
            self._send(json.dumps({
                "backups": [{
                    "name": "cycle_demo.zip",
                    "size": 123,
                    "mtime": "2026-09-04T09:00:00",
                    "label": "demo",
                    "created_at": "2026-09-04T09:00:00",
                    "files": [],
                    "script_files": [],
                }]
            }).encode())
        elif self.path == "/api/status":
            self._send(b'{"ok":true}')
        else:
            self._send(b'{"error":"not found"}', 404)

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


def main():
    stub = HTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env["AI_TOOL_API_BASE"] = f"http://127.0.0.1:{STUB_PORT}"
    env["AI_TOOL_USER"] = "tester"
    env["AI_TOOL_PASS"] = "dummy"
    env["WEBAPP_WRITE_TOKEN"] = "write-secret"
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

        Stub.hits.clear()
        status, body, ctype = http_call(proxy, "/up/api/cycle/backup")
        data = json.loads(body.decode("utf-8"))
        check("GET backup -> 200 backups shape + JSON",
              status == 200 and isinstance(data.get("backups"), list)
              and "application/json" in (ctype or ""),
              f"status={status} body={body[:200]}")
        reads = [h for h in Stub.hits if h[0] == "GET" and h[1] == "/api/cycle/backup"]
        check("GET backup forwards Basic auth", len(reads) == 1 and reads[0][2].startswith("Basic "))

        Stub.hits.clear()
        for method in ("PUT", "PATCH", "DELETE"):
            status, body, _ = http_call(proxy, "/up/api/cycle/backup", method=method, data=b"{}")
            check(f"{method} backup -> 403 JSON", status == 403 and b'\"error\"' in body,
                  f"got {status}")
        status, body, _ = http_call(proxy, "/up/api/cycle/backup", method="POST", data=b"{}")
        check("POST backup without Bearer -> 401 JSON", status == 401 and b'\"error\"' in body,
              f"got {status}")
        check("blocked backup methods cause zero upstream writes",
              not [h for h in Stub.hits if h[0] != "GET"])

        Stub.hits.clear()
        for raw in ("/up/api/cycle/%2e%2e/status",
                    "/up/api/cycle/backup%00",
                    "/up//api/cycle/backup"):
            status, _, _ = http_call(proxy, raw)
            check(f"traversal {raw} not 2xx", status in (400, 403, 404), f"got {status}")
        bad = [h for h in Stub.hits if h[1] != "/api/cycle/backup"]
        check("traversal never forwards outside exact backup path", not bad, str(bad)[:200])

        # The write gate from Slice 2 remains closed without its own Bearer token.
        Stub.hits.clear()
        status, body, _ = http_call(proxy, "/up/api/log", method="POST", data=b"{}")
        check("Slice 2 log write gate remains enforced", status == 401 and b'\"error\"' in body
              and not [h for h in Stub.hits if h[0] != "GET"])

        # Simulate upstream HTTP failure, then verify proxy remains usable.
        original = Stub.do_GET
        def failing_get(self):
            self._rec()
            self._send(b'{"error":"upstream failure"}', 500)
        Stub.do_GET = failing_get
        status, body, _ = http_call(proxy, "/up/api/cycle/backup")
        check("upstream HTTP error -> status/body preserved", status == 500 and b"upstream failure" in body)
        Stub.do_GET = original
        status, _, _ = http_call(proxy, "/up/api/cycle/backup")
        check("proxy survives upstream error", status == 200)

        # Close the upstream socket to exercise repository connection-refused handling.
        stub.shutdown()
        stub.server_close()
        time.sleep(0.2)
        status, body, _ = http_call(proxy, "/up/api/cycle/backup", timeout=10)
        try:
            error_body = json.loads(body.decode("utf-8"))
        except Exception:
            error_body = {}
        check("upstream unreachable -> 502 JSON error",
              status == 502 and "error" in error_body, f"got {status} body={body[:200]}")
        check("proxy survives upstream unreachable", proc.poll() is None)

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
