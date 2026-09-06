"""Guarded SQLite generation publication, normalized reads, and write fencing."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from repositories.aitool import UpstreamError, ai_tool
from repositories.sqlite import SCHEMA_CHECKSUM, SCHEMA_VERSION, SQLiteCandidateRepository
from services import settings as settings_service
from services.sqlite_import import (
    AiToolHttpSource,
    PUBLIC_SETTINGS_FIELDS,
    SourceAcquisitionError,
    SourceValue,
    _canonical_bytes,
    _json_bytes,
    _sha256,
    _validate_public_settings,
    capture_stable_snapshot,
    import_candidate,
)

GROUP_MASTER_DATABASE = "master_database"
GROUP_PUBLIC_SETTINGS = "public_settings"
GROUPS = (GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS)
ROUTE_ENDPOINTS = {
    "api/master": (GROUP_MASTER_DATABASE, "api/master"),
    "client_database.json": (GROUP_MASTER_DATABASE, "client_database.json"),
    "api/settings": (GROUP_PUBLIC_SETTINGS, "api/settings"),
}
MUTEX_NAME = "Local\\WebsiteSQLiteGenerationMutex"

_FALLBACK_LOCKS = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


class RuntimeStateError(RuntimeError):
    """The current generation or runtime state cannot be trusted."""


class NamedGenerationMutex:
    """Explicit Windows cross-process mutex; POSIX fallback is test-only."""

    def __init__(self, name=MUTEX_NAME, timeout_seconds=15):
        self.name = name
        self.timeout_seconds = timeout_seconds
        with _FALLBACK_LOCKS_GUARD:
            self._fallback = _FALLBACK_LOCKS.setdefault(name, threading.Lock())

    @contextmanager
    def hold(self, timeout_seconds=None):
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if os.name != "nt":
            if not self._fallback.acquire(timeout=timeout):
                raise TimeoutError("SQLite generation mutex timeout")
            try:
                yield
            finally:
                self._fallback.release()
            return

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        try:
            result = kernel32.WaitForSingleObject(
                handle, max(0, int(timeout * 1000))
            )
            if result not in (0, 0x80):
                raise TimeoutError("SQLite generation mutex timeout")
            try:
                yield
            finally:
                kernel32.ReleaseMutex(handle)
        finally:
            kernel32.CloseHandle(handle)


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


def _default_state():
    return {
        "version": 1,
        "current": None,
        "groups": {
            group: {
                "eligible": False,
                "generation_id": None,
                "reason": "not_verified",
                "updated_at": _utc_now(),
            }
            for group in GROUPS
        },
        "fences": {},
        "refresh_lease": None,
        "refresh_pending": [],
    }


def _atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _remove_candidate(path):
    """Best-effort cleanup; Windows may keep a timed-out SQLite file open."""
    path = Path(path)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except OSError:
            pass


def _parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeStateError("invalid generation timestamp") from error


class _TimedRepository:
    """Give each upstream request only the remaining refresh budget."""

    def __init__(self, deadline, clock):
        self.deadline = deadline
        self.clock = clock

    def _remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise SourceAcquisitionError("refresh timeout")
        return remaining

    def get(self, endpoint):
        return ai_tool.get(endpoint, timeout=self._remaining())


class _BoundedAiToolHttpSource(AiToolHttpSource):
    """Use the real HTTP source with a per-request remaining deadline."""

    def __init__(self, deadline, clock):
        super().__init__(repository=_TimedRepository(deadline, clock))
        self.deadline = deadline
        self.clock = clock

    def _remaining(self):
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise SourceAcquisitionError("refresh timeout")
        return remaining

    def fetch(self, endpoint):
        if endpoint != "api/settings":
            return super().fetch(endpoint)
        try:
            body, status, content_type = settings_service.get_settings(
                timeout=self._remaining()
            )
        except UpstreamError as error:
            raise SourceAcquisitionError("public settings unavailable") from error
        if status != 200 or not isinstance(body, bytes):
            raise SourceAcquisitionError("settings source returned unexpected response")
        try:
            payload = json.loads(body.decode("utf-8"))
            _validate_public_settings(payload)
            body = _json_bytes(payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SourceAcquisitionError("source JSON is invalid") from error
        return SourceValue(
            endpoint=endpoint,
            body=body,
            content_type=content_type or "",
            raw_sha256=_sha256(body),
            canonical_sha256=_sha256(_canonical_bytes(endpoint, body)),
        )


class _DeadlineSource:
    """Stop starting new fetches after the coordinator deadline."""

    def __init__(self, source, deadline, clock):
        self.source = source
        self.deadline = deadline
        self.clock = clock

    def fetch(self, endpoint):
        if self.clock() >= self.deadline:
            raise SourceAcquisitionError("refresh timeout")
        return self.source.fetch(endpoint)


class SQLiteRuntimeCoordinator:
    """Select only verified immutable generations and fail closed to HTTP."""

    def __init__(
        self,
        runtime_dir,
        read_enabled=False,
        group_enabled=None,
        freshness_seconds=60,
        refresh_timeout_seconds=15,
        mutex_name=MUTEX_NAME,
        source_factory=None,
        importer=import_candidate,
        background_refresh=True,
        clock=None,
    ):
        self.runtime_dir = Path(runtime_dir)
        self.state_path = self.runtime_dir / "current.json"
        self.read_enabled = bool(read_enabled)
        self.group_enabled = dict(group_enabled or {})
        self.freshness_seconds = freshness_seconds
        self.refresh_timeout_seconds = refresh_timeout_seconds
        self._clock = clock or time.monotonic
        self._mutex = NamedGenerationMutex(mutex_name, refresh_timeout_seconds)
        self._source_factory = source_factory
        self._importer = importer
        self._background_refresh = background_refresh
        self._refresh_guard = threading.Lock()
        self._refresh_active = False
        self._refresh_pending = set()

    def configured_enabled(self, group):
        return self.read_enabled and self.group_enabled.get(group, False)

    def _load_state(self):
        try:
            with self.state_path.open("r", encoding="utf-8") as stream:
                state = json.load(stream)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return _default_state()
        if not isinstance(state, dict) or state.get("version") != 1:
            return _default_state()
        state.setdefault("current", None)
        state.setdefault("groups", {})
        state.setdefault("fences", {})
        state.setdefault("refresh_lease", None)
        pending = state.get("refresh_pending")
        state["refresh_pending"] = pending if isinstance(pending, list) else []
        for group in GROUPS:
            state["groups"].setdefault(
                group,
                {
                    "eligible": False,
                    "generation_id": None,
                    "reason": "not_verified",
                    "updated_at": _utc_now(),
                },
            )
        return state

    @staticmethod
    def _mark_ineligible_locked(state, group, reason):
        current = state.get("current") or {}
        state["groups"][group] = {
            "eligible": False,
            "generation_id": current.get("generation_id"),
            "reason": reason,
            "updated_at": _utc_now(),
        }

    def _mark_ineligible(self, group, reason, timeout_seconds=0):
        try:
            with self._mutex.hold(timeout_seconds=timeout_seconds):
                state = self._load_state()
                self._mark_ineligible_locked(state, group, reason)
                _atomic_json(self.state_path, state)
        except (OSError, TimeoutError):
            # State marking is advisory; HTTP fallback must never wait on refresh.
            pass

    def _release_refresh_lease(self, token):
        if not token:
            return
        try:
            with self._mutex.hold(timeout_seconds=0):
                state = self._load_state()
                lease = state.get("refresh_lease") or {}
                if lease.get("token") == token:
                    state["refresh_lease"] = None
                    _atomic_json(self.state_path, state)
        except (OSError, TimeoutError):
            # Expiry is the crash-recovery backstop if the mutex is busy.
            pass

    @staticmethod
    def _lease_active(lease):
        if not isinstance(lease, dict) or not lease.get("token"):
            return False
        try:
            return datetime.now(timezone.utc) < _parse_time(lease.get("expires_at"))
        except RuntimeStateError:
            return False

    def _lease_expiry(self):
        return (
            datetime.now(timezone.utc)
            + timedelta(seconds=max(1.0, self.refresh_timeout_seconds * 2))
        ).isoformat()

    def _renew_refresh_lease(self, token):
        try:
            with self._mutex.hold(timeout_seconds=0):
                state = self._load_state()
                lease = state.get("refresh_lease") or {}
                if lease.get("token") != token:
                    return False
                lease["expires_at"] = self._lease_expiry()
                lease["heartbeat_at"] = _utc_now()
                state["refresh_lease"] = lease
                _atomic_json(self.state_path, state)
                return True
        except (OSError, TimeoutError):
            return True

    def _refresh_lease_heartbeat(self, token, stop):
        interval = max(0.05, min(1.0, self.refresh_timeout_seconds / 3))
        while not stop.wait(interval):
            if not self._renew_refresh_lease(token):
                return

    def _stop_lease_heartbeat(self, stop, heartbeat):
        if stop is None:
            return
        stop.set()
        if heartbeat is not None and heartbeat is not threading.current_thread():
            heartbeat.join(1)

    def _queue_shared_pending_locked(self, state, group):
        pending = state.setdefault("refresh_pending", [])
        if group not in pending:
            pending.append(group)

    def _schedule_shared_pending(self):
        group = None
        try:
            with self._mutex.hold(timeout_seconds=0):
                state = self._load_state()
                pending = state.get("refresh_pending", [])
                if pending:
                    group = pending.pop(0)
                    state["refresh_pending"] = pending
                    _atomic_json(self.state_path, state)
        except (OSError, TimeoutError):
            return
        if group is None:
            return
        if self.request_refresh(group):
            return
        try:
            with self._mutex.hold(timeout_seconds=0):
                state = self._load_state()
                self._queue_shared_pending_locked(state, group)
                _atomic_json(self.state_path, state)
        except (OSError, TimeoutError):
            pass

    def _eligible(self, state, group):
        current = state.get("current")
        group_state = state.get("groups", {}).get(group, {})
        if not current or current.get("status") != "verified":
            return False
        if not group_state.get("eligible"):
            return False
        if group_state.get("generation_id") != current.get("generation_id"):
            return False
        try:
            captured_at = _parse_time(current.get("captured_at"))
        except RuntimeStateError:
            return False
        age = (datetime.now(timezone.utc) - captured_at).total_seconds()
        return 0 <= age <= self.freshness_seconds

    def _candidate_for_current(self, state):
        current = state.get("current")
        if not isinstance(current, dict):
            raise RuntimeStateError("current generation missing")
        candidate = Path(current.get("candidate_path", "")).resolve()
        runtime_root = self.runtime_dir.resolve()
        try:
            candidate.relative_to(runtime_root)
        except ValueError as error:
            raise RuntimeStateError("candidate escapes runtime directory") from error
        if not candidate.is_file():
            raise RuntimeStateError("current candidate missing")
        if current.get("schema_checksum") != SCHEMA_CHECKSUM:
            raise RuntimeStateError("schema checksum mismatch")
        if current.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeStateError("schema version mismatch")
        return current, candidate

    @staticmethod
    def _rows_json(repository, query, args):
        return [json.loads(row[0]) for row in repository.rows(query, args)]

    def _normalized_response(self, repository, run_id, route):
        if route == "api/master":
            meta = repository.rows(
                "SELECT raw_json FROM master_meta WHERE run_id=?", (run_id,)
            )
            if len(meta) != 1:
                raise RuntimeStateError("normalized master meta missing")
            return _json_bytes({
                "meta": json.loads(meta[0][0]),
                "clients": self._rows_json(
                    repository,
                    "SELECT raw_json FROM master_clients WHERE run_id=? ORDER BY position",
                    (run_id,),
                ),
                "schedule": self._rows_json(
                    repository,
                    "SELECT raw_json FROM schedule_slots WHERE run_id=? ORDER BY position",
                    (run_id,),
                ),
            })
        if route == "client_database.json":
            meta = repository.rows(
                "SELECT last_updated FROM database_meta WHERE run_id=?", (run_id,)
            )
            if len(meta) != 1:
                raise RuntimeStateError("normalized database meta missing")
            return _json_bytes({
                "lastUpdated": meta[0][0],
                "clients": self._rows_json(
                    repository,
                    "SELECT raw_json FROM database_clients WHERE run_id=? ORDER BY position",
                    (run_id,),
                ),
                "schedule": self._rows_json(
                    repository,
                    "SELECT raw_json FROM database_schedule WHERE run_id=? ORDER BY position",
                    (run_id,),
                ),
            })
        if route == "api/settings":
            rows = repository.rows(
                "SELECT tunnel_port, auto_restart_tunnel, auto_telegram, auto_open_browser "
                "FROM public_settings WHERE run_id=?",
                (run_id,),
            )
            if len(rows) != 1:
                raise RuntimeStateError("normalized public settings missing")
            payload = {
                key: value for key, value in zip(PUBLIC_SETTINGS_FIELDS, rows[0])
                if value is not None
            }
            for key in PUBLIC_SETTINGS_FIELDS[1:]:
                if key in payload:
                    payload[key] = bool(payload[key])
            return _json_bytes(payload)
        raise RuntimeStateError("route is not SQLite allowlisted")

    def _normalize_and_verify(self, candidate, run_id, snapshot):
        repository = SQLiteCandidateRepository.open_existing(candidate)
        try:
            with repository.transaction():
                master = json.loads(snapshot.value("api/master").body.decode("utf-8"))
                if not isinstance(master, dict) or not isinstance(master.get("meta"), dict):
                    raise RuntimeStateError("master meta is not an object")
                repository.set_master_meta(run_id, _json_text(master["meta"]))
                for route, endpoint in (
                    ("api/master", "api/master"),
                    ("client_database.json", "client_database.json"),
                    ("api/settings", "api/settings"),
                ):
                    actual = self._normalized_response(repository, run_id, route)
                    expected = snapshot.value(endpoint).body
                    if _canonical_bytes(endpoint, actual) != _canonical_bytes(endpoint, expected):
                        raise RuntimeStateError("normalized route parity failed")
        finally:
            repository.close()

    def _read_generation(self, state, route):
        _group, endpoint = ROUTE_ENDPOINTS[route]
        current, candidate = self._candidate_for_current(state)
        repository = SQLiteCandidateRepository.open_read_only(candidate)
        try:
            schema = repository.rows(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            if schema != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
                raise RuntimeStateError("stored schema validation failed")
            run = repository.rows(
                "SELECT snapshot_id, source_hash, status, completed_at "
                "FROM import_runs WHERE run_id=?",
                (current.get("run_id"),),
            )
            if len(run) != 1 or run[0][2] != "verified":
                raise RuntimeStateError("import receipt is not verified")
            if run[0][1] != current.get("source_hash"):
                raise RuntimeStateError("stored source hash mismatch")
            if run[0][0] != current.get("snapshot_id"):
                raise RuntimeStateError("stored snapshot identity mismatch")
            snapshots = repository.rows(
                "SELECT position, endpoint, raw_bytes, raw_sha256, canonical_sha256 "
                "FROM source_snapshots WHERE run_id=? ORDER BY position",
                (current.get("run_id"),),
            )
            if not snapshots:
                raise RuntimeStateError("source snapshots missing")
            material = []
            endpoint_hashes = {}
            for _position, source_endpoint, raw_bytes, raw_hash, canonical_hash in snapshots:
                if not isinstance(raw_bytes, (bytes, bytearray, memoryview)):
                    raise RuntimeStateError("stored source snapshot is not bytes")
                raw_bytes = bytes(raw_bytes)
                if hashlib.sha256(raw_bytes).hexdigest() != raw_hash:
                    raise RuntimeStateError("stored raw source hash mismatch")
                try:
                    calculated_canonical = hashlib.sha256(
                        _canonical_bytes(source_endpoint, raw_bytes)
                    ).hexdigest()
                except Exception as error:
                    raise RuntimeStateError("stored canonical source is invalid") from error
                if calculated_canonical != canonical_hash:
                    raise RuntimeStateError("stored canonical source hash mismatch")
                endpoint_hashes[source_endpoint] = canonical_hash
                material.append(
                    source_endpoint.encode("utf-8") + b"\0" + canonical_hash.encode("ascii")
                )
            if hashlib.sha256(b"".join(material)).hexdigest() != current.get("source_hash"):
                raise RuntimeStateError("stored source hash material mismatch")
            body = self._normalized_response(repository, current.get("run_id"), route)
            expected_hash = endpoint_hashes.get(endpoint)
            if expected_hash is None or _sha256(_canonical_bytes(endpoint, body)) != expected_hash:
                raise RuntimeStateError("normalized response integrity mismatch")
            content = repository.rows(
                "SELECT content_type FROM source_snapshots WHERE run_id=? AND endpoint=?",
                (current.get("run_id"), endpoint),
            )
            if len(content) != 1:
                raise RuntimeStateError("route content type missing")
            return body, 200, content[0][0] or "application/json"
        finally:
            repository.close()

    def read(self, route, fallback):
        """Return a complete normalized SQLite response or invoke HTTP fallback."""
        if route not in ROUTE_ENDPOINTS:
            return fallback()
        group = ROUTE_ENDPOINTS[route][0]
        if not self.configured_enabled(group):
            return fallback()
        state = self._load_state()
        if not self._eligible(state, group):
            self._mark_ineligible(group, "stale_or_not_eligible", timeout_seconds=0)
            self.request_refresh(group)
            return fallback()
        try:
            body, status, content_type = self._read_generation(state, route)
        except Exception:
            self._mark_ineligible(group, "candidate_read_failed", timeout_seconds=0)
            self.request_refresh(group)
            return fallback()
        return body, status, content_type

    @staticmethod
    def _fence_observed(fence, snapshot):
        expected = fence.get("expected_observation")
        if not isinstance(expected, dict):
            return False
        if fence.get("expected_revision") != _sha256(_json_bytes(expected)):
            return False
        endpoint = expected.get("endpoint")
        try:
            payload = json.loads(snapshot.value(endpoint).body.decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        if endpoint == "api/master":
            if not isinstance(payload, dict):
                return False
            master_clients = {
                item.get("client"): item.get("name")
                for item in payload.get("clients", [])
                if isinstance(item, dict)
            }
            try:
                database = json.loads(
                    snapshot.value("client_database.json").body.decode("utf-8")
                )
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError):
                return False
            if not isinstance(database, dict):
                return False
            database_clients = {
                item.get("client"): item.get("name")
                for item in database.get("clients", [])
                if isinstance(item, dict)
            }
            changes = expected.get("changes")
            return isinstance(changes, list) and all(
                isinstance(change, dict)
                and master_clients.get(change.get("client")) == change.get("name")
                and database_clients.get(change.get("client")) == change.get("name")
                for change in changes
            )
        if endpoint == "api/settings":
            if not isinstance(payload, dict):
                return False
            fields = expected.get("fields")
            return isinstance(fields, dict) and all(
                payload.get(key) == value for key, value in fields.items()
            )
        return False

    @contextmanager
    def write_fence(self, group, expected_observation=None):
        """Fence stale reads until a later snapshot observes the completed write."""
        if group not in GROUPS:
            yield
            return
        expected = expected_observation or {}
        try:
            with self._mutex.hold():
                state = self._load_state()
                token = uuid.uuid4().hex
                self._mark_ineligible_locked(state, group, "write_invalidation")
                state["fences"][group] = {
                    "token": token,
                    "created_at": _utc_now(),
                    "reason": "pre_dispatch_write",
                    "expected_observation": expected,
                    "expected_revision": _sha256(_json_bytes(expected)),
                }
                _atomic_json(self.state_path, state)
                # The caller's upstream request executes while this mutex is held.
                yield
        except (OSError, TimeoutError) as error:
            raise UpstreamError(
                503, b'{"error":"sqlite generation fence unavailable"}'
            ) from error
        finally:
            # A crash skips this trigger but cannot lose the durable fence.
            self.request_refresh(group)

    def startup_refresh(self):
        """Queue refreshes for every enabled group without an eligible generation."""
        started = []
        state = self._load_state()
        for group in GROUPS:
            if self.configured_enabled(group) and not self._eligible(state, group):
                started.append((group, self.request_refresh(group)))
        return started

    def request_refresh(self, group):
        if not self.configured_enabled(group) or not self._background_refresh:
            return False
        with self._refresh_guard:
            self._refresh_pending.add(group)
            if self._refresh_active:
                return True
            self._refresh_active = True
        thread = threading.Thread(
            target=self._run_background_refresh,
            name="sqlite-refresh",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._refresh_guard:
                self._refresh_pending.discard(group)
                self._refresh_active = False
            return False
        return True

    def _run_background_refresh(self):
        while True:
            with self._refresh_guard:
                if not self._refresh_pending:
                    self._refresh_active = False
                    return
                group = next(iter(self._refresh_pending))
                self._refresh_pending.discard(group)
            try:
                self.refresh_now(group)
            except Exception:
                # Background refresh must never produce an uncaught thread error.
                self._mark_ineligible(group, "refresh_failed", timeout_seconds=0)

    def _build_candidate(self, group, staging, deadline):
        try:
            if self._source_factory is None:
                source = _BoundedAiToolHttpSource(deadline, self._clock)
            else:
                source = _DeadlineSource(
                    self._source_factory(), deadline, self._clock
                )
            snapshot = capture_stable_snapshot(source, max_passes=3)
            if self._clock() > deadline:
                raise SourceAcquisitionError("refresh timeout")
            receipt = self._importer(snapshot, staging)
            if receipt.status != "verified" or not receipt.checks.get("ok"):
                raise RuntimeStateError("candidate verification failed")
            self._normalize_and_verify(staging, receipt.run_id, snapshot)
            if self._clock() > deadline:
                raise SourceAcquisitionError("refresh timeout")
            return snapshot, receipt
        except Exception:
            _remove_candidate(staging)
            raise

    def _finish_timed_out_build(self, token, stop, heartbeat, staging, future):
        self._stop_lease_heartbeat(stop, heartbeat)
        _remove_candidate(staging)
        self._release_refresh_lease(token)
        self._schedule_shared_pending()

    def refresh_now(self, group, force=False):
        """Build outside the mutex and atomically publish only within the deadline."""
        if group not in GROUPS or not self.configured_enabled(group):
            return False
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        deadline = self._clock() + self.refresh_timeout_seconds
        generation_id = uuid.uuid4().hex
        staging = self.runtime_dir / (generation_id + ".sqlite3.staging")
        final = self.runtime_dir / (generation_id + ".sqlite3")
        published = False
        lease_token = None
        lease_stop = None
        heartbeat = None
        lease_release_deferred = False
        initial_generation_id = None
        initial_fence_token = None
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SourceAcquisitionError("refresh timeout")
            with self._mutex.hold(timeout_seconds=remaining):
                state = self._load_state()
                if not force and self._eligible(state, group):
                    return True
                if self._lease_active(state.get("refresh_lease")):
                    self._queue_shared_pending_locked(state, group)
                    _atomic_json(self.state_path, state)
                    # Another process owns the refresh; its publication is shared.
                    return True
                initial_generation_id = (state.get("current") or {}).get("generation_id")
                initial_fence_token = (state.get("fences", {}).get(group) or {}).get("token")
                lease_token = uuid.uuid4().hex
                state["refresh_lease"] = {
                    "token": lease_token,
                    "owner_pid": os.getpid(),
                    "started_at": _utc_now(),
                    "heartbeat_at": _utc_now(),
                    "expires_at": self._lease_expiry(),
                }
                _atomic_json(self.state_path, state)
            lease_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._refresh_lease_heartbeat,
                args=(lease_token, lease_stop),
                name="sqlite-refresh-lease",
                daemon=True,
            )
            heartbeat.start()

            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sqlite-build")
            future = executor.submit(self._build_candidate, group, staging, deadline)
            try:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise SourceAcquisitionError("refresh timeout")
                snapshot, receipt = future.result(timeout=remaining)
            except FutureTimeoutError as error:
                lease_release_deferred = True
                future.add_done_callback(
                    lambda completed: self._finish_timed_out_build(
                        lease_token, lease_stop, heartbeat, staging, completed
                    )
                )
                raise SourceAcquisitionError("refresh timeout") from error
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if self._clock() > deadline:
                raise SourceAcquisitionError("refresh timeout")
            self._stop_lease_heartbeat(lease_stop, heartbeat)

            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SourceAcquisitionError("refresh timeout")
            with self._mutex.hold(timeout_seconds=remaining):
                if self._clock() > deadline:
                    raise SourceAcquisitionError("refresh timeout")
                state = self._load_state()
                lease = state.get("refresh_lease") or {}
                if lease.get("token") != lease_token:
                    _remove_candidate(staging)
                    return False
                current_generation_id = (state.get("current") or {}).get("generation_id")
                current_fence_token = (state.get("fences", {}).get(group) or {}).get("token")
                if (
                    current_generation_id != initial_generation_id
                    or current_fence_token != initial_fence_token
                ):
                    _remove_candidate(staging)
                    if not self._eligible(state, group):
                        self.request_refresh(group)
                    return False
                fence = state.get("fences", {}).get(group)
                fence_observed = fence is not None and self._fence_observed(fence, snapshot)
                if fence is not None and not fence_observed:
                    raise RuntimeStateError("write mutation not observed")
                if not force and self._eligible(state, group):
                    _remove_candidate(staging)
                    return True
                os.replace(staging, final)
                published = True
                completed_at = _utc_now()
                current = {
                    "generation_id": generation_id,
                    "run_id": receipt.run_id,
                    "candidate_path": str(final),
                    "receipt_id": receipt.run_id,
                    "snapshot_id": receipt.snapshot_id,
                    "source_hash": receipt.source_hash,
                    "schema_version": SCHEMA_VERSION,
                    "schema_checksum": SCHEMA_CHECKSUM,
                    "status": "verified",
                    "captured_at": snapshot.captured_at,
                    "completed_at": completed_at,
                }
                state["current"] = current
                for target in GROUPS:
                    old = state["groups"].get(target, {})
                    target_fence = state.get("fences", {}).get(target)
                    if target_fence is not None and not (target == group and fence_observed):
                        state["groups"][target] = {
                            "eligible": False,
                            "generation_id": generation_id,
                            "reason": "write_invalidation",
                            "updated_at": completed_at,
                        }
                    elif target == group or old.get("eligible") or self.configured_enabled(target):
                        state["groups"][target] = {
                            "eligible": True,
                            "generation_id": generation_id,
                            "reason": "verified",
                            "updated_at": completed_at,
                        }
                    else:
                        state["groups"][target]["generation_id"] = generation_id
                if fence_observed:
                    state["fences"].pop(group, None)
                state["refresh_lease"] = None
                _atomic_json(self.state_path, state)
            self._schedule_shared_pending()
            return True
        except Exception:
            _remove_candidate(staging)
            if published:
                _remove_candidate(final)
            if not lease_release_deferred:
                self._stop_lease_heartbeat(lease_stop, heartbeat)
                if lease_token:
                    self._release_refresh_lease(lease_token)
            self._mark_ineligible(group, "refresh_failed", timeout_seconds=0)
            return False

    def state(self):
        """Return redacted runtime state for tests/diagnostics, never source data."""
        return self._load_state()


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
