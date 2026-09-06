"""HTTP-only two-pass importer for a disposable SQLite candidate.

The importer is intentionally not imported by Flask routes. Its source client
accepts only golden HTTP API projections, including the redacted settings
projection, and never falls back to ai tool filesystem access.
"""
from dataclasses import dataclass
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
import uuid

from repositories.aitool import UpstreamError, ai_tool
from repositories.sqlite import SQLiteCandidateRepository
from services import settings as settings_service


SOURCE_ORDER = (
    "api/master",
    "client_database.json",
    "api/settings",
    "api/cycle/status",
    "cache/activity_history.jsonl",
    "cache/change_log.jsonl",
    "cache/action.log",
    "api/ai_fix/status",
    "api/cycle/backup",
    "api/status",
    "api/sync_status",
)
JSON_ENDPOINTS = frozenset(
    endpoint for endpoint in SOURCE_ORDER if not endpoint.startswith("cache/")
)
PUBLIC_SETTINGS_FIELDS = frozenset({
    "tunnel_port", "auto_restart_tunnel", "auto_telegram", "auto_open_browser",
})
LOG_STREAMS = {
    "cache/activity_history.jsonl": "activity",
    "cache/change_log.jsonl": "change",
    "cache/action.log": "action",
}
SAFE_AI_FILE = re.compile(r"^ai_fix_(?:cycle|web|userimport)_\d{8}_\d{6}\.json$")
VOLATILE_FIELDS = {
    "api/status": frozenset({"time"}),
    "api/cycle/status": frozenset({"checked_at"}),
}


class SourceAcquisitionError(RuntimeError):
    """A required source API cannot provide a valid projection."""


class UnstableSnapshotError(SourceAcquisitionError):
    """The source changed during the bounded stability fence."""


class FidelityError(SourceAcquisitionError):
    """Source data cannot be represented without silent loss."""


@dataclass(frozen=True)
class SourceValue:
    endpoint: str
    body: bytes
    content_type: str
    raw_sha256: str
    canonical_sha256: str


@dataclass(frozen=True)
class StableSnapshot:
    snapshot_id: str
    captured_at: str
    values: dict

    def value(self, endpoint):
        try:
            return self.values[endpoint]
        except KeyError as error:
            raise SourceAcquisitionError("required source endpoint missing") from error


@dataclass(frozen=True)
class ImportReceipt:
    candidate_path: str
    run_id: str
    snapshot_id: str
    source_hash: str
    status: str
    checks: dict


class AiToolHttpSource:
    """Acquire only fixed HTTP projections from the golden ai tool API."""

    def __init__(self, repository=None):
        self.repository = repository or ai_tool

    def fetch(self, endpoint):
        if endpoint not in SOURCE_ORDER:
            raise SourceAcquisitionError("source endpoint is not allowlisted")
        if endpoint == "api/settings":
            # settings_service returns only the four public fields. The raw
            # upstream settings response never reaches this importer.
            try:
                body, status, content_type = settings_service.get_settings()
            except UpstreamError as error:
                raise SourceAcquisitionError("public settings unavailable") from error
        else:
            try:
                body, status, content_type = self.repository.get(endpoint)
            except UpstreamError as error:
                raise SourceAcquisitionError("source endpoint unavailable") from error
        if status != 200:
            raise SourceAcquisitionError("source endpoint returned unexpected status")
        if not isinstance(body, bytes):
            raise SourceAcquisitionError("source endpoint returned non-bytes")
        if endpoint in JSON_ENDPOINTS:
            try:
                payload = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SourceAcquisitionError("source JSON is invalid") from error
            if endpoint == "api/settings":
                _validate_public_settings(payload)
                # Re-serialize the already-redacted projection for stable input.
                body = _json_bytes(payload)
            else:
                body = body
        return SourceValue(
            endpoint=endpoint,
            body=body,
            content_type=content_type or "",
            raw_sha256=_sha256(body),
            canonical_sha256=_sha256(_canonical_bytes(endpoint, body)),
        )


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _canonical_bytes(endpoint, body):
    if endpoint not in JSON_ENDPOINTS:
        return body
    payload = json.loads(body.decode("utf-8"))
    for field in VOLATILE_FIELDS.get(endpoint, ()):
        if isinstance(payload, dict):
            payload.pop(field, None)
    return _json_bytes(_sorted_json(payload))


