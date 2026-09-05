#!/usr/bin/env python3
"""Slice 11 tests for dedicated DELETE /up/api/cycle/backup/<name>."""
import copy
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "webapp", "backend")
PASS = FAIL = 0
TOKEN = "backup-delete-secret"
CANARY = "BACKUP_DELETE_SECRET_CANARY"
VALID_NAME = "cycle_test.zip"


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


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
    names = [VALID_NAME]
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.hits.clear()
        cls.mode = "success"
        cls.names = [VALID_NAME]
        cls.active = 0
        cls.max_active = 0

    def _record(self, body=b""):
        Stub.hits.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "marker": self.headers.get("X-DB-Editor", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
            "at": time.monotonic(),
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
        self._record()
        if self.path == "/api/status":
            self._send(b'{"ok":true}')
            return
        if self.path != "/api/cycle/backup":
            self._send(b'{"error":"not found"}', 404)
            return
        with Stub.active_lock:
            Stub.active += 1
            Stub.max_active = max(Stub.max_active, Stub.active)
        try:
            time.sleep(0.04)
            if Stub.mode == "list_error":
                self._send(b'{"error":"' + CANARY.encode() + b'"}', 500)
            elif Stub.mode == "list_malformed":
                self._send(b'{"backups":')
            elif Stub.mode == "list_non_object":
                self._send(b'[{"name":"' + VALID_NAME.encode() + b'"}]')
            elif Stub.mode == "list_missing_name":
                self._send(b'{"backups":[{"size":1}]}')
            else:
                self._send(json.dumps({"backups": [{"name": name, "size": 1} for name in Stub.names]}, separators=(",", ":")).encode())
        finally:
            with Stub.active_lock:
                Stub.active -= 1

    def do_DELETE(self):
        self._record()
        with Stub.active_lock:
            Stub.active += 1
            Stub.max_active = max(Stub.max_active, Stub.active)
        try:
            time.sleep(0.04)
            if self.path != "/api/cycle/backup/" + VALID_NAME:
                self._send(b'{"error":"not found"}', 404)
            elif Stub.mode == "delete_error":
                self._send(b'{"error":"' + CANARY.encode() + b'"}', 500)
            elif Stub.mode == "delete_malformed":
                self._send(b'{"status":"OK"')
            elif Stub.mode == "delete_non_object":
                self._send(b'[{"status":"OK","deleted":"' + VALID_NAME.encode() + b'"}]')
            elif Stub.mode == "delete_wrong_name":
                self._send(b'{"status":"OK","deleted":"other.zip"}')
            elif Stub.mode == "delete_wrong_status":
                self._send(b'{"status":"DELETED","deleted":"' + VALID_NAME.encode() + b'"}')
            else:
                self._send(json.dumps({"status": "OK", "deleted": VALID_NAME}, separators=(",", ":")).encode())
        finally:
            with Stub.active_lock:
                Stub.active -= 1

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self._record(self.rfile.read(length) if length else b"")
        self._send(b'{"error":"not found"}', 404)

    do_PUT = do_POST
    do_PATCH = do_POST

    def log_message(self, *args):
        pass


def call(base, path, method="GET", data=None, headers=None, timeout=20):
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
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


def upstream_hits(method=None):
    return [hit for hit in Stub.hits if method is None or hit["method"] == method]


def main():
    proxy_port, stub_port = free_port(), free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", stub_port), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env.update(AI_TOOL_API_BASE=f"http://127.0.0.1:{stub_port}", AI_TOOL_USER="tester",
               AI_TOOL_PASS="dummy", WEBAPP_WRITE_TOKEN=TOKEN, WEBAPP_PORT=str(proxy_port))
    proc = subprocess.Popen([sys.executable, os.path.join(BACKEND, "proxy.py")],
                            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proxy = f"http://127.0.0.1:{proxy_port}"
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
            Stub.reset()
            status, raw, response_headers = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=headers)
            challenge = response_headers.get("WWW-Authenticate") or response_headers.get("Www-Authenticate")
            check(label + " -> 401 + Bearer challenge + zero upstream",
                  status == 401 and challenge == "Bearer" and body_json(raw) == {"error": "write authentication required"}
                  and not upstream_hits(), raw.decode(errors="replace"))

        Stub.reset()
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer)
        deletes = upstream_hits("DELETE")
        gets = upstream_hits("GET")
        check("valid delete -> exact 200 JSON contract", status == 200
              and body_json(raw) == {"status": "OK", "deleted": VALID_NAME})
        check("valid delete uses fresh list then one dedicated DELETE",
              len(gets) == 1 and len(deletes) == 1 and gets[0]["path"] == "/api/cycle/backup"
              and deletes[0]["path"] == "/api/cycle/backup/" + VALID_NAME)
        check("delete forwards Basic auth and editor marker", deletes[0]["auth"].startswith("Basic ")
              and deletes[0]["marker"] == "1" and deletes[0]["body"] == b"")

        Stub.reset()
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME + "?ignored=1",
                              "DELETE", data=b'{"' + CANARY.encode() + b'":true}',
                              headers=dict(bearer, **{"Content-Type": "application/json"}))
        deletes = upstream_hits("DELETE")
        check("query/body compatibility -> same success", status == 200
              and body_json(raw) == {"status": "OK", "deleted": VALID_NAME})
        check("query/body are not used or forwarded", len(deletes) == 1
              and deletes[0]["path"] == "/api/cycle/backup/" + VALID_NAME
              and deletes[0]["body"] == b"" and CANARY not in deletes[0]["path"])

        Stub.reset()
        Stub.names = ["other.zip"]
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer)
        check("unknown fresh backup -> generic 409 and zero DELETE", status == 409
              and body_json(raw) == {"error": "backup target unavailable"}
              and not upstream_hits("DELETE"))

        Stub.reset()
        Stub.names = [VALID_NAME, VALID_NAME]
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer)
        check("ambiguous fresh backup -> generic 409 and zero DELETE", status == 409
              and body_json(raw) == {"error": "backup target unavailable"}
              and not upstream_hits("DELETE"))

        invalid_400 = ("..", "%2e%2e", "bad%", "bad%252e%252e", "bad%00name.zip",
                       "bad name.zip", "bad%FFname.zip")
        for name in invalid_400:
            Stub.reset()
            status, raw, _ = call(proxy, "/up/api/cycle/backup/" + name, "DELETE", headers=bearer)
            check("invalid raw/name " + name + " -> 400 zero upstream DELETE",
                  status == 400 and body_json(raw) == {"error": "invalid backup name"}
                  and not upstream_hits(), raw.decode(errors="replace"))

        for name in ("bad%2Fname.zip", "bad%5Cname.zip"):
            Stub.reset()
            status, raw, _ = call(proxy, "/up/api/cycle/backup/" + name, "DELETE", headers=bearer)
            check("encoded separator " + name + " -> blocked zero upstream DELETE",
                  status in (400, 403, 404) and not upstream_hits(), str(status))

        Stub.reset()
        for path in ("/up/api/cycle/backup", "/up/api/cycle/backup/" + VALID_NAME + "/restore"):
            status, raw, _ = call(proxy, path, "DELETE", headers=bearer)
            check("non-dedicated DELETE path -> 403 JSON", status == 403 and body_json(raw) == {"error": "read-only proxy: write methods blocked"})
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "POST", b"{}", bearer)
        check("dedicated backup path POST remains blocked", status == 403 and body_json(raw) == {"error": "read-only proxy: write methods blocked"})
        check("blocked paths cause zero upstream calls", not upstream_hits())

        for mode, label in (("delete_error", "upstream 500"), ("delete_malformed", "malformed success"),
                            ("delete_non_object", "non-object success"), ("delete_wrong_name", "wrong deleted name"),
                            ("delete_wrong_status", "wrong success status")):
            Stub.reset()
            Stub.mode = mode
            status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer)
            check(label + " -> exact sanitized 502", status == 502
                  and body_json(raw) == {"error": "backup deletion unavailable"}
                  and CANARY not in raw.decode(errors="replace"), raw.decode(errors="replace"))
        Stub.reset()
        Stub.mode = "list_error"
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer)
        check("fresh list failure -> exact sanitized 502 and zero DELETE", status == 502
              and body_json(raw) == {"error": "backup deletion unavailable"}
              and not upstream_hits("DELETE"))
        check("proxy survives delete failures", call(proxy, "/up/api/status")[0] == 200)

        Stub.reset()
        results = []
        def delete_once():
            results.append(call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer))
        first = threading.Thread(target=delete_once)
        second = threading.Thread(target=delete_once)
        first.start(); second.start(); first.join(10); second.join(10)
        check("concurrent deletes both complete", len(results) == 2 and all(result[0] == 200 for result in results))
        check("complete delete transactions serialize upstream", Stub.max_active == 1, f"max_active={Stub.max_active}")
        check("concurrent delete calls remain GET plus DELETE only",
              not [hit for hit in Stub.hits if hit["method"] not in ("GET", "DELETE")])

        with open(os.path.join(BACKEND, "app.py"), encoding="utf-8") as source_file:
            app_source = source_file.read()
        check("DELETE has dedicated route boundary",
              '@app.route("/up/api/cycle/backup/<name>", methods=["DELETE"])' in app_source
              and "delete_cycle_backup" in app_source and "startswith" not in app_source)
        with open(os.path.join(BACKEND, "repositories", "aitool.py"), encoding="utf-8") as source_file:
            repository_source = source_file.read()
        check("repository exposes only named backup DELETE",
              "def delete_backup(self, name" in repository_source
              and "method=\"DELETE\"" in repository_source and "def delete(self" not in repository_source)
        with open(os.path.join(BACKEND, "services", "backup.py"), encoding="utf-8") as source_file:
            service_source = source_file.read()
        check("delete service uses fresh membership and transaction lock",
              "_BACKUP_LOCK" in service_source and "_fresh_backup_names" in service_source
              and "names.count(name) != 1" in service_source)

        stub.shutdown(); stub.server_close(); time.sleep(0.2)
        status, raw, _ = call(proxy, "/up/api/cycle/backup/" + VALID_NAME, "DELETE", headers=bearer, timeout=10)
        check("upstream unreachable -> exact sanitized 502", status == 502
              and body_json(raw) == {"error": "backup deletion unavailable"})
        check("proxy survives upstream outage", proc.poll() is None)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        try:
            proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill(); proc.wait(timeout=5)
            except Exception: pass
        try:
            stub.shutdown(); stub.server_close()
        except Exception: pass


if __name__ == "__main__":
    sys.exit(main())
