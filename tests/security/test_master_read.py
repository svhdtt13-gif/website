#!/usr/bin/env python3
"""Slice 4 contract/security test cho GET /up/api/master.

Tu dung upstream stub + proxy subprocess; khong dung ai tool that, khong ghi file.
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
    master_body = b'{"source":"api-master","clients":[{"client":"api-1"}],"schedule":[],"meta":{"source":"stub"}}'
    file_body = b'{"source":"clients-master-file","clients":[{"client":"file-1"}],"schedule":[]}'

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
        if self.path == "/api/master":
            self._send(Stub.master_body)
        elif self.path == "/clients_master.json":
            self._send(Stub.file_body)
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
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
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
        status, body, ctype = http_call(proxy, "/up/api/master")
        check("GET api/master -> exact API body + JSON",
              status == 200 and body == Stub.master_body
              and "application/json" in (ctype or ""),
              f"status={status} body={body[:200]}")
        status, body, _ = http_call(proxy, "/up/clients_master.json")
        check("GET clients_master.json -> legacy exact body",
              status == 200 and body == Stub.file_body,
              f"status={status} body={body[:200]}")
        paths = [h[1] for h in Stub.hits if h[0] == "GET"]
        check("two routes hit two distinct upstream paths",
              "/api/master" in paths and "/clients_master.json" in paths)
        auth_ok = all(h[2].startswith("Basic ") for h in Stub.hits if h[0] == "GET")
        check("both master reads forward Basic auth", auth_ok)

        Stub.hits.clear()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, body, _ = http_call(proxy, "/up/api/master", method=method, data=b"{}")
            expected = 401 if method == "POST" else 403
            check(f"{method} api/master -> {expected} JSON",
                  status == expected and b'"error"' in body,
                  f"got {status}")
        check("master write methods cause zero upstream writes",
              not [h for h in Stub.hits if h[0] != "GET"])

        # Simulate upstream HTTP 500, then connection refusal on the same read route.
        original = Stub.do_GET
        def failing_get(self):
            self._rec()
            self._send(b'{"error":"upstream failure"}', 500)
        Stub.do_GET = failing_get
        status, body, _ = http_call(proxy, "/up/api/master")
        check("upstream HTTP error -> status/body preserved",
              status == 500 and b"upstream failure" in body)
        Stub.do_GET = original
        status, _, _ = http_call(proxy, "/up/api/master")
        check("proxy survives upstream HTTP error", status == 200)
        stub.shutdown()
        stub.server_close()
        time.sleep(0.2)
        status, body, _ = http_call(proxy, "/up/api/master", timeout=10)
        try:
            error_body = json.loads(body.decode("utf-8"))
        except Exception:
            error_body = {}
        check("upstream unreachable -> 502 JSON error",
              status == 502 and "error" in error_body, f"got {status} body={body[:200]}")
        check("proxy survives upstream unreachable", proc.poll() is None)

        source_path = os.path.join(BACKEND_DIR, "services", "master.py")
        with open(source_path, encoding="utf-8") as source_file:
            source = source_file.read()
        check("master service has separate upstream mappings",
              'get_master()' in source and 'clients_master.json' in source
              and 'get_api_master()' in source and 'api/master' in source)
        check("master service has no direct file access", "open(" not in source)
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
