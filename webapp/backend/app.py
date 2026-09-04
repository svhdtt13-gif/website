"""Flask app: serve frontend tĩnh + proxy GET allowlist (read-only)."""
import pathlib

from flask import Flask, Response, jsonify, request, send_from_directory

import config
from upstream import UpstreamError, forward_get

BASE = pathlib.Path(__file__).resolve().parent
FRONTEND = BASE.parent / "frontend"


def create_app():
    app = Flask(__name__, static_folder=None)

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

    @app.route("/up/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def upstream(subpath):
        if request.method != "GET":
            return jsonify({"error": "read-only proxy: write methods blocked"}), 403
        if subpath not in config.READ_ONLY_ALLOWLIST:
            return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
        try:
            body, status, ctype = forward_get(subpath)
        except UpstreamError as e:
            return Response(e.body, status=e.status, content_type="application/json")
        return Response(body, status=status, content_type=ctype)

    return app