def _sorted_json(value):
    if isinstance(value, dict):
        return {key: _sorted_json(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sorted_json(item) for item in value]
    return value


def _parse_json(value, endpoint):
    try:
        return json.loads(value.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SourceAcquisitionError("source JSON is invalid: " + endpoint) from error


def _validate_public_settings(payload):
    if not isinstance(payload, dict) or set(payload) - PUBLIC_SETTINGS_FIELDS:
        raise FidelityError("settings projection contains a non-public field")
    if "tunnel_port" in payload and (
        type(payload["tunnel_port"]) is not int or not 1 <= payload["tunnel_port"] <= 65535
    ):
        raise FidelityError("settings tunnel_port has invalid type or range")
    for field in PUBLIC_SETTINGS_FIELDS - {"tunnel_port"}:
        if field in payload and type(payload[field]) is not bool:
            raise FidelityError("settings boolean has invalid type")


def capture_stable_snapshot(source, max_passes=3):
    if max_passes < 2:
        raise ValueError("stable snapshot requires at least two passes")
    previous = None
    for _attempt in range(max_passes):
        values = {endpoint: source.fetch(endpoint) for endpoint in SOURCE_ORDER}
        signature = tuple(
            (endpoint, values[endpoint].canonical_sha256)
            for endpoint in SOURCE_ORDER
        )
        if previous == signature:
            return StableSnapshot(
                snapshot_id=uuid.uuid4().hex,
                captured_at=datetime.now(timezone.utc).isoformat(),
                values=values,
            )
        previous = signature
    raise UnstableSnapshotError("source changed during stability fence")


def _require_list(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    if type(value) is not list:
        raise FidelityError("missing or invalid list: " + key)
    return value


def _text(value, field, allow_none=False):
    if allow_none and value is None:
        return None
    if type(value) is not str or not value:
        raise FidelityError("invalid text field: " + field)
    return value


def _raw_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _map_master(repo, run_id, snapshot):
    payload = _parse_json(snapshot.value("api/master"), "api/master")
    clients = _require_list(payload, "clients")
    schedule = _require_list(payload, "schedule")
    for position, item in enumerate(clients):
        if not isinstance(item, dict):
            raise FidelityError("master client is not an object")
        selected = item.get("selected", True)
        if type(selected) is not bool:
            raise FidelityError("master selected is not boolean")
        repo.add_master_client(run_id, position, {
            "client_id": _text(item.get("client"), "client"),
            "display_name": _text(item.get("name"), "name"),
            "remote_name": _text(item.get("remote_name"), "remote_name", True),
            "group_name": _text(item.get("group"), "group"),
            "selected": selected,
            "status": _text(item.get("status"), "status", True),
            "raw_json": _raw_json(item),
        })
    for position, item in enumerate(schedule):
        if not isinstance(item, dict):
            raise FidelityError("master schedule item is not an object")
        repo.add_schedule_slot(run_id, position, {
            "group_name": _text(item.get("group"), "schedule group"),
            "open_time": _text(item.get("time"), "schedule time"),
            "close_time": _text(item.get("close"), "schedule close"),
            "raw_json": _raw_json(item),
        })


def _map_database(repo, run_id, snapshot):
    payload = _parse_json(snapshot.value("client_database.json"), "client_database.json")
    clients = _require_list(payload, "clients")
    schedule = _require_list(payload, "schedule")
    repo.set_database_meta(run_id, payload.get("lastUpdated"))
    for position, item in enumerate(clients):
        if not isinstance(item, dict) or type(item.get("idx")) is not int:
            raise FidelityError("database client index is invalid")
        selected = item.get("selected")
        if selected is not None and type(selected) is not bool:
            raise FidelityError("database selected is not boolean")
        repo.add_database_client(run_id, position, {
            "remote_idx": item["idx"],
            "client_id": _text(item.get("client"), "database client"),
            "display_name": _text(item.get("name"), "database name", True),
            "status": _text(item.get("status"), "database status", True),
            "group_name": _text(item.get("group"), "database group", True),
            "selected": selected,
            "raw_json": _raw_json(item),
        })
    for position, item in enumerate(schedule):
        if not isinstance(item, dict):
            raise FidelityError("database schedule item is not an object")
        repo.add_database_schedule(run_id, position, _raw_json(item))


def _map_settings(repo, run_id, snapshot):
    payload = _parse_json(snapshot.value("api/settings"), "api/settings")
    _validate_public_settings(payload)
    repo.set_public_settings(run_id, payload, snapshot.captured_at)


def _map_cycle(repo, run_id, snapshot):
    payload = _parse_json(snapshot.value("api/cycle/status"), "api/cycle/status")
    state = payload.get("state")
    if state is None:
        state = {}
    if not isinstance(state, dict):
        raise FidelityError("cycle state projection is not an object")
    today = state.get("today")
    if today is not None:
        _text(today, "cycle today")
    repo.set_cycle_state(run_id, today, _raw_json(state))
    done = state.get("done", {})
    if not isinstance(done, dict):
        raise FidelityError("cycle done ledger is not an object")
    for slot_key, result in done.items():
        repo.add_cycle_slot(run_id, today or "", _text(str(slot_key), "cycle slot"), _text(str(result), "cycle result"))
    overrides = payload.get("manual_overrides", [])
    if type(overrides) is not list:
        raise FidelityError("manual overrides projection is not a list")
    for item in overrides:
        if not isinstance(item, dict):
            raise FidelityError("manual override is not an object")
        repo.add_manual_override(run_id, {
            "client_id": _text(item.get("client"), "override client"),
            "until_at": _text(item.get("until"), "override until"),
            "detected_at": _text(item.get("detected_at"), "override detected_at", True),
            "from_state": _text(item.get("change", ""), "override from", True),
            "to_state": None,
        })


def _map_logs(repo, run_id, snapshot):
    for endpoint, stream in LOG_STREAMS.items():
        body = snapshot.value(endpoint).body
        offset = 0
        for sequence, line in enumerate(body.splitlines(keepends=True), start=1):
            parsed = None
            if stream != "action":
                try:
                    parsed = _raw_json(json.loads(line.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed = None
            repo.add_audit_event(run_id, {
                "stream": stream,
                "stream_seq": sequence,
                "source_offset": offset,
                "raw_line": line,
                "parsed_json": parsed,
            })
            offset += len(line)


def _map_ai_status(repo, run_id, snapshot):
    payload = _parse_json(snapshot.value("api/ai_fix/status"), "api/ai_fix/status")
    if not isinstance(payload, dict):
        raise FidelityError("AI-fix status is not an object")
    position = 0
    for key, status in (("pending", "pending"), ("recent_done", "done"), ("recent_failed", "failed")):
        for item in _require_list(payload, key):
            if not isinstance(item, dict):
                raise FidelityError("AI-fix item is not an object")
            file_name = item.get("file") or item.get("file_name")
            if type(file_name) is not str or not SAFE_AI_FILE.fullmatch(file_name):
                raise FidelityError("AI-fix filename is unsafe or missing")
            result = item.get("result")
            repo.add_ai_fix_request(run_id, position, {
                "file_name": file_name,
                "lifecycle_status": status,
                "kind": _text(item.get("kind"), "AI kind", True),
                "command": _text(item.get("command"), "AI command", True),
                "user_text": _text(item.get("text"), "AI text", True),
                "result_json": None if result is None else _raw_json(result),
                "raw_json": _raw_json(item),
            })
            position += 1


def _map_backups(repo, run_id, snapshot):
    payload = _parse_json(snapshot.value("api/cycle/backup"), "api/cycle/backup")
    for position, item in enumerate(_require_list(payload, "backups")):
        if not isinstance(item, dict):
            raise FidelityError("backup item is not an object")
        name = _text(item.get("name"), "backup name")
        repo.add_backup(run_id, position, {
            "name": name,
            "size": item.get("size"),
            "mtime": _text(item.get("mtime"), "backup mtime", True),
            "label": _text(item.get("label"), "backup label", True),
            "created_at": _text(item.get("created_at"), "backup created_at", True),
            "raw_json": _raw_json(item),
        })


def _map_observations(repo, run_id, snapshot):
    for endpoint in ("api/status", "api/sync_status"):
        payload = _parse_json(snapshot.value(endpoint), endpoint)
        repo.add_observation(run_id, endpoint, _raw_json(payload))


def _source_hash(snapshot):
    material = b"".join(
        endpoint.encode("utf-8") + b"\0" + snapshot.values[endpoint].canonical_sha256.encode("ascii")
        for endpoint in SOURCE_ORDER
    )
    return _sha256(material)


def _canonical_payload(endpoint, body):
    return _canonical_bytes(endpoint, body)


def shadow_verify(repository, run_id, snapshot):
    checks = {}
    mismatches = []
    checks["integrity"] = repository.integrity_check() == "ok"
    if not checks["integrity"]:
        mismatches.append("sqlite integrity check failed")
    source_rows = repository.rows(
        "SELECT endpoint, raw_sha256, canonical_sha256 FROM source_snapshots WHERE run_id=? ORDER BY position",
        (run_id,),
    )
    checks["source_snapshot_count"] = len(source_rows) == len(SOURCE_ORDER)
    if not checks["source_snapshot_count"]:
        mismatches.append("source snapshot count mismatch")
    for endpoint, raw_hash, canonical_hash in source_rows:
        expected = snapshot.value(endpoint)
        if raw_hash != expected.raw_sha256 or canonical_hash != expected.canonical_sha256:
            mismatches.append("source hash mismatch: " + endpoint)
    master_clients = [json.loads(row[0]) for row in repository.rows(
        "SELECT raw_json FROM master_clients WHERE run_id=? ORDER BY position", (run_id,)
    )]
    master_schedule = [json.loads(row[0]) for row in repository.rows(
        "SELECT raw_json FROM schedule_slots WHERE run_id=? ORDER BY position", (run_id,)
    )]
    master_source = _parse_json(snapshot.value("api/master"), "api/master")
    checks["master_order_and_projection"] = (
        _canonical_payload("api/master", _json_bytes({"clients": master_clients, "schedule": master_schedule}))
        == _canonical_payload("api/master", _json_bytes({"clients": master_source["clients"], "schedule": master_source["schedule"]}))
    )
    if not checks["master_order_and_projection"]:
        mismatches.append("master order/projection mismatch")
    db_clients = [json.loads(row[0]) for row in repository.rows(
        "SELECT raw_json FROM database_clients WHERE run_id=? ORDER BY position", (run_id,)
    )]
    db_schedule = [json.loads(row[0]) for row in repository.rows(
        "SELECT raw_json FROM database_schedule WHERE run_id=? ORDER BY position", (run_id,)
    )]
    db_source = _parse_json(snapshot.value("client_database.json"), "client_database.json")
    db_projected = {"lastUpdated": repository.rows(
        "SELECT last_updated FROM database_meta WHERE run_id=?", (run_id,)
    )[0][0], "clients": db_clients, "schedule": db_schedule}
    checks["database_order_and_projection"] = (
        _canonical_payload("client_database.json", _json_bytes(db_projected))
        == _canonical_payload("client_database.json", _json_bytes({
            "lastUpdated": db_source.get("lastUpdated"),
            "clients": db_source["clients"],
            "schedule": db_source["schedule"],
        }))
    )
    if not checks["database_order_and_projection"]:
        mismatches.append("database order/projection mismatch")
    settings_source = _parse_json(snapshot.value("api/settings"), "api/settings")
    settings_row = repository.rows(
        "SELECT tunnel_port, auto_restart_tunnel, auto_telegram, auto_open_browser FROM public_settings WHERE run_id=?",
        (run_id,),
    )[0]
    settings_projected = {
        key: value for key, value in zip(PUBLIC_SETTINGS_FIELDS, settings_row) if value is not None
    }
    for key in ("auto_restart_tunnel", "auto_telegram", "auto_open_browser"):
        if key in settings_projected:
            settings_projected[key] = bool(settings_projected[key])
    checks["settings_redacted_projection"] = settings_projected == settings_source
    if not checks["settings_redacted_projection"]:
        mismatches.append("settings public projection mismatch")
    for endpoint, stream in LOG_STREAMS.items():
        rows = repository.rows(
            "SELECT raw_line FROM audit_events WHERE run_id=? AND stream=? ORDER BY stream_seq",
            (run_id, stream),
        )
        checks[stream + "_raw_bytes"] = b"".join(row[0] for row in rows) == snapshot.value(endpoint).body
        if not checks[stream + "_raw_bytes"]:
            mismatches.append(stream + " raw bytes mismatch")
    checks["source_hashes"] = not any(
        mismatch.startswith("source hash mismatch") for mismatch in mismatches
    )
    checks["no_settings_secret_columns"] = not any(
        name in {"telegram_bot_token", "telegram_chat_id", "cloudflared_path"}
        for name in (row[1] for row in repository.rows("PRAGMA table_info(public_settings)"))
    )
    if not checks["no_settings_secret_columns"]:
        mismatches.append("settings secret column present")
    return {"ok": not mismatches, "checks": checks, "mismatches": mismatches}


def import_candidate(snapshot, candidate_path):
    candidate = Path(candidate_path)
    repository = SQLiteCandidateRepository.create(candidate)
    run_id = uuid.uuid4().hex
    source_hash = _source_hash(snapshot)
    started_at = datetime.now(timezone.utc).isoformat()
    try:
        with repository.transaction():
            repository.begin_import(run_id, snapshot.snapshot_id, source_hash, started_at)
            for position, endpoint in enumerate(SOURCE_ORDER):
                repository.add_source_snapshot(run_id, position, snapshot.value(endpoint))
            _map_master(repository, run_id, snapshot)
            _map_database(repository, run_id, snapshot)
            _map_settings(repository, run_id, snapshot)
            _map_cycle(repository, run_id, snapshot)
            _map_logs(repository, run_id, snapshot)
            _map_ai_status(repository, run_id, snapshot)
            _map_backups(repository, run_id, snapshot)
            _map_observations(repository, run_id, snapshot)
            report = shadow_verify(repository, run_id, snapshot)
            status = "verified" if report["ok"] else "failed"
            repository.finish_import(
                run_id, datetime.now(timezone.utc).isoformat(),
                _raw_json(report), status,
                None if report["ok"] else "shadow_mismatch",
            )
        return ImportReceipt(str(candidate), run_id, snapshot.snapshot_id,
                             source_hash, status, report)
    finally:
        repository.close()
