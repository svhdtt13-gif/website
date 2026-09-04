#!/usr/bin/env python3
"""Phase 0 security/contract tests cho webapp/backend/proxy.py.

Tự dựng: upstream stub (local) + proxy thật (subprocess) trên port tạm.
Không dùng ai tool, không cần credentials thật, không ghi gì ra ngoài.
Exit 0 khi tất cả PASS.
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND_DIR = os.path.join(REPO_ROOT, "webapp", "backend")
PROXY_PORT = 18099
STUB_PORT = 18098
ALLOWLIST = {
    "api/cycle/status", "api/cycle_status", "api/sync_status", "api/status",
    "api/ai_fix/status", "clients_master.json", "client_database.json",
}
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
    hits = []

    def _rec(self):
        Stub.hits.append((self.command, self.path,
                          self.headers.get("Authorization", "")))

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
        if self.path == "/api/cycle/status":
            self._send({"cycle_running": True, "manual_overrides": [], "qnyh": 1})
        elif self.path == "/clients_master.json":
            self._send({"clients": [{"client": "client_1"}], "schedule": []})
        else:
            self._send({"ok": True})

    def _write(self):
        self._rec()
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length:
            try:
                self.rfile.read(length)
            except Exception:
                pass
        self._send({"ok": True})

    do_POST = _write
    do_PUT = _write
    do_PATCH = _write
    do_DELETE = _write

    def log_message(self, *a):
        pass


def http_call(base, path, method="GET", timeout=10):
    req = urllib.request.Request(base + path, data=b"" if method != "GET" else None,
                                 method=method)
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
        Stub.hits.clear()
        # 1. Read-only allowlist passthrough
        for path, keys in [("/up/api/cycle/status", ["cycle_running", "manual_overrides"]),
                           ("/up/clients_master.json", ["clients"])]:
            try:
                status, body, ctype = http_call(proxy_url, path)
                data = json.loads(body.decode("utf-8"))
                missing = [k for k in keys if k not in data]
                check(f"GET {path} [200 shape+ctype]",
                      status == 200 and not missing and "application/json" in (ctype or ""),
                      f"status={status} missing={missing} ctype={ctype}")
            except Exception as e:
                check(f"GET {path} [200 shape+ctype]", False, str(e)[:150])
        auth_ok = any(h[2].startswith("Basic ") for h in Stub.hits)
        check("auth forwarded to upstream", auth_ok)
        # 2. Method block on every allowlisted path
        Stub.hits.clear()
        blocked_all = True
        for path in sorted(ALLOWLIST):
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                status, body, _ = http_call(proxy_url, "/up/" + path, method=method)
                try:
                    err = json.loads((body or b"{}").decode("utf-8"))
                    has_err = "error" in err
                except Exception:
                    has_err = False
                if not (status == 403 and has_err):
                    blocked_all = False
                    check(f"{method} /up/{path} -> 403 json-error", False,
                          f"got {status}")
        if blocked_all:
            check("write methods blocked on all allowlist paths (28 calls)", True)
        writes_seen = [h for h in Stub.hits if h[0] != "GET"]
        check("upstream saw zero write requests", not writes_seen, str(writes_seen)[:150])
        # 3. Path traversal: only 403/404, stub never sees outside path
        Stub.hits.clear()
        for raw in ("/up/../api/status", "/up/..%2Fapi/status",
                    "/up/%2e%2e/api/status", "/up/api/%2e%2e/status",
                    "/up/%2E%2E%5C..%5Cwindows/win.ini", "/up//api/status",
                    "/up/api/status%00", "/nope.exe", "/..%2fsecret"):
            try:
                status, _, _ = http_call(proxy_url, raw)
                if status not in (403, 404, 400):
                    check(f"traversal {raw} not 2xx", False, f"got {status}")
            except Exception:
                pass
        bad_hits = []
        for _m, p, _a in Stub.hits:
            pure = p.split("?")[0]
            if ".." in pure or "\\" in pure or "%" in pure.lower():
                bad_hits.append(p)
                continue
            target = pure.lstrip("/")
            if target not in ALLOWLIST:
                bad_hits.append(p)
        check("no traversal reached upstream outside allowlist", not bad_hits,
              str(bad_hits)[:200])
        if not bad_hits:
            check("traversal variants contained", True)
        # 4. Upstream down -> stable 502 JSON, proxy stays alive
        stub.shutdown()
        time.sleep(0.5)
        try:
            status, body, _ = http_call(proxy_url, "/up/api/status")
            try:
                err = json.loads((body or b"{}").decode("utf-8"))
                ok = status == 502 and "error" in err
            except Exception:
                ok = False
            check("upstream down -> 502 json-error", ok, f"got {status}")
        except Exception as e:
            check("upstream down -> 502 json-error", False, str(e)[:150])
        alive = proc.poll() is None
        check("proxy survives upstream outage", alive)
        print(f"\nSUMMARY: {PASS_COUNT} passed, {FAIL_COUNT} failed")
        return 0 if FAIL_COUNT == 0 else 1
    finally:
        try:
            proc.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
