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
# Keep this ordered: the tuple is also the SQLite column mapping order.
PUBLIC_SETTINGS_FIELDS = (
    "tunnel_port", "auto_restart_tunnel", "auto_telegram", "auto_open_browser",
)
LOG_STREAMS = {
    "cache/activity_history.jsonl": "activity",
    "cache/change_log.jsonl": "change",
    "cache/action.log": "action",
}
SAFE_AI_FILE = re.compile(
    r"^ai_fix_(?:cycle|web|userimport)_\d{8}_\d{6}"
    r"(?:\.(?:processing|done|failed))?\.json$"
)
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


def _object(payload, endpoint):
    if not isinstance(payload, dict):
        raise FidelityError(endpoint + " projection is not an object")
    return payload


def _required(payload, key, field=None):
    if key not in payload:
        raise FidelityError("missing required field: " + (field or key))
    return payload[key]


def _string(value, field, nonempty=False):
    if type(value) is not str or (nonempty and not value):
        raise FidelityError("invalid string field: " + field)
    return value


def _required_string(payload, key, field=None, nonempty=False):
    return _string(_required(payload, key, field), field or key, nonempty)


def _validate_public_settings(payload):
    public_fields = set(PUBLIC_SETTINGS_FIELDS)
    if not isinstance(payload, dict) or set(payload) - public_fields:
        raise FidelityError("settings projection contains a non-public field")
    if "tunnel_port" in payload and (
        type(payload["tunnel_port"]) is not int or not 1 <= payload["tunnel_port"] <= 65535
    ):
        raise FidelityError("settings tunnel_port has invalid type or range")
    for field in public_fields - {"tunnel_port"}:
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


def _map_master(repo, run_id, snapshot):
    payload = _object(_parse_json(snapshot.value("api/master"), "api/master"), "api/master")
    clients = _required(payload, "clients")
    schedule = _required(payload, "schedule")
    if type(clients) is not list or type(schedule) is not list:
        raise FidelityError("master clients and schedule must be lists")
    for position, item in enumerate(clients):
        item = _object(item, "master client")
        selected = _required(item, "selected")
        if type(selected) is not bool:
            raise FidelityError("master selected is not boolean")
        repo.add_master_client(run_id, position, {
            "client_id": _required_string(item, "client", "client", True),
            "display_name": _required_string(item, "name", "name", True),
            "remote_name": _required_string(item, "remote_name", "remote_name"),
            "group_name": _required_string(item, "group", "group", True),
            "selected": selected,
            "status": _required_string(item, "status", "status"),
            "raw_json": _raw_json(item),
        })
    for position, item in enumerate(schedule):
        item = _object(item, "master schedule")
        repo.add_schedule_slot(run_id, position, {
            "group_name": _required_string(item, "group", "schedule group", True),
            "open_time": _required_string(item, "time", "schedule time", True),
            "close_time": _required_string(item, "close", "schedule close", True),
            "raw_json": _raw_json(item),
        })


def _map_database(repo, run_id, snapshot):
    payload = _object(_parse_json(snapshot.value("client_database.json"), "client_database.json"), "client_database.json")
    last_updated = _required_string(payload, "lastUpdated", "database lastUpdated", True)
    clients = _required(payload, "clients")
    schedule = _required(payload, "schedule")
    if type(clients) is not list or type(schedule) is not list:
        raise FidelityError("database clients and schedule must be lists")
    master_ids = {row[0] for row in repo.rows(
        "SELECT client_id FROM master_clients WHERE run_id=?", (run_id,)
    )}
    repo.set_database_meta(run_id, last_updated)
    for position, item in enumerate(clients):
        item = _object(item, "database client")
        remote_idx = _required(item, "idx", "database idx")
        selected = _required(item, "selected", "database selected")
        if type(remote_idx) is not int or type(selected) is not bool:
            raise FidelityError("database client types are invalid")
        client_id = _required_string(item, "client", "database client", True)
        if client_id not in master_ids:
            raise FidelityError("database client is not present in master")
        repo.add_database_client(run_id, position, {
            "remote_idx": remote_idx,
            "client_id": client_id,
            "display_name": _required_string(item, "name", "database name", True),
            "status": _required_string(item, "status", "database status"),
            "group_name": _required_string(item, "group", "database group", True),
            "selected": selected,
            "raw_json": _raw_json(item),
        })
    for position, item in enumerate(schedule):
        repo.add_database_schedule(run_id, position, _raw_json(_object(item, "database schedule")))


