#!/usr/bin/env python3
"""Slice 2 security/contract tests cho POST /up/api/log (write dau tien).

Tu dung: upstream stub (mimic ai tool POST /api/log) + proxy that (subprocess).
Khong dung ai tool that, khong ghi gi ra ngoai. Exit 0 khi tat ca PASS.
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_DIR = os.path.join(REPO_ROOT, "webapp", "backend")
PROXY_PORT = 18093
STUB_PORT = 18092
PASS_COUNT = 0
FAIL_COUNT = 0


def check(name, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"PASS: {name}")
    else:
        FAIL_COUNT += 1
        print(f"FAIL: {name} {detail}"[:300])


class Stub(BaseHTTPRequestHandler):
    """Mimic toi thieu ai tool: GET ok; POST /api/log can Basic + X-DB-Editor."""
    hits = []

    def _rec(self):
        Stub.hits.append((self.command, self.path,
                          self.headers.get("Authorization", ""),
                          self.headers.get("X-DB-Editor", "")))

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
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
        self._send({"ok": True})

    def do_POST(self):
        self._rec()
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path != "/api/log":
            self._send({"error": "not found"}, 404)
            return
        if self.headers.get("X-DB-Editor") != "1":
            self._send({"error": "missing marker"}, 403)
            return
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            self._send({"error": "bad json"}, 500)
            return
        if not isinstance(data, dict):
            self._send({"error": "bad json"}, 500)
            return
        self._send({"status": "logged"})

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, *a):
        pass


def http_call(base, path, method="GET", data=None, ctype="application/json", timeout=10):
    headers = {"Content-Type": ctype} if data is not None else {}
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
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
    proxy_url = f"http://127.0.0.1:{PROXY_PORT}"
    stub = HTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1},
                     daemon=True).start()
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
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                status, _, _ = http_call(proxy_url, "/up/api/status", timeout=3)
                if status == 200:
                    ready = True
                    break
            except Exception:
                pass
        check("proxy boots", ready)
        if not ready:
            return 1
        # 1. POST hop le -> 200 passthrough, stub thay Basic + X-DB-Editor + dung body
        Stub.hits.clear()
        payload = json.dumps({"action": "phase2-slice2-test",
                              "clients": "c1", "schedule": "s1"}).encode()
        status, body, ctype = http_call(proxy_url, "/up/api/log",
                                        method="POST", data=payload)
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        check("POST valid -> 200 logged passthrough",
              status == 200 and data.get("status") == "logged"
              and "application/json" in (ctype or ""),
              f"status={status} body={body[:200]}")
        fwd = [h for h in Stub.hits if h[0] == "POST" and h[1] == "/api/log"]
        check("upstream got POST with Basic + X-DB-Editor",
              len(fwd) == 1 and fwd[0][2].startswith("Basic ") and fwd[0][3] == "1",
              str(fwd)[:200])
        # 2. JSON sai -> passthrough 500 tu upstream, khong crash
        status, body, _ = http_call(proxy_url, "/up/api/log",
                                    method="POST", data=b"{not-json")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        check("POST malformed -> upstream 500 passthrough",
              status == 500 and "error" in data, f"got {status}")
        # 3. POST path READ khac van 403, upstream zero write ngoai api/log
        Stub.hits.clear()
        status, body, _ = http_call(proxy_url, "/up/api/cycle/status",
                                    method="POST", data=b"{}")
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            data = {}
        writes = [h for h in Stub.hits if h[0] != "GET"]
        check("POST other path -> 403 + zero upstream writes",
              status == 403 and "error" in data and not writes,
              f"got {status} writes={writes}"[:200])
        # 4. PUT/PATCH/DELETE len api/log van 403
        for method in ("PUT", "PATCH", "DELETE"):
            status, _, _ = http_call(proxy_url, "/up/api/log",
                                     method=method, data=b"{}")
            check(f"{method} /up/api/log -> 403", status == 403, f"got {status}")
        # 5. GET /up/api/log van 403 (upstream khong co GET)
        status, _, _ = http_call(proxy_url, "/up/api/log")
        check("GET /up/api/log -> 403", status == 403, f"got {status}")
        # 6. Upstream chet -> 502 JSON on dinh, proxy song
        stub.shutdown()
        time.sleep(0.5)
        status, body, _ = http_call(proxy_url, "/up/api/log",
                                    method="POST", data=payload, timeout=30)
        try:
            data = json.loads(body.decode("utf-8"))
            ok = status == 502 and "error" in data
        except Exception:
            ok = False
        check("upstream down -> 502 json-error", ok, f"got {status}")
        check("proxy survives upstream outage", proc.poll() is None)
        print(f"\nSUMMARY: {PASS_COUNT} passed, {FAIL_COUNT} failed")
        return 0 if FAIL_COUNT == 0 else 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
