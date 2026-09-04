#!/usr/bin/env python3
"""Slice 10 tests for guarded GET /up/api/remote_live selector queries."""
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
TOKEN = "selector-secret"
CANARY = "SELECTOR_SECRET_CANARY"
VALID_SOURCE = {
    "ok": True,
    "fetchedAt": "2026-09-04T14:00:00+00:00",
    "room": "room-internal",
    "roster": {"token": CANARY},
    "clientCount": 2,
    "selectedClientIdx": 0,
    "clients": [
        {"idx": 0, "id": "0:client-a", "name": "Alpha", "state": "online",
         "cap": "ready", "capAge": 10, "uiStatus": "OK", "level": "42",
         "resources": {"nPhieu": "1", "bac": "2", "vang": "3", "ngoc": "4"}, "checked": False},
        {"idx": 1, "id": "0:client-b", "name": "Beta", "state": "offline",
         "cap": "", "capAge": None, "uiStatus": "OFF", "level": "41",
         "resources": {"nPhieu": "5", "bac": "6", "vang": "7", "ngoc": "8"}, "checked": True},
    ],
    "tasks": [{"idx": 0, "name": "Task A", "status": "idle", "checked": False}],
    "logs": [{"text": "remote log", "color": "#fff"}],
}
PUBLIC_KEYS = {"ok", "fetchedAt", "clientCount", "selectedClientIdx", "clients", "tasks", "logs"}


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
    selected = 0
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.hits.clear()
        cls.mode = "success"
        cls.selected = 0
        cls.active = 0
        cls.max_active = 0

    def _rec(self, body=b""):
        Stub.hits.append({"method": self.command, "path": self.path,
                          "auth": self.headers.get("Authorization", ""), "body": body,
                          "at": time.monotonic()})

    def _send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        self._rec()
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == "/api/status":
            self._send(b'{"ok":true}')
            return
        if parsed.path != "/api/remote_live":
            self._send(b'{"error":"not found"}', 404)
            return
        with Stub.active_lock:
            Stub.active += 1
            Stub.max_active = max(Stub.max_active, Stub.active)
        try:
            time.sleep(0.05)
            if Stub.mode == "error":
                self._send((b'{"error":"upstream failure","token":"' + CANARY.encode()
                            + b'"}'), 503)
                return
            source = copy.deepcopy(VALID_SOURCE)
            source["selectedClientIdx"] = Stub.selected
            params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            if "client" in params and params["client"]:
                target = params["client"][0]
                normalized = target[2:] if target.startswith("0:") else target
                for client in source["clients"]:
                    current = client["id"]
                    current = current[2:] if current.startswith("0:") else current
                    if current == normalized:
                        Stub.selected = client["idx"]
                        source["selectedClientIdx"] = client["idx"]
                        break
            if Stub.mode == "malformed":
                self._send(b'{"ok":true')
                return
            self._send(json.dumps(source, ensure_ascii=False, separators=(",", ":")).encode())
        finally:
            with Stub.active_lock:
                Stub.active -= 1

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        self._rec(self.rfile.read(length) if length else b"")
        self._send(b'{"error":"not found"}', 404)

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

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


def remote_hits():
    return [hit for hit in Stub.hits if urllib.parse.urlsplit(hit["path"]).path == "/api/remote_live"]


