"""Guarded SQLite generation publication, reads, refresh, and write fencing."""
from contextlib import contextmanager
from datetime import datetime, timezone
import ctypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import uuid

from repositories.aitool import UpstreamError
from repositories.sqlite import SCHEMA_CHECKSUM, SCHEMA_VERSION, SQLiteCandidateRepository
from services.sqlite_import import (
    AiToolHttpSource,
    SourceAcquisitionError,
    _canonical_bytes,
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
MUTEX_NAME = r"Local\WebsiteSQLiteGenerationMutex"

_FALLBACK_LOCKS = {}
_FALLBACK_LOCKS_GUARD = threading.Lock()


class RuntimeStateError(RuntimeError):
    """The current generation or runtime state cannot be trusted."""


class NamedGenerationMutex:
    """Explicit cross-process mutex; POSIX fallback is only for unit tests."""

    def __init__(self, name=MUTEX_NAME, timeout_seconds=15):
        self.name = name
        self.timeout_seconds = timeout_seconds
        with _FALLBACK_LOCKS_GUARD:
            self._fallback = _FALLBACK_LOCKS.setdefault(name, threading.Lock())

    @contextmanager
    def hold(self):
        if os.name != "nt":
            if not self._fallback.acquire(timeout=self.timeout_seconds):
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
                handle, int(self.timeout_seconds * 1000)
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
    path = Path(path)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _parse_time(value):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise RuntimeStateError("invalid generation timestamp") from error


class _DeadlineSource:
    """Stop starting new HTTP fetches after the coordinator deadline."""

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
        self._source_factory = source_factory or (lambda: AiToolHttpSource())
        self._importer = importer
        self._background_refresh = background_refresh
        self._refresh_guard = threading.Lock()
        self._refresh_active = False

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

    def _mark_ineligible(self, group, reason):
        try:
            with self._mutex.hold():
                state = self._load_state()
                self._mark_ineligible_locked(state, group, reason)
                _atomic_json(self.state_path, state)
        except (OSError, TimeoutError):
            # A failed state transition is itself fail-closed; HTTP remains source.
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
            completed_at = _parse_time(current.get("completed_at"))
        except RuntimeStateError:
            return False
        age = (datetime.now(timezone.utc) - completed_at).total_seconds()
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

    def _read_generation(self, state, route):
        _group, endpoint = ROUTE_ENDPOINTS[route]
        current, candidate = self._candidate_for_current(state)
        repository = SQLiteCandidateRepository.open_existing(candidate)
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
                material.append(
                    source_endpoint.encode("utf-8") + b"\0" + canonical_hash.encode("ascii")
                )
            if hashlib.sha256(b"".join(material)).hexdigest() != current.get("source_hash"):
                raise RuntimeStateError("stored source hash material mismatch")
            rows = repository.rows(
                "SELECT content_type, raw_bytes FROM source_snapshots "
                "WHERE run_id=? AND endpoint=?",
                (current.get("run_id"), endpoint),
            )
            if len(rows) != 1:
                raise RuntimeStateError("route source snapshot missing")
            content_type, body = rows[0]
            if not isinstance(body, (bytes, bytearray, memoryview)):
                raise RuntimeStateError("route source snapshot is not bytes")
            return bytes(body), content_type or "application/json"
        finally:
            repository.close()

    def read(self, route, fallback):
        """Return a complete SQLite response or invoke the typed HTTP fallback."""
        if route not in ROUTE_ENDPOINTS:
            return fallback()
        group = ROUTE_ENDPOINTS[route][0]
        if not self.configured_enabled(group):
            return fallback()
        state = self._load_state()
        if not self._eligible(state, group):
            self._mark_ineligible(group, "stale_or_not_eligible")
            self.request_refresh(group)
            return fallback()
        try:
            body, content_type = self._read_generation(state, route)
        except Exception:
            self._mark_ineligible(group, "candidate_read_failed")
            self.request_refresh(group)
            return fallback()
        return body, 200, content_type

    @contextmanager
    def write_fence(self, group):
        """Durably fence stale reads before the caller dispatches its write."""
        if group not in GROUPS:
            yield
            return
        try:
            with self._mutex.hold():
                state = self._load_state()
                token = uuid.uuid4().hex
                self._mark_ineligible_locked(state, group, "write_invalidation")
                state["fences"][group] = {
                    "token": token,
                    "created_at": _utc_now(),
                    "reason": "pre_dispatch_write",
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

    def request_refresh(self, group):
        if not self.configured_enabled(group) or not self._background_refresh:
            return False
        with self._refresh_guard:
            if self._refresh_active:
                return False
            self._refresh_active = True
        thread = threading.Thread(
            target=self._run_background_refresh,
            args=(group,),
            name="sqlite-refresh",
            daemon=True,
        )
        try:
            thread.start()
        except Exception:
            with self._refresh_guard:
                self._refresh_active = False
            return False
        return True

    def _run_background_refresh(self, group):
        try:
            self.refresh_now(group)
        finally:
            with self._refresh_guard:
                self._refresh_active = False

    def refresh_now(self, group, force=False):
        """Build and publish one verified generation, never a partial candidate."""
        if group not in GROUPS or not self.configured_enabled(group):
            return False
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        staging = None
        with self._mutex.hold():
            state = self._load_state()
            if not force and self._eligible(state, group):
                return True
            generation_id = uuid.uuid4().hex
            staging = self.runtime_dir / (generation_id + ".sqlite3.staging")
            final = self.runtime_dir / (generation_id + ".sqlite3")
            deadline = self._clock() + self.refresh_timeout_seconds
            try:
                source = _DeadlineSource(
                    self._source_factory(), deadline, self._clock
                )
                snapshot = capture_stable_snapshot(source, max_passes=3)
                if self._clock() > deadline:
                    raise SourceAcquisitionError("refresh timeout")
                receipt = self._importer(snapshot, staging)
                if receipt.status != "verified" or not receipt.checks.get("ok"):
                    raise RuntimeStateError("candidate verification failed")
                if self._clock() > deadline:
                    raise SourceAcquisitionError("refresh timeout")
                os.replace(staging, final)
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
                    "completed_at": completed_at,
                }
                state["current"] = current
                for target in GROUPS:
                    old = state["groups"].get(target, {})
                    if target == group or old.get("eligible"):
                        state["groups"][target] = {
                            "eligible": True,
                            "generation_id": generation_id,
                            "reason": "verified",
                            "updated_at": completed_at,
                        }
                    else:
                        state["groups"][target]["generation_id"] = generation_id
                state["fences"].pop(group, None)
                _atomic_json(self.state_path, state)
                return True
            except Exception:
                if staging is not None:
                    _remove_candidate(staging)
                state = self._load_state()
                self._mark_ineligible_locked(state, group, "refresh_failed")
                _atomic_json(self.state_path, state)
                return False

    def state(self):
        """Return redacted runtime state for tests/diagnostics, never source data."""
        return self._load_state()