def _map_settings(repo, run_id, snapshot):
    payload = _object(_parse_json(snapshot.value("api/settings"), "api/settings"), "api/settings")
    _validate_public_settings(payload)
    repo.set_public_settings(run_id, payload, snapshot.captured_at)


def _map_cycle(repo, run_id, snapshot):
    payload = _object(_parse_json(snapshot.value("api/cycle/status"), "api/cycle/status"), "api/cycle/status")
    state = _required(payload, "state")
    overrides = _required(payload, "manual_overrides")
    if type(state) is not dict or type(overrides) is not list:
        raise FidelityError("cycle state and manual_overrides have invalid types")
    today = _required_string(state, "today", "cycle today", True)
    done = _required(state, "done")
    if type(done) is not dict:
        raise FidelityError("cycle done ledger is not an object")
    repo.set_cycle_state(run_id, today, _raw_json(state))
    for position, (slot_key, result) in enumerate(done.items()):
        if type(slot_key) is not str or type(result) is not str:
            raise FidelityError("cycle ledger key/value must be strings")
        repo.add_cycle_slot(run_id, today, position, slot_key, result)
    for position, item in enumerate(overrides):
        item = _object(item, "manual override")
        change = _required_string(item, "change", "override change", True)
        if change.count("->") != 1:
            raise FidelityError("override change is not a single transition")
        from_state, to_state = (part.strip() for part in change.split("->", 1))
        if not from_state or not to_state:
            raise FidelityError("override transition is empty")
        repo.add_manual_override(run_id, position, {
            "client_id": _required_string(item, "client", "override client", True),
            "until_at": _required_string(item, "until", "override until", True),
            "detected_at": _required_string(item, "detected_at", "override detected_at", True),
            "from_state": from_state,
            "to_state": to_state,
            "raw_json": _raw_json(item),
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


def _validate_ai_item(item):
    item = _object(item, "AI-fix item")
    file_name = _required_string(item, "file", "AI filename", True)
    if not SAFE_AI_FILE.fullmatch(file_name):
        raise FidelityError("AI-fix filename is unsafe or missing")
    for field in ("kind", "time", "summary", "model", "answer"):
        _required_string(item, field, "AI " + field)
    return item


def _map_ai_status(repo, run_id, snapshot):
    payload = _object(_parse_json(snapshot.value("api/ai_fix/status"), "api/ai_fix/status"), "api/ai_fix/status")
    position = 0
    for key, status in (("pending", "pending"), ("recent_done", "done"), ("recent_failed", "failed")):
        items = _required(payload, key)
        if type(items) is not list:
            raise FidelityError("AI-fix list is not a list: " + key)
        for item in items:
            item = _validate_ai_item(item)
            repo.add_ai_fix_request(run_id, position, {
                "file_name": item["file"],
                "lifecycle_status": status,
                "kind": item["kind"],
                "command": item.get("command"),
                "user_text": item.get("text"),
                "result_json": None if item.get("result") is None else _raw_json(item["result"]),
                "raw_json": _raw_json(item),
            })
            position += 1


def _validate_backup_files(items, field):
    if type(items) is not list:
        raise FidelityError("backup " + field + " is not a list")
    for item in items:
        item = _object(item, "backup file")
        _required_string(item, "path", "backup file path", True)
        _required_string(item, "sha256", "backup file hash", True)
        if type(_required(item, "size", "backup file size")) is not int:
            raise FidelityError("backup file size is not an integer")


def _map_backups(repo, run_id, snapshot):
    payload = _object(_parse_json(snapshot.value("api/cycle/backup"), "api/cycle/backup"), "api/cycle/backup")
    backups = _required(payload, "backups")
    if type(backups) is not list:
        raise FidelityError("backups is not a list")
    for position, item in enumerate(backups):
        item = _object(item, "backup item")
        size = _required(item, "size", "backup size")
        if type(size) is not int:
            raise FidelityError("backup size is not an integer")
        _validate_backup_files(_required(item, "files"), "files")
        _validate_backup_files(_required(item, "script_files"), "script_files")
        repo.add_backup(run_id, position, {
            "name": _required_string(item, "name", "backup name", True),
            "size": size,
            "mtime": _required_string(item, "mtime", "backup mtime", True),
            "label": _required_string(item, "label", "backup label"),
            "created_at": _required_string(item, "created_at", "backup created_at", True),
            "raw_json": _raw_json(item),
        })


def _map_observations(repo, run_id, snapshot):
    status = _object(_parse_json(snapshot.value("api/status"), "api/status"), "api/status")
    if type(_required(status, "clients")) is not int:
        raise FidelityError("status clients is not an integer")
    _required_string(status, "lastUpdated", "status lastUpdated", True)
    _required_string(status, "time", "status time", True)
    sync = _object(_parse_json(snapshot.value("api/sync_status"), "api/sync_status"), "api/sync_status")
    if type(_required(sync, "continuous_running")) is not bool:
        raise FidelityError("sync continuous_running is not boolean")
    pid = _required(sync, "continuous_pid")
    if pid is not None and type(pid) is not int:
        raise FidelityError("sync continuous_pid is not an integer or null")
    for field in ("interval_sec", "status_interval_sec"):
        if type(_required(sync, field)) is not int:
            raise FidelityError("sync field is not an integer: " + field)
    for endpoint, payload in (("api/status", status), ("api/sync_status", sync)):
        repo.add_observation(run_id, endpoint, _raw_json(payload))


def _source_hash(snapshot):
    material = b"".join(
        endpoint.encode("utf-8") + b"\0" + snapshot.values[endpoint].canonical_sha256.encode("ascii")
        for endpoint in SOURCE_ORDER
    )
    return _sha256(material)


def _canonical_payload(endpoint, body):
    return _canonical_bytes(endpoint, body)


def _same_projection(endpoint, actual, expected):
    return _canonical_payload(endpoint, _json_bytes(actual)) == _canonical_payload(endpoint, _json_bytes(expected))


def _raw_rows(repository, query, args):
    return [json.loads(row[0]) for row in repository.rows(query, args)]


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

    master_source = _parse_json(snapshot.value("api/master"), "api/master")
    master_actual = {
        "clients": _raw_rows(repository, "SELECT raw_json FROM master_clients WHERE run_id=? ORDER BY position", (run_id,)),
        "schedule": _raw_rows(repository, "SELECT raw_json FROM schedule_slots WHERE run_id=? ORDER BY position", (run_id,)),
    }
    checks["master_order_and_projection"] = _same_projection(
        "api/master", master_actual,
        {"clients": master_source["clients"], "schedule": master_source["schedule"]},
    )
    if not checks["master_order_and_projection"]:
        mismatches.append("master order/projection mismatch")

    db_source = _parse_json(snapshot.value("client_database.json"), "client_database.json")
    db_actual = {
        "lastUpdated": repository.rows("SELECT last_updated FROM database_meta WHERE run_id=?", (run_id,))[0][0],
        "clients": _raw_rows(repository, "SELECT raw_json FROM database_clients WHERE run_id=? ORDER BY position", (run_id,)),
        "schedule": _raw_rows(repository, "SELECT raw_json FROM database_schedule WHERE run_id=? ORDER BY position", (run_id,)),
    }
    checks["database_order_and_projection"] = _same_projection(
        "client_database.json", db_actual,
        {"lastUpdated": db_source["lastUpdated"], "clients": db_source["clients"], "schedule": db_source["schedule"]},
    )
    if not checks["database_order_and_projection"]:
        mismatches.append("database order/projection mismatch")

    settings_source = _parse_json(snapshot.value("api/settings"), "api/settings")
    settings_row = repository.rows(
        "SELECT tunnel_port, auto_restart_tunnel, auto_telegram, auto_open_browser FROM public_settings WHERE run_id=?",
        (run_id,),
    )[0]
    settings_actual = {
        key: value for key, value in zip(PUBLIC_SETTINGS_FIELDS, settings_row) if value is not None
    }
    for key in ("auto_restart_tunnel", "auto_telegram", "auto_open_browser"):
        if key in settings_actual:
            settings_actual[key] = bool(settings_actual[key])
    checks["settings_redacted_projection"] = settings_actual == settings_source
    if not checks["settings_redacted_projection"]:
        mismatches.append("settings public projection mismatch")

    cycle_source = _parse_json(snapshot.value("api/cycle/status"), "api/cycle/status")
    state_row = repository.rows("SELECT today, state_json FROM cycle_state WHERE run_id=?", (run_id,))[0]
    checks["cycle_state_projection"] = (
        state_row[0] == cycle_source["state"]["today"]
        and _same_projection("api/cycle/status", json.loads(state_row[1]), cycle_source["state"])
    )
    if not checks["cycle_state_projection"]:
        mismatches.append("cycle state projection mismatch")
    actual_slots = repository.rows(
        "SELECT slot_key, result FROM cycle_slot_state WHERE run_id=? ORDER BY position", (run_id,)
    )
    expected_slots = list(cycle_source["state"]["done"].items())
    checks["cycle_slot_ledger"] = actual_slots == expected_slots
    if not checks["cycle_slot_ledger"]:
        mismatches.append("cycle slot ledger mismatch")
    checks["manual_overrides"] = _raw_rows(
        repository,
        "SELECT raw_json FROM manual_overrides WHERE run_id=? ORDER BY position",
        (run_id,),
    ) == cycle_source["manual_overrides"]
    if not checks["manual_overrides"]:
        mismatches.append("manual overrides mismatch")

    ai_source = _parse_json(snapshot.value("api/ai_fix/status"), "api/ai_fix/status")
    ai_ok = True
    for key, status in (("pending", "pending"), ("recent_done", "done"), ("recent_failed", "failed")):
        actual = _raw_rows(
            repository,
            "SELECT raw_json FROM ai_fix_requests WHERE run_id=? AND lifecycle_status=? ORDER BY position",
            (run_id, status),
        )
        if actual != ai_source[key]:
            ai_ok = False
    checks["ai_fix_rows_and_order"] = ai_ok
    if not ai_ok:
        mismatches.append("AI-fix rows/order mismatch")

    backup_source = _parse_json(snapshot.value("api/cycle/backup"), "api/cycle/backup")
    checks["backup_metadata_and_order"] = _raw_rows(
        repository,
        "SELECT raw_json FROM backup_metadata WHERE run_id=? ORDER BY position",
        (run_id,),
    ) == backup_source["backups"]
    if not checks["backup_metadata_and_order"]:
        mismatches.append("backup metadata/order mismatch")

    for endpoint in ("api/status", "api/sync_status"):
        actual = repository.rows(
            "SELECT payload_json FROM source_observations WHERE run_id=? AND endpoint=?",
            (run_id, endpoint),
        )[0][0]
        checks[endpoint + "_observation"] = _same_projection(
            endpoint, json.loads(actual), _parse_json(snapshot.value(endpoint), endpoint)
        )
        if not checks[endpoint + "_observation"]:
            mismatches.append(endpoint + " observation mismatch")

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
