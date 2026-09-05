#!/usr/bin/env python3
"""Bundle 2: guarded AI queue creation and deferred-action checks."""
import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BACKEND = os.path.join(ROOT, "webapp", "backend")
TOKEN = "write-secret"
PASS = FAIL = 0


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


PROXY_PORTS = [free_port(), free_port()]
STUB_PORT = free_port()


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + " " + detail[:300])


COMMANDS = {
    "cycle": "Trong dự án ai tool, hãy kiểm tra và fix lỗi cycle: đọc tools/AutoCycle.ps1, tools/cache/cycle.log, tools/cache/cycle_state.json và tools/client_database.json; kiểm tra trạng thái AutoCycle/sync workers qua /api/cycle/status; sửa lỗi rồi restart worker và xác minh lại.",
    "web": "Trong dự án ai tool, hãy kiểm tra và fix trang web db.html: đọc tools/db.html và WebAppControl/flask/app_public.py; kiểm tra lỗi console trình duyệt và các API /api/settings, /api/cycle/fix; sửa rồi xác minh lại trên trình duyệt.",
}


class Stub(BaseHTTPRequestHandler):
    posts = []
    mode = "success"
    delay = 0.0
    lock = threading.Lock()

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
        if self.path == "/api/status":
            self._send(b'{"ok":true}')
        else:
            self._send(b'{"error":"not found"}', 404)

    def do_POST(self):
        started = time.monotonic()
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else b""
        entry = {"started": started, "path": self.path, "body": body,
                 "auth": self.headers.get("Authorization", ""),
                 "marker": self.headers.get("X-DB-Editor", "")}
        with Stub.lock:
            Stub.posts.append(entry)
        if self.path != "/api/ai_fix":
            self._send(b'{"error":"not found"}', 404)
            return
        if Stub.delay:
            time.sleep(Stub.delay)
        if Stub.mode == "error":
            self._send(b'{"error":"secret upstream detail"}', 500)
            return
        if Stub.mode == "malformed":
            self._send(b"not-json")
            return
        try:
            posted = json.loads(body.decode("utf-8"))
            kind = posted["kind"]
            text = posted.get("text")
        except Exception:
            self._send(b'{"error":"invalid"}', 400)
            return
        command = COMMANDS.get(kind)
        if kind == "userimport":
            command = "[Userimport - dự án ai tool] " + text
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"ai_fix_{kind}_{stamp}.json"
        if Stub.mode == "bad_file":
            filename = r"C:\secret\ai_fix.json"
        response = {"status": "OK", "kind": kind, "project": "ai tool",
                    "file": filename, "command": command}
        entry["completed"] = time.monotonic()
        self._send(json.dumps(response, ensure_ascii=False).encode("utf-8"))

    def log_message(self, *args):
        pass


