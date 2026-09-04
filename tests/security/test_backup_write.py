#!/usr/bin/env python3
"""Slice 6 contract/security tests for POST /up/api/cycle/backup."""
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

PROXY_PORT, STUB_PORT = free_port(), free_port()
TOKEN = "write-secret"
SUCCESS = b'{"status":"OK","backup":"cycle_test.zip","manifest":{"created_at":"2026-09-04T12:00:00","label":"test","files":[],"script_files":[]}}'
GENERIC_ERROR = b'{"error":"invalid backup response"}'


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
    mode = "success"

    def _rec(self, body=b""):
        Stub.hits.append({
            "method": self.command, "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "marker": self.headers.get("X-DB-Editor", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body, "at": time.monotonic(),
        })

    def _send(self, body, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        self._rec()
        if self.path == "/api/status":
            self._send(b'{"ok":true}')
        elif self.path == "/api/cycle/backup":
            self._send(b'{"backups":[]}')
        else:
            self._send(b'{"error":"not found"}', 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self._rec(body)
        if self.path != "/api/cycle/backup":
            self._send(b'{"error":"not found"}', 404)
        elif Stub.mode == "error":
            self._send(b'{"error":"backup upstream failure"}', 500)
        elif Stub.mode == "malformed":
            self._send(b'{"status":"OK","backup":"cycle_test.zip"BOT_TOKEN_CANARY')
        elif Stub.mode == "non_object":
            self._send(b'[{"status":"OK","backup":"cycle_test.zip"}]')
        elif Stub.mode == "missing_contract":
            self._send(b'{"status":"OK","backup":"cycle_test.zip","manifest":{}}')
        else:
            self._send(SUCCESS)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, *args):
        pass


def call(base, path, method="GET", data=None, headers=None, timeout=20):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body, ""


def main():
    stub = HTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env.update(AI_TOOL_API_BASE=f"http://127.0.0.1:{STUB_PORT}", AI_TOOL_USER="tester",
               AI_TOOL_PASS="dummy", WEBAPP_WRITE_TOKEN=TOKEN, WEBAPP_PORT=str(PROXY_PORT))
    proc = subprocess.Popen([sys.executable, os.path.join(BACKEND, "proxy.py")],
                            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    bearer = {"Authorization": "Bearer " + TOKEN}
    try:
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            status, _, _ = call(proxy, "/up/api/status")
            if status == 200:
                ready = True
                break
        check("proxy boots", ready)
        if not ready:
            return 1

        for label, headers in (("missing Bearer", {}), ("wrong Bearer", {"Authorization": "Bearer wrong"}),
                               ("forged marker only", {"X-DB-Editor": "1"})):
            Stub.hits.clear()
            status, body, _ = call(proxy, "/up/api/cycle/backup", "POST", b"{}", headers)
            check(label + " -> 401 JSON + zero upstream writes",
                  status == 401 and b'"error"' in body
                  and not [h for h in Stub.hits if h["method"] != "GET"], str(status))

        Stub.hits.clear()
        payload = b'{"label":"phase2-slice6-json"}'
        ctype = "application/json; charset=utf-8"
        headers = dict(bearer, **{"X-DB-Editor": "client-forged", "Content-Type": ctype})
        status, body, response_type = call(proxy, "/up/api/cycle/backup", "POST", payload, headers)
        posts = [h for h in Stub.hits if h["method"] == "POST"]
        check("valid JSON backup -> exact success contract",
              status == 200 and body == SUCCESS and "application/json" in response_type, body.decode(errors="replace"))
        check("valid JSON forwards exact body/content-type/auth/marker",
              len(posts) == 1 and posts[0]["path"] == "/api/cycle/backup"
              and posts[0]["body"] == payload and posts[0]["content_type"] == ctype
              and posts[0]["auth"].startswith("Basic ") and posts[0]["marker"] == "1")

        Stub.hits.clear()
        form_payload = b"label=phase2-slice6-form"
        status, body, _ = call(proxy, "/up/api/cycle/backup", "POST", form_payload,
                               dict(bearer, **{"Content-Type": "application/x-www-form-urlencoded"}))
        posts = [h for h in Stub.hits if h["method"] == "POST"]
        check("form backup -> success and exact raw body", status == 200 and body == SUCCESS
              and len(posts) == 1 and posts[0]["body"] == form_payload
              and posts[0]["content_type"] == "application/x-www-form-urlencoded")

        # Empty request body must remain b"" at the upstream boundary.
        Stub.hits.clear()
        status, body, _ = call(proxy, "/up/api/cycle/backup", "POST", b"",
                               dict(bearer, **{"Content-Type": "application/json"}))
        posts = [h for h in Stub.hits if h["method"] == "POST"]
        check("empty body -> success without synthesizing JSON", status == 200 and body == SUCCESS
              and len(posts) == 1 and posts[0]["body"] == b"")

        # A 200 success response must be valid JSON object with the full contract.
        for mode, label in (("malformed", "malformed success"),
                            ("non_object", "non-object success"),
                            ("missing_contract", "missing success contract")):
            Stub.mode = mode
            Stub.hits.clear()
            status, body, _ = call(proxy, "/up/api/cycle/backup", "POST", b"{}", bearer)
            posts = [h for h in Stub.hits if h["method"] == "POST"]
            check(label + " -> generic 502 without raw echo",
                  status == 502 and body == GENERIC_ERROR and len(posts) == 1
                  and b"BOT_TOKEN_CANARY" not in body, body.decode(errors="replace"))
        Stub.mode = "success"
        check("proxy survives malformed success responses", call(proxy, "/up/api/status")[0] == 200)

        # Error status/body remains golden for backup writes; proxy survives it.
        Stub.mode = "error"
        status, body, _ = call(proxy, "/up/api/cycle/backup", "POST", b"{}", bearer)
        check("upstream backup error -> status/body preserved", status == 500
              and body == b'{"error":"backup upstream failure"}')
        Stub.mode = "success"
        check("proxy survives backup upstream error", call(proxy, "/up/api/status")[0] == 200)

        # Concurrent valid requests are serialized and spaced by at least one second.
        Stub.hits.clear()
        results = []
        def send(label):
            results.append(call(proxy, "/up/api/cycle/backup", "POST",
                                json.dumps({"label": label}).encode(),
                                dict(bearer, **{"Content-Type": "application/json"})))
        first = threading.Thread(target=send, args=("phase2-slice6-concurrent-a",))
        second = threading.Thread(target=send, args=("phase2-slice6-concurrent-b",))
        first.start(); second.start(); first.join(10); second.join(10)
        posts = [h for h in Stub.hits if h["method"] == "POST"]
        posts.sort(key=lambda hit: hit["at"])
        spacing = posts[1]["at"] - posts[0]["at"] if len(posts) == 2 else 0
        check("concurrent backups both succeed", len(results) == 2 and all(r[0] == 200 for r in results))
        check("concurrent backup upstream calls are spaced >= 1.0s",
              len(posts) == 2 and spacing >= 1.0, f"spacing={spacing}")
        check("concurrent backup bodies remain distinct",
              len(posts) == 2 and posts[0]["body"] != posts[1]["body"])

        status, body, _ = call(proxy, "/up/api/cycle/backup")
        check("GET backup remains available", status == 200 and body == b'{"backups":[]}')
        Stub.hits.clear()
        for method in ("PUT", "PATCH", "DELETE"):
            status, body, _ = call(proxy, "/up/api/cycle/backup", method, b"{}", bearer)
            check(method + " backup -> 403 JSON", status == 403 and b'"error"' in body)
        for path in ("/up/api/cycle/backup/test.zip", "/up/api/cycle/backup/test.zip/restore"):
            status, body, _ = call(proxy, path, "POST", b"{}", bearer)
            check("restore/delete subpath -> 403 JSON", status == 403 and b'"error"' in body)
        check("blocked backup methods cause zero upstream writes",
              not [h for h in Stub.hits if h["method"] != "GET"])

        with open(os.path.join(BACKEND, "services", "backup.py"), encoding="utf-8") as f:
            source = f.read()
        check("backup service has no direct file access", "open(" not in source)
        check("backup service validates success without retry",
              "_validate_success" in source and "_invalid_success" in source
              and "ai_tool.post(\"api/cycle/backup\"" in source)
        stub.shutdown(); stub.server_close(); time.sleep(0.2)
        status, body, _ = call(proxy, "/up/api/cycle/backup", "POST", b"{}", bearer, timeout=10)
        try:
            error = json.loads(body.decode())
        except Exception:
            error = None
        check("upstream unreachable -> 502 JSON error", status == 502 and isinstance(error, dict) and "error" in error)
        check("proxy survives backup upstream outage", proc.poll() is None)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL else 1
    finally:
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try:
            stub.shutdown(); stub.server_close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
