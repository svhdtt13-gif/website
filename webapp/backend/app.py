"""Flask app: serve frontend tĩnh + proxy GET allowlist (read-only).

Luồng Phase 2: route -> service -> repository -> ai tool.
Hành vi và contract giữ nguyên y hệt bản proxy trực tiếp.
"""
import pathlib

from flask import Flask, Response, jsonify, request, send_from_directory

import config
from repositories.aitool import UpstreamError
from services import aifix as aifix_service
from services import cycle as cycle_service
from services import master as master_service
from services import sync as sync_service

BASE = pathlib.Path(__file__).resolve().parent
FRONTEND = BASE.parent / "frontend"

ALLOWED_STATIC_EXT = {".html", ".css", ".js"}

READ_HANDLERS = {
    "api/cycle/status": cycle_service.get_cycle_status,
    "api/cycle_status": cycle_service.get_cycle_simple_status,
    "api/sync_status": sync_service.get_sync_status,
    "api/status": sync_service.get_general_status,
    "api/ai_fix/status": aifix_service.get_ai_fix_status,
    "clients_master.json": master_service.get_master,
    "client_database.json": master_service.get_database,
}


def create_app():
    app = Flask(__name__, static_folder=None)

    @app.route("/")
    def index():
        return send_from_directory(str(FRONTEND), "index.html")

    @app.route("/<path:filename>")
    def static_files(filename):
        # Cho phép file lồng nhau (js/, css/) nhưng giam trong FRONTEND:
        # resolve + kiểm tra relative, chặn traversal, chỉ 3 đuôi file.
        try:
            target = (FRONTEND / filename).resolve()
            target.relative_to(FRONTEND.resolve())
        except Exception:
            return jsonify({"error": "not found"}), 404
        if target.suffix not in ALLOWED_STATIC_EXT or not target.is_file():
            return jsonify({"error": "not found"}), 404
        return send_from_directory(str(target.parent), target.name)

    @app.route("/up/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def upstream(subpath):
        if request.method != "GET":
            return jsonify({"error": "read-only proxy: write methods blocked"}), 403
        if subpath not in config.READ_ONLY_ALLOWLIST:
            return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
        handler = READ_HANDLERS.get(subpath)
        if handler is None:
            # Allowlist có path chưa có service: chặn thay vì forward trực tiếp.
            return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
        try:
            body, status, ctype = handler()
        except UpstreamError as e:
            return Response(e.body, status=e.status, content_type="application/json")
        return Response(body, status=status, content_type=ctype)

    return app