def call(base, path, method="GET", data=None, headers=None, timeout=20):
    request = urllib.request.Request(base + path, data=data, method=method,
                                     headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, error.read(), dict(error.headers)


def start_proxy(port, env):
    proxy_env = dict(env, WEBAPP_PORT=str(port))
    return subprocess.Popen([sys.executable, os.path.join(BACKEND, "proxy.py")],
                            cwd=BACKEND, env=proxy_env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def wait_ready(base):
    for _ in range(40):
        time.sleep(0.25)
        if call(base, "/up/api/status")[0] == 200:
            return True
    return False


def body_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def main():
    stub = ThreadingHTTPServer(("127.0.0.1", STUB_PORT), Stub)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    env = dict(os.environ)
    env.update(AI_TOOL_API_BASE=f"http://127.0.0.1:{STUB_PORT}", AI_TOOL_USER="tester",
               AI_TOOL_PASS="dummy", WEBAPP_WRITE_TOKEN=TOKEN)
    procs = [start_proxy(port, env) for port in PROXY_PORTS]
    bases = [f"http://127.0.0.1:{port}" for port in PROXY_PORTS]
    bearer = {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}
    try:
        ready = all(wait_ready(base) for base in bases)
        check("two independent proxies boot", ready)
        if not ready:
            return 1

        Stub.posts.clear()
        status, raw, headers = call(bases[0], "/up/api/ai_fix", "POST", b'{"kind":"cycle"}',
                                    {"Content-Type": "application/json"})
        check("missing Bearer -> 401 and zero upstream", status == 401
              and body_json(raw) == {"error": "write authentication required"}
              and not Stub.posts)

        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", b'{"kind":"cycle"}',
                              {**bearer, "Authorization": "Bearer wrong"})
        check("wrong Bearer -> 401 and zero upstream", status == 401
              and body_json(raw) == {"error": "write authentication required"}
              and not Stub.posts)

        status, raw, _ = call(bases[0], "/up/api/ai_fix?unexpected=1", "POST",
                              b'{"kind":"cycle"}', bearer)
        check("query -> 400 and zero upstream", status == 400
              and body_json(raw) == {"error": "query parameters not allowed"}
              and not Stub.posts)

        for method in ("GET", "PUT", "PATCH", "DELETE"):
            Stub.posts.clear()
            status, raw, _ = call(bases[0], "/up/api/ai_fix", method, b"{}", bearer)
            check(method + " exact route -> 403 and zero upstream", status == 403
                  and body_json(raw) == {"error": "read-only proxy: write methods blocked"}
                  and not Stub.posts)

        for payload in (b"", b"[]", b"null", b'{"kind":"bad"}',
                        b'{"kind":"cycle","text":"unexpected"}',
                        b'{"kind":"userimport","text":" \t "}',
                        b'{"kind":"userimport","text":"x\\n"}'):
            Stub.posts.clear()
            status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", payload, bearer)
            check("invalid AI schema -> 400 and zero upstream", status == 400
                  and body_json(raw) == {"error": "invalid ai fix request"}
                  and not Stub.posts)

        overlong = json.dumps({"kind": "userimport", "text": "x" * 2001}).encode()
        Stub.posts.clear()
        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", overlong, bearer)
        check("userimport overlong -> 400 and zero upstream", status == 400
              and body_json(raw) == {"error": "invalid ai fix request"}
              and not Stub.posts)

        forged = {**bearer, "X-DB-Editor": "forged", "X-AI-Tool-Authorization": "Basic forged"}
        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", b'{"kind":"cycle"}', forged)
        result = body_json(raw)
        posts = list(Stub.posts)
        check("cycle create exact success", status == 200
              and set(result) == {"status", "kind", "project", "file", "command"}
              and result["kind"] == "cycle" and len(posts) == 1)
        check("forged client auth/marker do not reach upstream",
              posts[0]["auth"].startswith("Basic ") and posts[0]["marker"] == "1"
              and json.loads(posts[0]["body"]) == {"kind": "cycle"})

        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", b'{"kind":"web"}', bearer)
        result = body_json(raw)
        check("web create exact success", status == 200 and result.get("kind") == "web"
              and result.get("command") == COMMANDS["web"])

        text = "  inspect the failing job  "
        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST",
                              json.dumps({"kind": "userimport", "text": text}).encode(), bearer)
        result = body_json(raw)
        check("userimport trims input and preserves frozen command", status == 200
              and result.get("command") == "[Userimport - dự án ai tool] inspect the failing job")

        Stub.mode = "bad_file"
        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", b'{"kind":"web"}', bearer)
        check("unsafe upstream filename -> generic 502", status == 502
              and body_json(raw) == {"error": "ai fix unavailable"}
              and b"secret" not in raw)
        Stub.mode = "malformed"
        status, raw, _ = call(bases[0], "/up/api/ai_fix", "POST", b'{"kind":"web"}', bearer)
        check("malformed upstream -> generic 502", status == 502
              and body_json(raw) == {"error": "ai fix unavailable"})
        Stub.mode = "success"

        Stub.delay = 1.25
        Stub.posts.clear()
        responses = [None, None]
        def worker(index):
            responses[index] = call(bases[index], "/up/api/ai_fix", "POST",
                                    json.dumps({"kind": "userimport", "text": f"job {index}"}).encode(), bearer)
        threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(20)
        Stub.delay = 0.0
        successful = [item for item in responses if item and item[0] == 200]
        files = [body_json(item[1]).get("file") for item in successful]
        posts = sorted(Stub.posts, key=lambda post: post["started"])
        check("two-process creates have distinct artifacts", len(files) == 2 and len(set(files)) == 2)
        check("named mutex spaces after upstream completion",
              len(posts) == 2 and posts[0].get("completed") is not None
              and posts[1]["started"] - posts[0]["completed"] >= 1.05,
              str(posts))

        for path, method in (("/up/api/ai_fix/answers", "DELETE"),
                             ("/up/api/ai_fix/answers", "POST"),
                             ("/up/api/ai_fix/watcher", "POST"),
                             ("/up/api/sync_remote", "POST"),
                             ("/up/api/sync_all", "POST")):
            Stub.posts.clear()
            status, raw, _ = call(bases[0], path, method, b"{}", bearer)
            check(path + " " + method + " remains deferred", status == 403
                  and body_json(raw) == {"error": "read-only proxy: write methods blocked"}
                  and not Stub.posts)
        print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
        return 0 if FAIL == 0 else 1
    finally:
        for proc in procs:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        stub.shutdown()
        stub.server_close()


if __name__ == "__main__":
    sys.exit(main())