"""Flask app: serve frontend tinh + proxy GET allowlist + write allowlists.

Luong Phase 2: route -> service -> repository -> ai tool.
Hanh vi GET giu nguyen proxy read-only; Slice 2 chi mo write api/log
sau khi qua Bearer gate rieng cua website.
Slice 3 them GET api/cycle/backup; Slice 4 them GET api/master rieng.
Slice 5 them GET api/settings voi positive allowlist va fail-closed redaction.
Slice 6 them POST api/cycle/backup voi write gate va spacing.
Slice 7 them POST api/settings voi shared projector va safe partial update.
Slice 8 them guarded CAS name-only POST api/master.
Slice 9 them canonical GET api/remote_live khong selector.
Slice 10 them guarded remote selector query.
Slice 11 them dedicated guarded DELETE api/cycle/backup/<name>.
Bundle 1 them guarded settings Telegram test va browser open actions.
Bundle 2 chi mo guarded AI-fix creation; sync/answers/watcher remain deferred.
Phase 3 adds guarded SQLite generation reads; enablement remains disabled by default.
"""
import hmac
import pathlib

from flask import Flask, Response, jsonify, request, send_from_directory

import config
from repositories.aitool import UpstreamError
from services import aifix as aifix_service
from services import backup as backup_service
from services import cycle as cycle_service
from services import log as log_service
from services import master as master_service
from services import remote_live as remote_live_service
from services import settings as settings_service
from services import settings_actions as settings_actions_service
from services import sqlite_runtime
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
    "api/cycle/backup": backup_service.get_cycle_backups,
    "api/master": master_service.get_api_master,
    "api/remote_live": remote_live_service.get_remote_live,
    "clients_master.json": master_service.get_master,
    "client_database.json": master_service.get_database,
    "api/settings": settings_service.get_settings,
}

WRITE_HANDLERS = {
    "api/log": log_service.append_log,
    "api/cycle/backup": backup_service.create_cycle_backup,
    "api/settings": settings_service.update_settings,
    "api/master": master_service.update_master_names,
}


def _write_authorized():
    """Require the website's minimal write token before contacting upstream."""
    expected = config.WEBAPP_WRITE_TOKEN
    supplied = request.headers.get("Authorization", "")
    return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")


def _raw_request_path():
    """Return the raw path when the WSGI server exposes it, without query data."""
    raw = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
    return (raw or request.path).split("?", 1)[0]


def _settings_action_gate():
    """Reject query/methods before action handlers or upstream access."""
    if request.query_string:
        return jsonify({"error": "query parameters not allowed"}), 400
    if request.method != "POST":
        return jsonify({"error": "read-only proxy: write methods blocked"}), 403
    if not _write_authorized():
        return jsonify({"error": "write authentication required"}), 401, {
            "WWW-Authenticate": "Bearer"
        }
    return None


