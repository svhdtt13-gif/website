"""Proxy read-only: serve frontend tĩnh + chuyển tiếp GET allowlist sang ai tool.

Chạy: set AI_TOOL_API_BASE / AI_TOOL_USER / AI_TOOL_PASS rồi `python proxy.py`.
Bind 127.0.0.1 — local only. Không bao giờ proxy POST/PUT/PATCH/DELETE.
"""
import base64
import pathlib
import urllib.request
import urllib.error
from flask import Flask, Response, jsonify, send_from_directory

import config

BASE = pathlib.Path(__file__).resolve().parent
FRONTEND = BASE.parent / "frontend"
app = Flask(__name__, static_folder=None)


def _upstream_headers():
    raw = f"{config.AI_TOOL_USER}:{config.AI_TOOL_PASS}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


@app.route("/")
def index():
    return send_from_directory(str(FRONTEND), "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    if "/" in filename or "\\" in filename or filename.startswith("."):
        return jsonify({"error": "not found"}), 404
    if not filename.endswith((".html", ".css", ".js")):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(str(FRONTEND), filename)


@app.route("/up/<path:subpath>")
def upstream(subpath):
    if subpath not in config.READ_ONLY_ALLOWLIST:
        return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
    url = f"{config.AI_TOOL_API_BASE}/{subpath}"
    req = urllib.request.Request(url, headers=_upstream_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return Response(r.read(), status=r.status,
                            content_type=r.headers.get_content_type())
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b'{"error":"upstream error"}'
        return Response(body, status=e.code, content_type="application/json")
    except Exception as e:
        return jsonify({"error": f"upstream unreachable: {e}"[:300]}), 502


if __name__ == "__main__":
    from waitress import serve
    print(f"Website skeleton on http://127.0.0.1:{config.PORT} (read-only -> {config.AI_TOOL_API_BASE})")
    serve(app, host="127.0.0.1", port=config.PORT, threads=8)
