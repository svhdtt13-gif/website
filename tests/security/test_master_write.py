#!/usr/bin/env python3
"""Slice 8 security/contract tests for POST /up/api/master CAS rename."""
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


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PROXY_PORT, STUB_PORT = free_port(), free_port()
TOKEN = "write-secret"
CANARY = "MASTER_SECRET_CANARY"
INITIAL_MASTER = {
    "clients": [
        {"client": "client_a", "name": "Alpha", "group": "group_a", "selected": True, "slot": "00:00"},
        {"client": "client_b", "name": "Beta", "group": "group_b", "selected": True, "status": "offline"},
    ],
    "schedule": [{"group": "group_a", "time": "00:00", "close": "12:00", "secret": CANARY}],
}


def clean_master():
    return {
        "clients": [
            {"client": "client_a", "name": "Alpha", "group": "group_a", "selected": True},
            {"client": "client_b", "name": "Beta", "group": "group_b", "selected": True},
        ],
        "schedule": [{"group": "group_a", "time": "00:00", "close": "12:00"}],
    }


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
    master = clean_master()
    mode = "success"
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.hits.clear()
        cls.master = clean_master()
        cls.mode = "success"
        cls.active = 0
        cls.max_active = 0

    def _rec(self, body=b""):
        Stub.hits.append({
            "method": self.command,
            "path": self.path,
            "auth": self.headers.get("Authorization", ""),
            "marker": self.headers.get("X-DB-Editor", ""),
            "content_type": self.headers.get("Content-Type", ""),
            "body": body,
            "at": time.monotonic(),
        })

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
        elif self.path == "/api/master":
            self._send(json.dumps(Stub.master, separators=(",", ":")).encode())
        else:
            self._send(b'{"error":"not found"}', 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self._rec(body)
        with Stub.active_lock:
            Stub.active += 1
            Stub.max_active = max(Stub.max_active, Stub.active)
        try:
            time.sleep(0.05)
            if self.path != "/api/master":
                self._send(b'{"error":"not found"}', 404)
            elif Stub.mode == "error":
                self._send((b'{"error":"upstream failure","token":"' + CANARY.encode()
                            + b'"}'), 500)
            elif Stub.mode == "malformed":
                self._send(b'{"status":"OK"')
            elif Stub.mode == "non_object":
                self._send(b'[{"status":"OK"}]')
            elif Stub.mode == "missing_counters":
                self._send(b'{"status":"OK"}')
            elif Stub.mode == "bool_clients":
                self._send(b'{"status":"OK","clients":true,"schedule":1}')
            elif Stub.mode == "float_clients":
                self._send(b'{"status":"OK","clients":1.0,"schedule":1}')
            elif Stub.mode == "string_schedule":
                self._send(b'{"status":"OK","clients":2,"schedule":"1"}')
            elif Stub.mode == "negative":
                self._send(b'{"status":"OK","clients":-1,"schedule":1}')
            else:
                try:
                    posted = json.loads(body.decode("utf-8"))
                    Stub.master = {
                        "clients": posted["clients"],
                        "schedule": posted["schedule"],
                    }
                except Exception:
                    self._send(b'{"error":"invalid upstream payload"}', 400)
                    return
                self._send(json.dumps({
                    "status": "OK",
                    "clients": len(Stub.master["clients"]),
                    "schedule": len(Stub.master["schedule"]),
                    "secret": CANARY,
                }, separators=(",", ":")).encode())
        finally:
            with Stub.active_lock:
                Stub.active -= 1

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


def body_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def posts():
    return [hit for hit in Stub.hits if hit["method"] == "POST" and hit["path"] == "/api/master"]


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env.update(AI_TOOL_API_BASE=f"http://127.0.0.1:{STUB_PORT}", AI_TOOL_USER="tester",
               AI_TOOL_PASS="dummy", WEBAPP_WRITE_TOKEN=TOKEN, WEBAPP_PORT=str(PROXY_PORT))
    proc = subprocess.Popen([sys.executable, os.path.join(BACKEND, "proxy.py")],
                            cwd=BACKEND, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    bearer = {"Authorization": "Bearer " + TOKEN}
    json_bearer = dict(bearer, **{"Content-Type": "application/json"})
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
            status, raw, response_headers = call(proxy, "/up/api/master", "POST",
                                                   b'{"changes":[]}', headers)
            check(label + " -> 401 + WWW-Authenticate + zero upstream writes",
                  status == 401
                  and (response_headers.get("WWW-Authenticate") or response_headers.get("Www-Authenticate")) == "Bearer"
                  and body_json(raw) == {"error": "write authentication required"}
                  and not posts())

        Stub.reset()
        rename = b'{"changes":[{"client":"client_a","expected_name":"Alpha","name":"Alpha-new"}]}'
        status, raw, _ = call(proxy, "/up/api/master", "POST", rename,
                              dict(bearer, **{"X-DB-Editor": "forged", "Content-Type": "application/json; charset=utf-8"}))
        response = body_json(raw)
        check("valid CAS rename -> safe success", status == 200
              and response == {"status": "OK", "clients": 2, "schedule": 1}
              and CANARY not in raw.decode(errors="replace"))
        sent = posts()
        sent_body = body_json(sent[0]["body"]) if len(sent) == 1 else None
        check("canonical payload comes from fresh snapshot",
              len(sent) == 1
              and sent[0]["auth"].startswith("Basic ") and sent[0]["marker"] == "1"
              and sent[0]["content_type"] == "application/json"
              and sent_body == {"clients": [
                  {"client": "client_a", "name": "Alpha-new", "group": "group_a", "selected": True},
                  {"client": "client_b", "name": "Beta", "group": "group_b", "selected": True},
              ], "schedule": [{"group": "group_a", "time": "00:00", "close": "12:00"}]})
        check("raw full collectMaster and slot fields are not forwarded", "expected_name" not in sent[0]["body"].decode()
              and "slot" not in sent[0]["body"].decode() and "secret" not in sent[0]["body"].decode())

        for content_type in ("text/plain", "application/json; charset=latin1",
                             "application/json; foo=bar", "application/json;charset=utf-8"):
            Stub.reset()
            status, raw, _ = call(proxy, "/up/api/master", "POST", rename,
                                  dict(bearer, **{"Content-Type": content_type}))
            check(content_type + " -> 400 and zero upstream writes",
                  status == 400 and body_json(raw) == {"error": "invalid master rename request"} and not posts())

        invalid = [
            ("empty body", b""), ("array", b"[]"), ("empty changes", b'{"changes":[]}'),
            ("full collectMaster", b'{"clients":[],"schedule":[]}'),
            ("missing expected_name", b'{"changes":[{"client":"client_a","name":"New"}]}'),
            ("unknown field", b'{"changes":[{"client":"client_a","expected_name":"Alpha","name":"New","group":"x"}]}'),
            ("duplicate client", b'{"changes":[{"client":"client_a","expected_name":"Alpha","name":"A1"},{"client":"client_a","expected_name":"Alpha","name":"A2"}]}'),
            ("unknown client", b'{"changes":[{"client":"missing","expected_name":"Old","name":"New"}]}'),
            ("name empty", b'{"changes":[{"client":"client_a","expected_name":"Alpha","name":""}]}'),
            ("name whitespace", b'{"changes":[{"client":"client_a","expected_name":"Alpha","name":"   "}]}'),
            ("name control", b'{"changes":[{"client":"client_a","expected_name":"Alpha","name":"bad\\nname"}]}'),
            ("name overlength", json.dumps({"changes":[{"client":"client_a","expected_name":"Alpha","name":"x" * 201}]}).encode()),
            ("wrong expected type", b'{"changes":[{"client":"client_a","expected_name":true,"name":"New"}]}'),
        ]
        for label, payload in invalid:
            Stub.reset()
            status, raw, _ = call(proxy, "/up/api/master", "POST", payload, json_bearer)
            check(label + " -> 400, no echo, zero upstream writes",
                  status == 400 and body_json(raw) == {"error": "invalid master rename request"}
                  and not any(token in raw.decode(errors="replace") for token in (CANARY, "Alpha", "New"))
                  and not posts(), raw.decode(errors="replace"))

        Stub.reset()
        Stub.master["clients"][0]["name"] = "AlreadyChanged"
        status, raw, _ = call(proxy, "/up/api/master", "POST", rename, json_bearer)
        check("CAS mismatch -> 409 zero upstream POST", status == 409
              and body_json(raw) == {"error": "master rename conflict"} and not posts())

        for mode, label in (("malformed", "malformed success"), ("non_object", "non-object success"),
                            ("missing_counters", "missing counters"), ("bool_clients", "boolean clients counter"),
                            ("float_clients", "float clients counter"), ("string_schedule", "string schedule counter"),
                            ("negative", "negative counter")):
            Stub.reset()
            Stub.mode = mode
            status, raw, _ = call(proxy, "/up/api/master", "POST", rename, json_bearer)
            check(label + " -> generic 502 no raw echo/no retry",
                  status == 502 and body_json(raw) == {"error": "master upstream response unavailable"}
                  and len(posts()) == 1 and CANARY not in raw.decode(errors="replace"))

        Stub.reset()
        Stub.mode = "error"
        status, raw, _ = call(proxy, "/up/api/master", "POST", rename, json_bearer)
        check("upstream error sanitized", status == 500
              and body_json(raw) == {"error": "master upstream response unavailable"}
              and CANARY not in raw.decode(errors="replace"))
        Stub.mode = "success"
        check("proxy survives master write failures", call(proxy, "/up/api/status")[0] == 200)

        Stub.reset()
        results = []
        def send(client, expected, name):
            payload = json.dumps({"changes": [{"client": client, "expected_name": expected, "name": name}]}).encode()
            results.append(call(proxy, "/up/api/master", "POST", payload, json_bearer))
        first = threading.Thread(target=send, args=("client_a", "Alpha", "Alpha-a"))
        second = threading.Thread(target=send, args=("client_b", "Beta", "Beta-b"))
        first.start(); second.start(); first.join(10); second.join(10)
        check("concurrent different-client renames both succeed", len(results) == 2 and all(result[0] == 200 for result in results))
        check("different-client renames preserve both names",
              Stub.master["clients"][0]["name"] == "Alpha-a" and Stub.master["clients"][1]["name"] == "Beta-b")
        check("master upstream writes never overlap", Stub.max_active == 1, f"max_active={Stub.max_active}")

        Stub.reset()
        results = []
        first = threading.Thread(target=send, args=("client_a", "Alpha", "Alpha-a"))
        second = threading.Thread(target=send, args=("client_a", "Alpha", "Alpha-b"))
        first.start(); second.start(); first.join(10); second.join(10)
        check("concurrent same-client CAS has one success and one conflict",
              sorted(result[0] for result in results) == [200, 409] and len(posts()) == 1)
        check("same-client CAS keeps the successful name", Stub.master["clients"][0]["name"] in ("Alpha-a", "Alpha-b"))
        check("proxy survives concurrent CAS", call(proxy, "/up/api/status")[0] == 200)

        Stub.reset()
        for method in ("PUT", "PATCH", "DELETE"):
            status, raw, _ = call(proxy, "/up/api/master", method, b"{}", bearer)
            check(method + " master -> 403 JSON", status == 403 and b'"error"' in raw)
        for path in ("/up/api/master/restore", "/up/api/master/client_a"):
            status, raw, _ = call(proxy, path, "POST", rename, bearer)
            check("master subpath -> 403 JSON", status == 403 and b'"error"' in raw)
        check("blocked master writes cause zero upstream writes", not posts())

        with open(os.path.join(BACKEND, "services", "master.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        check("master service has no direct file access", "open(" not in source)
        check("master service locks full CAS transaction",
              "_MASTER_WRITE_LOCK" in source and "_load_fresh_master()" in source
              and "ai_tool.post(\"api/master\"" in source)
        check("master response uses exact integer counter validation",
              "type(clients) is not int" in source and "type(schedule) is not int" in source)

        stub.shutdown(); stub.server_close(); time.sleep(0.2)
        status, raw, _ = call(proxy, "/up/api/master", "POST", rename, json_bearer)
        check("upstream unreachable -> generic 502", status == 502
              and body_json(raw) == {"error": "master upstream response unavailable"})
        check("proxy survives master outage", proc.poll() is None)
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
