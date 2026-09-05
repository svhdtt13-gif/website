#!/usr/bin/env python3
"""Slice 7 contract/security tests for POST /up/api/settings."""
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
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

PROXY_PORT, STUB_PORT = free_port(), free_port()
TOKEN = "write-secret"
CANARIES = ("BOT_TOKEN_CANARY", "CHAT_ID_CANARY", "PASSWORD_CANARY", "NESTED_SECRET_CANARY")
SOURCE = {
    "telegram_bot_token": CANARIES[0], "telegram_chat_id": CANARIES[1],
    "effective_telegram_bot_token": CANARIES[0], "effective_telegram_chat_id": CANARIES[1],
    "default_browser": r"C:\secret\browser.exe", "cloudflared_path": r"C:\secret\cloudflared.exe",
    "tunnel_port": 18080, "auto_restart_tunnel": True, "auto_telegram": True,
    "auto_open_browser": False, "unknown": {"password": CANARIES[2], "nested": CANARIES[3]},
}
SUCCESS = json.dumps({"status": "OK", "settings": SOURCE}, separators=(",", ":")).encode()
SAFE = {"tunnel_port": 18080, "auto_restart_tunnel": True, "auto_telegram": True, "auto_open_browser": False}
GENERIC = b'{"error":"settings upstream response unavailable"}'


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
    active = 0
    max_active = 0
    active_lock = threading.Lock()

    def _rec(self, body=b""):
        Stub.hits.append({"method": self.command, "path": self.path, "auth": self.headers.get("Authorization", ""), "marker": self.headers.get("X-DB-Editor", ""), "content_type": self.headers.get("Content-Type", ""), "body": body, "at": time.monotonic()})

    def _send(self, body, status=200, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try: self.wfile.write(body)
        except Exception: pass

    def do_GET(self):
        self._rec()
        if self.path == "/api/status": self._send(b'{"ok":true}')
        elif self.path == "/api/settings": self._send(json.dumps(SOURCE).encode())
        else: self._send(b'{"error":"not found"}', 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        self._rec(body)
        with Stub.active_lock:
            Stub.active += 1; Stub.max_active = max(Stub.max_active, Stub.active)
        try:
            time.sleep(0.05)
            if self.path != "/api/settings": self._send(b'{"error":"not found"}', 404)
            elif Stub.mode == "error": self._send(b'{"error":"upstream failure","token":"' + CANARIES[0].encode() + b'"}', 500)
            elif Stub.mode == "malformed": self._send(b'{"status":"OK","settings":' + CANARIES[0].encode())
            elif Stub.mode == "non_object": self._send(b'[{"status":"OK"}]')
            elif Stub.mode == "missing_contract": self._send(b'{"status":"OK"}')
            elif Stub.mode == "corrupt_port":
                source = dict(SOURCE); source["tunnel_port"] = CANARIES[0]; self._send(json.dumps({"status": "OK", "settings": source}).encode())
            elif Stub.mode == "corrupt_bool":
                source = dict(SOURCE); source["auto_telegram"] = {"secret": CANARIES[3]}; self._send(json.dumps({"status": "OK", "settings": source}).encode())
            elif Stub.mode == "missing_safe": self._send(json.dumps({"status": "OK", "settings": {"auto_telegram": False}}).encode())
            else: self._send(SUCCESS)
        finally:
            with Stub.active_lock: Stub.active -= 1

    do_PUT = do_POST
    do_PATCH = do_POST
    do_DELETE = do_POST

    def log_message(self, *args): pass


def call(base, path, method="GET", data=None, headers=None, timeout=20):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.read(), r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        try: body = e.read()
        except Exception: body = b""
        return e.code, body, ""


def call_headers(base, path, method="GET", data=None, headers=None, timeout=20):
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r: return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        try: body = e.read()
        except Exception: body = b""
        return e.code, body, dict(e.headers)


def json_body(raw):
    try: return json.loads(raw.decode())
    except Exception: return None


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    env = dict(os.environ)
    env.update(AI_TOOL_API_BASE=f"http://127.0.0.1:{STUB_PORT}", AI_TOOL_USER="tester", AI_TOOL_PASS="dummy", WEBAPP_WRITE_TOKEN=TOKEN, WEBAPP_PORT=str(PROXY_PORT))
    proc = subprocess.Popen([sys.executable, os.path.join(BACKEND, "proxy.py")], cwd=BACKEND, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proxy = f"http://127.0.0.1:{PROXY_PORT}"
    bearer = {"Authorization": "Bearer " + TOKEN}
    json_bearer = dict(bearer, **{"Content-Type": "application/json"})
    try:
        ready = False
        for _ in range(40):
            time.sleep(0.5)
            if call(proxy, "/up/api/status")[0] == 200: ready = True; break
        check("proxy boots", ready)
        if not ready: return 1
        for label, headers in (("missing Bearer", {}), ("wrong Bearer", {"Authorization": "Bearer wrong"}), ("forged marker only", {"X-DB-Editor": "1"})):
            Stub.hits.clear(); status, body, response_headers = call_headers(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', headers)
            challenge = response_headers.get("WWW-Authenticate") or response_headers.get("Www-Authenticate")
            check(label + " -> 401 + WWW-Authenticate + zero upstream writes", status == 401 and challenge == "Bearer" and b'"error"' in body and not [h for h in Stub.hits if h["method"] != "GET"], f"status={status} headers={response_headers}")
        Stub.mode = "success"; Stub.hits.clear(); payload = b'{"tunnel_port":18081,"auto_telegram":false}'
        headers = dict(bearer, **{"X-DB-Editor": "client-forged", "Content-Type": "application/json; charset=utf-8"})
        status, raw, ctype = call(proxy, "/up/api/settings", "POST", payload, headers); data = json_body(raw); posts = [h for h in Stub.hits if h["method"] == "POST"]
        check("valid partial update -> redacted success", status == 200 and data == {"status": "OK", "settings": SAFE} and "application/json" in ctype and not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))
        check("valid update forwards canonical safe JSON and owned marker", len(posts) == 1 and posts[0]["path"] == "/api/settings" and json.loads(posts[0]["body"]) == {"tunnel_port": 18081, "auto_telegram": False} and posts[0]["content_type"] == "application/json" and posts[0]["auth"].startswith("Basic ") and posts[0]["marker"] == "1")
        Stub.hits.clear(); status, _, _ = call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', dict(bearer, **{"Content-Type": "application/json"})); check("application/json accepted", status == 200)
        for content_type in ("text/plain", "application/json; charset=latin1", "application/json; foo=bar", "application/json;charset=utf-8"):
            Stub.hits.clear(); status, body, _ = call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', dict(bearer, **{"Content-Type": content_type})); check(content_type + " rejected with 400 and zero upstream write", status == 400 and b'"error"' in body and not [h for h in Stub.hits if h["method"] != "GET"], str(status))
        for label, body in (("empty body", b""), ("empty object", b"{}"), ("array", b"[]"), ("malformed", b"{not-json")):
            Stub.hits.clear(); status, raw, _ = call(proxy, "/up/api/settings", "POST", body, json_bearer); check(label + " -> 400 generic + zero upstream write", status == 400 and b'"error"' in raw and not [h for h in Stub.hits if h["method"] != "GET"], str(status))
        invalid_requests = [("secret field", b'{"telegram_bot_token":"BOT_TOKEN_CANARY"}'), ("unknown nested secret", b'{"unknown":{"nested":"NESTED_SECRET_CANARY"}}'), ("bad port type", b'{"tunnel_port":"TOKEN_CANARY"}'), ("bad port low", b'{"tunnel_port":0}'), ("bad port high", b'{"tunnel_port":65536}'), ("bad bool type", b'{"auto_telegram":{"secret":"NESTED_SECRET_CANARY"}}')]
        for label, body in invalid_requests:
            Stub.hits.clear(); status, raw, _ = call(proxy, "/up/api/settings", "POST", body, json_bearer); check(label + " -> 400 no echo + zero upstream write", status == 400 and not any(x.encode() in raw for x in CANARIES) and not [h for h in Stub.hits if h["method"] != "GET"], raw.decode(errors="replace"))
        for mode, label in (("corrupt_port", "corrupt allowlisted port response"), ("corrupt_bool", "corrupt allowlisted bool response"), ("malformed", "malformed response"), ("non_object", "non-object response"), ("missing_contract", "missing response contract")):
            Stub.mode = mode; Stub.hits.clear(); status, raw, _ = call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', json_bearer); posts = [h for h in Stub.hits if h["method"] == "POST"]; check(label + " -> generic 502 no raw echo/no retry", status == 502 and raw == GENERIC and len(posts) == 1 and not any(x.encode() in raw for x in CANARIES), raw.decode(errors="replace"))
        Stub.mode = "missing_safe"; status, raw, _ = call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":false}', json_bearer); check("missing response safe keys stay absent", status == 200 and json_body(raw) == {"status": "OK", "settings": {"auto_telegram": False}})
        Stub.mode = "error"; status, raw, _ = call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', json_bearer); check("upstream 500 settings body sanitized", status == 500 and json_body(raw) == {"error": "settings upstream response unavailable"} and not any(x.encode() in raw for x in CANARIES)); Stub.mode = "success"; check("proxy survives settings write failures", call(proxy, "/up/api/status")[0] == 200)
        Stub.max_active = 0; results = []
        def send(): results.append(call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', json_bearer))
        first = threading.Thread(target=send); second = threading.Thread(target=send); first.start(); second.start(); first.join(10); second.join(10)
        check("concurrent settings writes both succeed", len(results) == 2 and all(r[0] == 200 for r in results)); check("settings upstream writes never overlap", Stub.max_active == 1, f"max_active={Stub.max_active}"); check("proxy survives concurrent settings writes", call(proxy, "/up/api/status")[0] == 200)
        Stub.hits.clear()
        for method in ("PUT", "PATCH", "DELETE"):
            status, raw, _ = call(proxy, "/up/api/settings", method, b"{}", bearer); check(method + " settings -> 403 JSON", status == 403 and b'"error"' in raw)
        for path in ("/up/api/settings/test_telegram", "/up/api/settings/open_browser"):
            status, raw, _ = call(proxy, path, "POST", b"{}", {"Authorization": "Bearer wrong"}); check("settings action wrong Bearer -> 401 JSON", status == 401 and b'"error"' in raw)
        check("blocked settings writes cause zero upstream writes", not [h for h in Stub.hits if h["method"] != "GET"])
        with open(os.path.join(BACKEND, "services", "settings.py"), encoding="utf-8") as f: source = f.read()
        check("GET and POST use shared projector/validator", source.count("_project_public_settings") >= 3 and "def get_settings" in source and "def update_settings" in source); check("settings service has no direct file access", "open(" not in source); check("settings write uses process-local lock and exact MIME set", "_SETTINGS_WRITE_LOCK" in source and "_ALLOWED_JSON_CONTENT_TYPES" in source)
        stub.shutdown(); stub.server_close(); time.sleep(0.2); status, raw, _ = call(proxy, "/up/api/settings", "POST", b'{"auto_telegram":true}', json_bearer); check("upstream unreachable -> 502 generic JSON", status == 502 and json_body(raw) == {"error": "settings upstream response unavailable"}); check("proxy survives settings outage", proc.poll() is None)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed"); return 0 if FAIL == 0 else 1
    finally:
        try: proc.terminate(); proc.wait(timeout=5)
        except Exception:
            try: proc.kill()
            except Exception: pass
        try: stub.shutdown(); stub.server_close()
        except Exception: pass


if __name__ == "__main__": sys.exit(main())