def create_app(runtime=None):
    app = Flask(__name__, static_folder=None)
    sqlite = runtime or sqlite_runtime.SQLiteRuntimeCoordinator(
        runtime_dir=config.SQLITE_RUNTIME_DIR,
        read_enabled=config.SQLITE_READ_ENABLED,
        group_enabled={
            sqlite_runtime.GROUP_MASTER_DATABASE: config.SQLITE_MASTER_DATABASE_READ_ENABLED,
            sqlite_runtime.GROUP_PUBLIC_SETTINGS: config.SQLITE_PUBLIC_SETTINGS_READ_ENABLED,
        },
        freshness_seconds=config.SQLITE_FRESHNESS_SECONDS,
        refresh_timeout_seconds=config.SQLITE_REFRESH_TIMEOUT_SECONDS,
        mutex_name=config.SQLITE_MUTEX_NAME,
    )
    app.extensions["sqlite_runtime"] = sqlite
    startup_refresh = getattr(sqlite, "startup_refresh", None)
    if startup_refresh is not None:
        startup_refresh()

    @app.route("/")
    def index():
        return send_from_directory(str(FRONTEND), "index.html")

    @app.route("/<path:filename>")
    def static_files(filename):
        # Cho phep file long nhau (js/, css/) nhung giam trong FRONTEND:
        # resolve + kiem tra relative, chan traversal, chi 3 duoi file.
        try:
            target = (FRONTEND / filename).resolve()
            target.relative_to(FRONTEND.resolve())
        except Exception:
            return jsonify({"error": "not found"}), 404
        if target.suffix not in ALLOWED_STATIC_EXT or not target.is_file():
            return jsonify({"error": "not found"}), 404
        return send_from_directory(str(target.parent), target.name)

    @app.route(
        "/up/api/settings/test_telegram",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def test_telegram():
        rejection = _settings_action_gate()
        if rejection:
            return rejection
        try:
            body, status, ctype = settings_actions_service.test_telegram()
        except UpstreamError as e:
            return Response(e.body, status=e.status, content_type="application/json")
        return Response(body, status=status, content_type=ctype)

    @app.route(
        "/up/api/settings/open_browser",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def open_browser():
        rejection = _settings_action_gate()
        if rejection:
            return rejection
        try:
            body, status, ctype = settings_actions_service.open_browser(request.get_data())
        except UpstreamError as e:
            return Response(e.body, status=e.status, content_type="application/json")
        return Response(body, status=status, content_type=ctype)

    @app.route(
        "/up/api/ai_fix",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    def create_ai_fix():
        rejection = _settings_action_gate()
        if rejection:
            return rejection
        try:
            body, status, ctype = aifix_service.create_ai_fix(request.get_data())
        except UpstreamError as e:
            return Response(e.body, status=e.status, content_type="application/json")
        return Response(body, status=status, content_type=ctype)

    @app.route("/up/api/cycle/backup/<name>", methods=["DELETE"])
    def delete_backup(name):
        """Dedicated dynamic backup DELETE boundary; no generic prefix dispatch."""
        if not _write_authorized():
            return jsonify({"error": "write authentication required"}), 401, {
                "WWW-Authenticate": "Bearer"
            }
        try:
            canonical_name = backup_service.canonical_backup_name(_raw_request_path(), name)
        except backup_service.BackupNameError:
            return jsonify({"error": "invalid backup name"}), 400
        try:
            body, status, ctype = backup_service.delete_cycle_backup(canonical_name)
        except UpstreamError as e:
            return Response(e.body, status=e.status, content_type="application/json")
        return Response(body, status=status, content_type=ctype)

    @app.route("/up/<path:subpath>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def upstream(subpath):
        if request.method == "GET":
            handler = READ_HANDLERS.get(subpath)
            if subpath == "api/remote_live" and request.query_string:
                try:
                    selector = remote_live_service.parse_selector_query(request.query_string)
                except remote_live_service.RemoteSelectorQueryError:
                    return jsonify({"error": "invalid remote selector query"}), 400
                if not _write_authorized():
                    return jsonify({"error": "write authentication required"}), 401, {
                        "WWW-Authenticate": "Bearer"
                    }
                handler = lambda: remote_live_service.get_remote_live_selector(selector)
            if subpath not in config.READ_ONLY_ALLOWLIST:
                return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
            if handler is None:
                # Allowlist co path chua co service: chan thay vi forward truc tiep.
                return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
            try:
                body, status, ctype = sqlite.read(subpath, handler)
            except UpstreamError as e:
                return Response(e.body, status=e.status, content_type="application/json")
            return Response(body, status=status, content_type=ctype)

        # Write: method/path allowlist is not enough; require website Bearer auth first.
        if request.method == "POST" and subpath in config.WRITE_ALLOWLIST:
            if not _write_authorized():
                return jsonify({"error": "write authentication required"}), 401, {
                    "WWW-Authenticate": "Bearer"
                }
            handler = WRITE_HANDLERS.get(subpath)
            if handler is None:
                return jsonify({"error": "read-only proxy: endpoint not allowed"}), 403
            try:
                if subpath == "api/master":
                    before_write = None
                    if sqlite.configured_enabled(sqlite_runtime.GROUP_MASTER_DATABASE):
                        before_write = lambda expected: sqlite.write_fence(
                            sqlite_runtime.GROUP_MASTER_DATABASE, expected
                        )
                    body, status, ctype = handler(
                        request.get_data(), request.content_type,
                        before_upstream_write=before_write,
                    )
                elif subpath == "api/settings":
                    before_write = None
                    if sqlite.configured_enabled(sqlite_runtime.GROUP_PUBLIC_SETTINGS):
                        before_write = lambda expected: sqlite.write_fence(
                            sqlite_runtime.GROUP_PUBLIC_SETTINGS, expected
                        )
                    body, status, ctype = handler(
                        request.get_data(), request.content_type,
                        before_upstream_write=before_write,
                    )
                else:
                    body, status, ctype = handler(
                        request.get_data(), request.content_type
                    )
            except UpstreamError as e:
                return Response(e.body, status=e.status, content_type="application/json")
            return Response(body, status=status, content_type=ctype)
        return jsonify({"error": "read-only proxy: write methods blocked"}), 403

    return app
