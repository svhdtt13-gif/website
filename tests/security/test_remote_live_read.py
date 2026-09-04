#!/usr/bin/env python3
"""Slice 9 contract/security tests for canonical GET /up/api/remote_live."""
import copy
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
PASS = FAIL = 0
PROXY_PORT = STUB_PORT = 0
TOKEN_CANARY = "REMOTE_SECRET_CANARY"
VALID_SOURCE = {
    "ok": True,
    "fetchedAt": "2026-09-04T14:00:00+00:00",
    "room": "room-internal",
    "roster": {"token": TOKEN_CANARY},
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
    mode = "valid"
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.hits.clear()
        cls.mode = "valid"
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
        if self.path == "/api/status":
            self._send(b'{"ok":true}')
            return
        if self.path != "/api/remote_live":
            self._send(b'{"error":"not found"}', 404)
            return
        with Stub.active_lock:
            Stub.active += 1
            Stub.max_active = max(Stub.max_active, Stub.active)
        try:
            time.sleep(0.05)
            if Stub.mode == "error":
                self._send((b'{"error":"upstream failure","token":"' + TOKEN_CANARY.encode()
                            + b'"}'), 503)
                return
            source = copy.deepcopy(VALID_SOURCE)
            if Stub.mode == "extra_top":
                source["unexpected"] = TOKEN_CANARY
            elif Stub.mode == "missing_room":
                source.pop("room")
            elif Stub.mode == "missing_roster":
                source.pop("roster")
            elif Stub.mode == "bad_room":
                source["room"] = 1
            elif Stub.mode == "bad_roster":
                source["roster"] = "not-an-object"
            elif Stub.mode == "extra_client":
                source["clients"][0]["password"] = TOKEN_CANARY
            elif Stub.mode == "missing_client_field":
                source["clients"][0].pop("name")
            elif Stub.mode == "extra_resource":
                source["clients"][0]["resources"]["secret"] = TOKEN_CANARY
            elif Stub.mode == "extra_task":
                source["tasks"][0]["secret"] = TOKEN_CANARY
            elif Stub.mode == "extra_log":
                source["logs"][0]["secret"] = TOKEN_CANARY
            elif Stub.mode == "bad_ok":
                source["ok"] = 1
            elif Stub.mode == "bad_timestamp":
                source["fetchedAt"] = "not-a-timestamp"
            elif Stub.mode == "count_string":
                source["clientCount"] = "2"
            elif Stub.mode == "count_bool":
                source["clientCount"] = True
            elif Stub.mode == "count_mismatch":
                source["clientCount"] = 1
            elif Stub.mode == "selected_string":
                source["selectedClientIdx"] = "0"
            elif Stub.mode == "selected_unknown":
                source["selectedClientIdx"] = 9
            elif Stub.mode == "client_idx_bool":
                source["clients"][0]["idx"] = False
            elif Stub.mode == "duplicate_client_idx":
                source["clients"][1]["idx"] = 0
            elif Stub.mode == "cap_age_float":
                source["clients"][0]["capAge"] = 1.0
            elif Stub.mode == "checked_number":
                source["clients"][0]["checked"] = 0
            elif Stub.mode == "task_idx_bool":
                source["tasks"][0]["idx"] = True
            elif Stub.mode == "invalid_resource":
                source["clients"][0]["resources"]["bac"] = 2
            elif Stub.mode == "selected_null":
                source["selectedClientIdx"] = None
            elif Stub.mode == "malformed":
                self._send(b'{"ok":true')
                return
            elif Stub.mode == "non_object":
                self._send(b'[{"ok":true}]')
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
    return [hit for hit in Stub.hits if hit["path"] == "/api/remote_live"]


def body_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def main():
    global PROXY_PORT, STUB_PORT
    PROXY_PORT, STUB_PORT = free_port(), free_port()
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
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
            status, _, _ = call(proxy, "/up/api/status")
            if status == 200:
                ready = True
                break
        check("proxy boots", ready)
        if not ready:
            return 1

        for query, expected, body in (("?client=client-a", 401, {"error": "write authentication required"}),
                                      ("?t=123", 400, {"error": "invalid remote selector query"}),
                                      ("?anything=1", 400, {"error": "invalid remote selector query"})):
            Stub.reset()
            status, raw, response_headers = call(proxy, "/up/api/remote_live" + query)
            challenge_ok = expected != 401 or (response_headers.get("WWW-Authenticate") or response_headers.get("Www-Authenticate")) == "Bearer"
            check("query " + query + " -> boundary guard/auth and zero upstream",
                  status == expected and body_json(raw) == body and challenge_ok and not remote_hits())

        Stub.reset()
        status, raw, _ = call(proxy, "/up/api/remote_live")
        response = body_json(raw)
        check("canonical GET -> projected safe snapshot", status == 200
              and set(response) == PUBLIC_KEYS and response["ok"] is True
              and response["clientCount"] == 2 and response["selectedClientIdx"] == 0
              and TOKEN_CANARY not in raw.decode(errors="replace"))
        check("positive nested allowlists are preserved", response["clients"][0].keys() == VALID_SOURCE["clients"][0].keys()
              and response["clients"][0]["resources"].keys() == VALID_SOURCE["clients"][0]["resources"].keys()
              and response["tasks"][0].keys() == VALID_SOURCE["tasks"][0].keys()
              and response["logs"][0].keys() == VALID_SOURCE["logs"][0].keys())
        check("remote live forwards Basic auth", len(remote_hits()) == 1 and remote_hits()[0]["auth"].startswith("Basic "))

        Stub.reset()
        Stub.mode = "selected_null"
        status, raw, _ = call(proxy, "/up/api/remote_live")
        check("selectedClientIdx null is accepted", status == 200 and body_json(raw)["selectedClientIdx"] is None)

        invalid_modes = [
            ("extra_top", "extra top-level field"), ("missing_room", "missing upstream room"),
            ("missing_roster", "missing upstream roster"), ("bad_room", "invalid upstream room"),
            ("bad_roster", "invalid upstream roster"), ("extra_client", "extra client field"),
            ("missing_client_field", "missing client field"), ("extra_resource", "extra resource field"),
            ("extra_task", "extra task field"), ("extra_log", "extra log field"),
            ("bad_ok", "non-boolean ok"), ("bad_timestamp", "invalid fetchedAt"),
            ("count_string", "string clientCount"), ("count_bool", "boolean clientCount"),
            ("count_mismatch", "inconsistent clientCount"), ("selected_string", "string selected index"),
            ("selected_unknown", "out-of-range selected index"), ("client_idx_bool", "boolean client index"),
            ("duplicate_client_idx", "duplicate client index"), ("cap_age_float", "float capAge"),
            ("checked_number", "numeric checked"), ("task_idx_bool", "boolean task index"),
            ("invalid_resource", "numeric resource"), ("malformed", "malformed JSON"),
            ("non_object", "non-object JSON"),
        ]
        for mode, label in invalid_modes:
            Stub.reset()
            Stub.mode = mode
            status, raw, _ = call(proxy, "/up/api/remote_live")
            check(label + " -> generic 502, no raw echo",
                  status == 502 and body_json(raw) == {"error": "remote live snapshot unavailable"}
                  and len(remote_hits()) == 1 and TOKEN_CANARY not in raw.decode(errors="replace"),
                  raw.decode(errors="replace"))

        Stub.reset()
        Stub.mode = "error"
        status, raw, _ = call(proxy, "/up/api/remote_live")
        check("upstream 4xx/5xx -> generic 502", status == 502
              and body_json(raw) == {"error": "remote live snapshot unavailable"}
              and TOKEN_CANARY not in raw.decode(errors="replace"))
        check("proxy survives upstream failure", call(proxy, "/up/api/status")[0] == 200)

        Stub.reset()
        results = []
        def read_snapshot():
            results.append(call(proxy, "/up/api/remote_live"))
        first = threading.Thread(target=read_snapshot)
        second = threading.Thread(target=read_snapshot)
        first.start(); second.start(); first.join(10); second.join(10)
        check("concurrent remote reads both succeed", len(results) == 2 and all(result[0] == 200 for result in results))
        check("remote live reads serialize at upstream", Stub.max_active == 1, f"max_active={Stub.max_active}")
        check("concurrent reads do not create writes", not [hit for hit in Stub.hits if hit["method"] != "GET"])

        Stub.reset()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            status, raw, _ = call(proxy, "/up/api/remote_live", method, b"{}")
            check(method + " remote live -> 403 JSON", status == 403 and b'"error"' in raw)
        status, raw, _ = call(proxy, "/up/api/remote_live/subpath")
        check("remote live subpath -> 403 JSON", status == 403 and b'"error"' in raw)
        check("blocked remote actions cause zero upstream writes", not [hit for hit in Stub.hits if hit["method"] != "GET"])

        with open(os.path.join(BACKEND, "services", "remote_live.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        check("remote live service has no direct file/subprocess/WebSocket access",
              "open(" not in source and "subprocess" not in source and "WebSocket" not in source)
        check("remote live service has lock and positive allowlists",
              "_REMOTE_LIVE_LOCK" in source and "_CLIENT_FIELDS" in source
              and "_RESOURCE_FIELDS" in source and "_TASK_FIELDS" in source and "_LOG_FIELDS" in source)
        with open(os.path.join(BACKEND, "app.py"), encoding="utf-8") as source_file:
            app_source = source_file.read()
        check("query guard is at route boundary", "request.query_string" in app_source
              and "remote live selector not allowed" in app_source)

        stub.shutdown(); stub.server_close(); time.sleep(0.2)
        status, raw, _ = call(proxy, "/up/api/remote_live")
        check("upstream unreachable -> generic 502", status == 502
              and body_json(raw) == {"error": "remote live snapshot unavailable"})
        check("proxy survives remote live outage", proc.poll() is None)
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