def body_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


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

        for label, headers in (("missing Bearer", {}), ("wrong Bearer", {"Authorization": "Bearer wrong"})):
            Stub.reset()
            status, raw, response_headers = call(proxy, "/up/api/remote_live?t=1&client=0%3Aclient-b",
                                                   "GET", headers=headers)
            challenge = response_headers.get("WWW-Authenticate") or response_headers.get("Www-Authenticate")
            check(label + " selector -> 401 + challenge + zero upstream",
                  status == 401 and challenge == "Bearer" and not remote_hits(), raw.decode(errors="replace"))

        valid_query = "/up/api/remote_live?client=0%3Aclient-b&t=123"
        Stub.reset()
        status, raw, _ = call(proxy, valid_query, headers=bearer)
        response = body_json(raw)
        hits = remote_hits()
        query_paths = [urllib.parse.urlsplit(hit["path"]).query for hit in hits]
        check("valid selector -> safe projected response", status == 200
              and set(response) == PUBLIC_KEYS and response["selectedClientIdx"] == 1
              and CANARY not in raw.decode(errors="replace"))
        check("selector fresh-read then canonicalizes upstream query",
              len(hits) == 2 and query_paths[0] == "" and query_paths[1] == "t=123&client=0%3Aclient-b"
              and hits[0]["auth"].startswith("Basic ") and hits[1]["auth"].startswith("Basic "))

        Stub.reset()
        status, raw, _ = call(proxy, "/up/api/remote_live?t=123&client=0%3Aclient-a", headers=bearer)
        check("same selected target avoids selector side effect", status == 200
              and len(remote_hits()) == 1 and urllib.parse.urlsplit(remote_hits()[0]["path"]).query == "")

        Stub.reset()
        status, raw, _ = call(proxy, "/up/api/remote_live?t=1&client=0%3Amissing", headers=bearer)
        check("unknown fresh target -> 409 and no selector upstream", status == 409
              and body_json(raw) == {"error": "remote selector target unavailable"}
              and len(remote_hits()) == 1 and urllib.parse.urlsplit(remote_hits()[0]["path"]).query == "")

        invalid_queries = (
            "t=1&client=0%3Aclient-b&client=0%3Aclient-a",
            "t=1&%74=2&client=0%3Aclient-b",
            "t=1&client=0%3Aclient-b&%63lient=0%3Aclient-a",
            "t=1&client=0%3Aclient-b%",
            "t=1&client=0%3Aclient-%FF",
            "t=1+2&client=0%3Aclient-b",
            "t=1&client=0%3Aclient-b&extra=1",
            "t=&client=0%3Aclient-b",
            "client=0%3Aclient-b",
        )
        for query in invalid_queries:
            Stub.reset()
            status, raw, _ = call(proxy, "/up/api/remote_live?" + query, headers=bearer)
            check("invalid raw selector query -> 400 zero upstream: " + query,
                  status == 400 and body_json(raw) == {"error": "invalid remote selector query"}
                  and not remote_hits(), raw.decode(errors="replace"))

        Stub.reset()
        Stub.mode = "error"
        status, raw, _ = call(proxy, "/up/api/remote_live?t=1&client=0%3Aclient-b", headers=bearer)
        check("selector upstream failure -> generic 502", status == 502
              and body_json(raw) == {"error": "remote live snapshot unavailable"})
        Stub.mode = "success"
        check("proxy survives selector failure", call(proxy, "/up/api/status")[0] == 200)

        Stub.reset()
        results = []
        def select(client):
            results.append(call(proxy, "/up/api/remote_live?t=99&client=" + client, headers=bearer))
        first = threading.Thread(target=select, args=("0%3Aclient-a",))
        second = threading.Thread(target=select, args=("0%3Aclient-b",))
        first.start(); second.start(); first.join(10); second.join(10)
        check("concurrent selectors complete", len(results) == 2 and all(result[0] == 200 for result in results))
        check("selector upstream requests serialize", Stub.max_active == 1, f"max_active={Stub.max_active}")
        check("selectors create no non-GET upstream calls", not [hit for hit in Stub.hits if hit["method"] != "GET"])

        Stub.reset()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, raw, _ = call(proxy, "/up/api/remote_live", method, b"{}")
            check(method + " remote selector -> 403 JSON", status == 403 and b'"error"' in raw)
        status, raw, _ = call(proxy, "/up/api/remote_live/subpath")
        check("remote selector subpath -> 403 JSON", status == 403 and b'"error"' in raw)
        check("blocked selector actions cause zero upstream writes", not [hit for hit in Stub.hits if hit["method"] != "GET"])

        with open(os.path.join(BACKEND, "services", "remote_live.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        check("selector service has fresh membership and full lock",
              "_REMOTE_SELECTOR_LOCK" in source and "_find_target" in source
              and "_load_snapshot()" in source and "api/remote_live?" in source)
        check("selector service canonical re-encodes query", "quote(timestamp" in source and "quote(target_client" in source)
        with open(os.path.join(BACKEND, "app.py"), encoding="utf-8") as source_file:
            app_source = source_file.read()
        check("selector parser runs at route boundary", "parse_selector_query(request.query_string)" in app_source
              and "RemoteSelectorQueryError" in app_source)

        stub.shutdown(); stub.server_close(); time.sleep(0.2)
        status, raw, _ = call(proxy, "/up/api/remote_live?t=1&client=0%3Aclient-b", headers=bearer)
        check("selector upstream unreachable -> generic 502", status == 502
              and body_json(raw) == {"error": "remote live snapshot unavailable"})
        check("proxy survives selector outage", proc.poll() is None)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL else 1
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
