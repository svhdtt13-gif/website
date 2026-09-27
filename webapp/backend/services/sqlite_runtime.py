"""Guarded SQLite generation publication, normalized reads, and write fencing."""
import ctypes
import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol

from repositories.aitool import UpstreamError, ai_tool
from repositories.legacy_authority_store import (
    LegacyAuthorityStore,
    LegacyAuthorityStoreError,
    TransitionError,
)
from repositories.sqlite import (
    SCHEMA_CHECKSUM,
    SCHEMA_VERSION,
    SQLiteCandidateRepository,
)

from services import settings as settings_service
from services.legacy_observation_materializer import (
    ObservationMaterializer,
    ObservationMaterializerError,
    VerifiedRuntimeSnapshot,
)
from services.runtime_observation_generation import (
    DurableObservationBoundaryProvider,
    ObservationGenerationContractError,
    TrustedObservationBoundary,
    require_non_secret_source_identity,
)
from services.runtime_observation_ledger import (
    ObservationLedgerError,
    PublicationParity,
    PublicationRecord,
    PublicationReservation,
    RuntimeObservationPublicationLedger,
    ledger_path,
)
from services.sqlite_import import (
    PUBLIC_SETTINGS_FIELDS,
    SOURCE_SET_WAVE1,
    WAVE1_RUNTIME_SOURCE_ORDER,
    AiToolHttpSource,
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


class LegacyAuthorityStoreOpener(Protocol):
    """Open a fresh LEGACY authority connection for one runtime lookup."""

    def __call__(self) -> LegacyAuthorityStore: ...

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
            self._fallback = _FALLBACK_LOCKS.setdefault(name, threading.RLock())

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
        legacy_authority_store_factory: LegacyAuthorityStoreOpener | None = None,
        trusted_source_identity=None,
        test_only_observation_boundary_provider=None,
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
        if legacy_authority_store_factory is not None and not callable(
            legacy_authority_store_factory
        ):
            raise RuntimeStateError("LEGACY store factory is not callable")
        if legacy_authority_store_factory is not None and test_only_observation_boundary_provider is not None:
            raise RuntimeStateError("production and test boundary providers cannot mix")
        self._legacy_authority_store_factory = legacy_authority_store_factory
        self._test_only_observation_boundary_provider = test_only_observation_boundary_provider
        if trusted_source_identity is not None:
            try:
                require_non_secret_source_identity(trusted_source_identity)
            except ObservationGenerationContractError as error:
                raise RuntimeStateError(
                    "trusted runtime source identity is invalid"
                ) from error
        self._trusted_source_identity = trusted_source_identity
        self.ledger = RuntimeObservationPublicationLedger(
            ledger_path(self.runtime_dir), trusted_source_identity=trusted_source_identity
        )
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

    def _verify_publication_candidate(
        self,
        publication: PublicationRecord,
        expected_completed_at: str | None = None,
        candidate_path: str | None = None,
    ):
        candidate = Path(candidate_path or publication.candidate_path).resolve()
        runtime_root = self.runtime_dir.resolve()
        try:
            candidate.relative_to(runtime_root)
        except ValueError as error:
            raise RuntimeStateError("publication candidate escapes runtime directory") from error
        if not candidate.is_file():
            raise RuntimeStateError("publication candidate is missing")
        repository = SQLiteCandidateRepository.open_read_only(candidate)
        verified_target = None
        try:
            schema = repository.rows(
                "SELECT version, checksum FROM schema_migrations ORDER BY version"
            )
            if schema != [(SCHEMA_VERSION, SCHEMA_CHECKSUM)]:
                raise RuntimeStateError("publication candidate schema mismatch")
            row = repository.rows(
                "SELECT snapshot_id, source_hash, status, checks_json, "
                "observation_generation, publication_generation_id, "
                "observation_source_set, source_identity_ref, observation_boundary_id, "
                "observation_generation_floor, observation_captured_at, "
                "observation_completed_at, observation_pre_send_identity, "
                "observation_canary_run_id, observation_fence_identity, "
                "observation_authority_epoch, observation_fence_counter, "
                "observation_target_ref, observation_requested_state, "
                "observation_binding_fingerprint, observation_materializer_run_id, "
                "observation_observed_state, observation_attestation_fingerprint "
                "FROM import_runs WHERE run_id=?",
                (publication.run_id,),
            )
            if len(row) != 1 or row[0][2] != "verified":
                raise RuntimeStateError("publication candidate is not verified")
            if (
                row[0][0] != publication.snapshot_id
                or row[0][1] != publication.source_hash
                or row[0][4] != publication.observation_generation
                or row[0][5] != publication.generation_id
                or row[0][6] != publication.source_set
                or row[0][7] != publication.source_identity_ref
                or row[0][8] != publication.observation_boundary_id
                or row[0][9] != publication.observation_generation_floor
                or row[0][10] != publication.captured_at
                or row[0][11]
                != (
                    publication.completed_at
                    if expected_completed_at is None
                    else expected_completed_at
                )
                or row[0][12] != publication.pre_send_identity
                or row[0][13] != publication.canary_run_id
                or row[0][14] != publication.fence_identity
                or row[0][15] != publication.authority_epoch
                or row[0][16] != publication.fence_counter
                or row[0][17] != publication.target_ref
                or row[0][18] != publication.requested_state
                or row[0][19] != publication.observation_binding_fingerprint
                or row[0][20] != publication.materializer_run_id
                or row[0][21] != publication.observed_state
            ):
                raise RuntimeStateError("publication candidate metadata mismatch")
            checks = json.loads(row[0][3])
            nested = checks.get("checks") if isinstance(checks, dict) else None
            if (
                not isinstance(checks, dict)
                or checks.get("source_set") != publication.source_set
                or not isinstance(nested, dict)
                or nested.get("source_set") != publication.source_set
            ):
                raise RuntimeStateError("publication candidate source set mismatch")
            if publication.observation_boundary_id is not None:
                verified_target = repository.observed_state_for_target(
                    publication.run_id,
                    publication.target_ref,
                    publication.requested_state,
                )
                if verified_target.observed_state != publication.observed_state:
                    raise RuntimeStateError("publication target observation mismatch")
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeStateError("publication candidate metadata is invalid") from error
        finally:
            repository.close()
        return verified_target

    def _state_from_publication(self, publication: PublicationRecord, state):
        projection = state if isinstance(state, dict) and state.get("version") == 1 else _default_state()
        current = {
            "generation_id": publication.generation_id,
            "run_id": publication.run_id,
            "candidate_path": publication.candidate_path,
            "receipt_id": publication.run_id,
            "snapshot_id": publication.snapshot_id,
            "source_hash": publication.source_hash,
            "source_set": publication.source_set,
            "source_identity_ref": publication.source_identity_ref,
            "pre_send_identity": publication.pre_send_identity,
            "canary_run_id": publication.canary_run_id,
            "fence_identity": publication.fence_identity,
            "authority_epoch": publication.authority_epoch,
            "fence_counter": publication.fence_counter,
            "target_ref": publication.target_ref,
            "requested_state": publication.requested_state,
            "schema_version": publication.candidate_schema_version,
            "schema_checksum": publication.candidate_schema_checksum,
            "status": "verified",
            "captured_at": publication.captured_at,
            "completed_at": publication.completed_at,
            "observation_generation": publication.observation_generation,
            "publication_generation_id": publication.generation_id,
            "observation_boundary_id": publication.observation_boundary_id,
            "observation_generation_floor": publication.observation_generation_floor,
            "observation_binding_fingerprint": publication.observation_binding_fingerprint,
            "materializer_run_id": publication.materializer_run_id,
            "observed_state": publication.observed_state,
            "attestation_fingerprint": publication.attestation_fingerprint,
        }
        projection["current"] = current
        for group in GROUPS:
            group_state = projection["groups"].get(group, {})
            if (
                self.configured_enabled(group)
                and group_state.get("eligible")
                and group not in projection.get("fences", {})
            ):
                projection["groups"][group] = {
                    "eligible": True,
                    "generation_id": publication.generation_id,
                    "reason": "verified",
                    "updated_at": publication.completed_at,
                }
        return projection

    @staticmethod
    def _projection_matches_publication(state, publication):
        current = state.get("current")
        if not isinstance(current, dict):
            return False
        return (
            current.get("observation_generation") == publication.observation_generation
            and current.get("generation_id") == publication.generation_id
            and current.get("snapshot_id") == publication.snapshot_id
            and current.get("run_id") == publication.run_id
            and current.get("candidate_path") == publication.candidate_path
            and current.get("source_hash") == publication.source_hash
            and current.get("source_set") == publication.source_set
            and current.get("source_identity_ref") == publication.source_identity_ref
            and current.get("pre_send_identity") == publication.pre_send_identity
            and current.get("canary_run_id") == publication.canary_run_id
            and current.get("fence_identity") == publication.fence_identity
            and current.get("authority_epoch") == publication.authority_epoch
            and current.get("fence_counter") == publication.fence_counter
            and current.get("target_ref") == publication.target_ref
            and current.get("requested_state") == publication.requested_state
            and current.get("captured_at") == publication.captured_at
            and current.get("completed_at") == publication.completed_at
            and current.get("observation_boundary_id") == publication.observation_boundary_id
            and current.get("observation_generation_floor") == publication.observation_generation_floor
            and current.get("observation_binding_fingerprint") == publication.observation_binding_fingerprint
            and current.get("materializer_run_id") == publication.materializer_run_id
            and current.get("observed_state") == publication.observed_state
            and current.get("attestation_fingerprint") == publication.attestation_fingerprint
            and current.get("schema_version") == publication.candidate_schema_version
            and current.get("schema_checksum") == publication.candidate_schema_checksum
            and current.get("status") == "verified"
        )

    def _ensure_generation_state(self, state, allow_reserved_recovery=False):
        try:
            publication = self.ledger.latest_available()
        except ObservationLedgerError as error:
            raise ObservationGenerationContractError(str(error)) from error
        if publication is not None and publication.state in ("finalized", "published"):
            try:
                self._verify_publication_candidate(publication)
                if (
                    publication.observation_boundary_id is not None
                    and self._legacy_authority_store_factory is None
                    and self._test_only_observation_boundary_provider is None
                ):
                    raise ObservationGenerationContractError(
                        "pending boundary requires authority verification"
                    )
                if publication.state == "finalized":
                    publication = self.ledger.publish_finalized(
                        publication.observation_generation
                    )
                if (
                    publication.observation_boundary_id is not None
                    and self._legacy_authority_store_factory is not None
                ):
                    publication = self._acknowledge_published_authority(publication)
            except (ObservationGenerationContractError, RuntimeStateError):
                if publication is not None:
                    pass
                elif not allow_reserved_recovery:
                    raise
                else:
                    for target in GROUPS:
                        self._mark_ineligible_locked(
                            state, target, "unproven_publication_not_recoverable"
                        )
                    return state
        if publication is not None:
            if publication.source_identity_ref != self.trusted_source_identity():
                raise ObservationGenerationContractError(
                    "ledger source identity does not match trusted runtime identity"
                )
            try:
                self._verify_publication_candidate(publication)
            except RuntimeStateError:
                if not allow_reserved_recovery:
                    raise
                for target in GROUPS:
                    self._mark_ineligible_locked(state, target, "published_candidate_unusable")
                return state
            if (
                publication.observation_boundary_id is not None
                and self._legacy_authority_store_factory is None
                and self._test_only_observation_boundary_provider is None
            ):
                raise ObservationGenerationContractError(
                    "boundary-bound publication requires authority verification"
                )
            if publication.observation_boundary_id is not None and self._legacy_authority_store_factory is not None:
                try:
                    self._verify_published_authority_ack(publication)
                    self._close_published_authority_fence(publication)
                except ObservationGenerationContractError:
                    if not allow_reserved_recovery:
                        raise
                    for target in GROUPS:
                        self._mark_ineligible_locked(state, target, "published_authority_ack_unavailable")
                    return state
            if not self._projection_matches_publication(state, publication):
                state = self._state_from_publication(publication, state)
                _atomic_json(self.state_path, state)
            return state
        if self.ledger.has_history() and allow_reserved_recovery:
            return state
        current = state.get("current")
        if isinstance(current, dict) and current.get("observation_generation") is not None:
            raise RuntimeStateError("published observation is absent from ledger")
        if self.ledger.has_history():
            raise RuntimeStateError("ledger has no published observation")
        if self.state_path.exists():
            try:
                with self.state_path.open("r", encoding="utf-8") as stream:
                    raw = json.load(stream)
            except (OSError, ValueError, TypeError) as error:
                raise RuntimeStateError("current projection is corrupt") from error
            if not isinstance(raw, dict) or raw.get("version") != 1:
                raise RuntimeStateError("current projection is invalid")
        return state

    @staticmethod
    def _receipt_matches_publication(receipt, publication: PublicationRecord) -> bool:
        return (
            receipt.pre_send_identity == publication.pre_send_identity
            and receipt.canary_run_id == publication.canary_run_id
            and receipt.fence_identity == publication.fence_identity
            and receipt.authority_epoch == publication.authority_epoch
            and receipt.fence_counter == publication.fence_counter
            and receipt.source_identity_ref == publication.source_identity_ref
            and receipt.target_ref == publication.target_ref
            and receipt.requested_state == publication.requested_state
            and receipt.post_dispatch_observation_boundary_id
            == publication.observation_boundary_id
            and receipt.post_dispatch_observation_generation_floor
            == publication.observation_generation_floor
        )

    def _record_published_authority_ack(
        self, publication: PublicationRecord
    ) -> str:
        if (
            self._legacy_authority_store_factory is None
            or publication.pre_send_identity is None
            or publication.materializer_run_id is None
        ):
            raise ObservationGenerationContractError(
                "published authority binding is unavailable"
            )
        store = self._legacy_authority_store_factory()
        try:
            current_receipt = store.current_observation_receipt()
            if (
                current_receipt is None
                or not self._receipt_matches_publication(current_receipt, publication)
            ):
                raise ObservationGenerationContractError(
                    "published authority lineage changed"
                )
            verified_target = self._verify_publication_candidate(publication)
            if verified_target is None:
                raise ObservationGenerationContractError(
                    "published target observation is unavailable"
                )
            materializer = ObservationMaterializer(store)
            receipt, evidence = materializer.prepare_verified_runtime_snapshot(
                publication.pre_send_identity,
                VerifiedRuntimeSnapshot(
                    snapshot_generation_id=publication.observation_generation,
                    snapshot_id=publication.snapshot_id,
                    captured_at=publication.captured_at,
                    source_hash=publication.source_hash,
                    target_observation=verified_target,
                    materializer_run_id=publication.materializer_run_id,
                ),
            )
            materializer.record_ack(receipt, evidence)
        except (
            LegacyAuthorityStoreError,
            ObservationMaterializerError,
            TransitionError,
        ) as error:
            raise ObservationGenerationContractError(
                "published authority evidence was rejected"
            ) from error
        finally:
            store.close()
        return self._verify_published_authority_ack(publication)

    def _acknowledge_published_authority(
        self, publication: PublicationRecord
    ) -> PublicationRecord:
        try:
            attestation_fingerprint = self._verify_published_authority_ack(publication)
        except ObservationGenerationContractError:
            attestation_fingerprint = self._record_published_authority_ack(publication)
        acknowledged = self.ledger.acknowledge(
            publication.observation_generation, attestation_fingerprint
        )
        self._close_published_authority_fence(acknowledged)
        return acknowledged

    def _close_published_authority_fence(
        self, publication: PublicationRecord
    ) -> None:
        if (
            self._legacy_authority_store_factory is None
            or publication.pre_send_identity is None
        ):
            raise ObservationGenerationContractError(
                "published authority binding is unavailable"
            )
        store = self._legacy_authority_store_factory()
        try:
            receipt = store.get_receipt(publication.pre_send_identity)
            if not self._receipt_matches_publication(receipt, publication):
                raise ObservationGenerationContractError(
                    "published authority lineage changed"
                )
            fence = store.get_fence(publication.pre_send_identity)
            if fence is None:
                raise ObservationGenerationContractError(
                    "published authority fence is unavailable"
                )
            if fence["state"] != "closed":
                store.close_fence(publication.pre_send_identity)
        except (LegacyAuthorityStoreError, TransitionError) as error:
            raise ObservationGenerationContractError(
                "published authority fence could not close"
            ) from error
        finally:
            store.close()

    def _verify_published_authority_ack(self, publication: PublicationRecord) -> str:
        if self._legacy_authority_store_factory is None or publication.pre_send_identity is None:
            raise ObservationGenerationContractError("published authority binding is unavailable")
        store = self._legacy_authority_store_factory()
        try:
            receipt = store.get_receipt(publication.pre_send_identity)
            ack = ObservationMaterializer(store).require_evidence(receipt)
        except (
            LegacyAuthorityStoreError,
            ObservationMaterializerError,
            TransitionError,
        ) as error:
            raise ObservationGenerationContractError(
                "published authority evidence is unavailable"
            ) from error
        finally:
            store.close()
        evidence = ack.evidence
        if (
            receipt.authority_epoch != publication.authority_epoch
            or receipt.pre_send_identity != publication.pre_send_identity
            or evidence.snapshot_generation_id != publication.observation_generation
            or evidence.snapshot_id != publication.snapshot_id
            or evidence.captured_at != publication.captured_at
            or evidence.materializer_run_id != publication.materializer_run_id
            or evidence.source_hash != publication.source_hash
            or evidence.source_identity_ref != publication.source_identity_ref
            or evidence.boundary_id != publication.observation_boundary_id
            or evidence.generation_floor != publication.observation_generation_floor
            or evidence.canary_run_id != publication.canary_run_id
            or evidence.fence_identity != publication.fence_identity
            or evidence.fence_counter != publication.fence_counter
            or evidence.target_ref != publication.target_ref
            or evidence.requested_state != publication.requested_state
            or evidence.observed_state != publication.observed_state
            or (
                publication.attestation_fingerprint is not None
                and evidence.attestation_fingerprint
                != publication.attestation_fingerprint
            )
        ):
            raise ObservationGenerationContractError(
                "published authority evidence does not match runtime publication"
            )
        return evidence.attestation_fingerprint

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
            return "released"
        try:
            with self._mutex.hold(timeout_seconds=0):
                state = self._load_state()
                lease = state.get("refresh_lease") or {}
                if lease.get("token") != token:
                    return "not_owner"
                state["refresh_lease"] = None
                _atomic_json(self.state_path, state)
                return "released"
        except (OSError, TimeoutError):
            return "busy"

    def _handoff_owner_lease(self, token, stop, heartbeat, follow_up=None):
        """Keep the owner heartbeat alive until the lease can be cleared."""
        def complete():
            self._stop_lease_heartbeat(stop, heartbeat)
            if follow_up is not None:
                follow_up()
            self._schedule_shared_pending()

        def retry():
            while True:
                try:
                    status = self._release_refresh_lease(token)
                except Exception:
                    self._stop_lease_heartbeat(stop, heartbeat)
                    return
                if status == "released":
                    complete()
                    return
                if status == "not_owner":
                    self._stop_lease_heartbeat(stop, heartbeat)
                    return
                time.sleep(0.05)

        status = self._release_refresh_lease(token)
        if status == "released":
            complete()
            return True
        if status == "not_owner":
            self._stop_lease_heartbeat(stop, heartbeat)
            return False
        threading.Thread(
            target=retry,
            name="sqlite-lease-handoff",
            daemon=True,
        ).start()
        return False

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
        if current.get("source_set") != SOURCE_SET_WAVE1:
            return False
        if group in state.get("fences", {}):
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
                "SELECT snapshot_id, source_hash, status, checks_json, "
                "observation_generation, observation_boundary_id, "
                "observation_generation_floor, observation_captured_at, "
                "observation_completed_at "
                "FROM import_runs WHERE run_id=?",
                (current.get("run_id"),),
            )
            if len(run) != 1 or run[0][2] != "verified":
                raise RuntimeStateError("import receipt is not verified")
            if run[0][1] != current.get("source_hash"):
                raise RuntimeStateError("stored source hash mismatch")
            if run[0][0] != current.get("snapshot_id"):
                raise RuntimeStateError("stored snapshot identity mismatch")
            if (
                type(run[0][4]) is not int
                or run[0][4] < 1
                or run[0][4] != current.get("observation_generation")
                or run[0][5] != current.get("observation_boundary_id")
                or run[0][6] != current.get("observation_generation_floor")
                or run[0][7] != current.get("captured_at")
                or run[0][8] != current.get("completed_at")
            ):
                raise RuntimeStateError("observation generation metadata mismatch")
            if run[0][5] is None and run[0][6] is not None:
                raise RuntimeStateError("observation boundary metadata is incomplete")
            if run[0][5] is not None and (
                type(run[0][6]) is not int or run[0][4] <= run[0][6]
            ):
                raise RuntimeStateError("observation generation is not later than boundary")
            try:
                checks = json.loads(run[0][3])
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeStateError("stored import checks are invalid") from error
            nested_checks = checks.get("checks") if isinstance(checks, dict) else None
            if (
                not isinstance(checks, dict)
                or checks.get("source_set") != SOURCE_SET_WAVE1
                or not isinstance(nested_checks, dict)
                or nested_checks.get("source_set") != SOURCE_SET_WAVE1
            ):
                raise RuntimeStateError("stored import source set mismatch")
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
        stale = False
        response = None
        try:
            # Nonblocking lock acquisition gives reads a clear linearization point
            # with write fences without waiting behind refresh or upstream writes.
            with self._mutex.hold(timeout_seconds=0):
                state = self._ensure_generation_state(self._load_state())
                if not self._eligible(state, group):
                    stale = True
                else:
                    response = self._read_generation(state, route)
        except (OSError, TimeoutError):
            return fallback()
        except Exception:
            self._mark_ineligible(group, "candidate_read_failed", timeout_seconds=0)
            self.request_refresh(group)
            return fallback()
        if stale:
            self._mark_ineligible(group, "stale_or_not_eligible", timeout_seconds=0)
            self.request_refresh(group)
            return fallback()
        return response

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
            snapshot = capture_stable_snapshot(
                source,
                max_passes=3,
                source_order=WAVE1_RUNTIME_SOURCE_ORDER,
                source_set=SOURCE_SET_WAVE1,
            )
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
        _remove_candidate(staging)
        self._handoff_owner_lease(token, stop, heartbeat)

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
        head_committed = False
        lease_token = None
        lease_stop = None
        heartbeat = None
        lease_release_deferred = False
        lease_owned = False
        lease_released = False
        initial_generation_id = None
        initial_fence_token = None
        initial_boundary = None
        requeue_group = False
        reservation = None
        try:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SourceAcquisitionError("refresh timeout")
            with self._mutex.hold(timeout_seconds=remaining):
                state = self._ensure_generation_state(
                    self._load_state(), allow_reserved_recovery=True
                )
                if not force and self._eligible(state, group):
                    return True
                if self._lease_active(state.get("refresh_lease")):
                    self._queue_shared_pending_locked(state, group)
                    _atomic_json(self.state_path, state)
                    # Another process owns the refresh; its publication is shared.
                    return True
                initial_generation_id = (state.get("current") or {}).get("generation_id")
                initial_fence_token = (state.get("fences", {}).get(group) or {}).get("token")
                initial_boundary = self._current_observation_boundary()
                lease_token = uuid.uuid4().hex
                state["refresh_lease"] = {
                    "token": lease_token,
                    "owner_pid": os.getpid(),
                    "started_at": _utc_now(),
                    "heartbeat_at": _utc_now(),
                    "expires_at": self._lease_expiry(),
                }
                _atomic_json(self.state_path, state)
                lease_owned = True
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

            def defer_timed_out_build():
                nonlocal lease_release_deferred
                lease_release_deferred = True
                future.add_done_callback(
                    lambda completed: self._finish_timed_out_build(
                        lease_token, lease_stop, heartbeat, staging, completed
                    )
                )

            try:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    defer_timed_out_build()
                    raise SourceAcquisitionError("refresh timeout")
                snapshot, receipt = future.result(timeout=remaining)
            except FutureTimeoutError as error:
                defer_timed_out_build()
                raise SourceAcquisitionError("refresh timeout") from error
            finally:
                executor.shutdown(wait=False, cancel_futures=True)
            if self._clock() > deadline:
                raise SourceAcquisitionError("refresh timeout")

            remaining = deadline - self._clock()
            if remaining <= 0:
                raise SourceAcquisitionError("refresh timeout")
            result = None
            with self._mutex.hold(timeout_seconds=remaining):
                if self._clock() > deadline:
                    raise SourceAcquisitionError("refresh timeout")
                state = self._ensure_generation_state(
                    self._load_state(), allow_reserved_recovery=True
                )
                lease = state.get("refresh_lease") or {}
                if lease.get("token") != lease_token:
                    _remove_candidate(staging)
                    lease_owned = False
                    requeue_group = not self._eligible(state, group)
                    result = False
                else:
                    current_generation_id = (state.get("current") or {}).get("generation_id")
                    current_fence_token = (state.get("fences", {}).get(group) or {}).get("token")
                    if (
                        current_generation_id != initial_generation_id
                        or current_fence_token != initial_fence_token
                    ):
                        _remove_candidate(staging)
                        requeue_group = not self._eligible(state, group)
                        result = False
                    elif not force and self._eligible(state, group):
                        _remove_candidate(staging)
                        result = True
                    else:
                        fence = state.get("fences", {}).get(group)
                        fence_observed = fence is not None and self._fence_observed(fence, snapshot)
                        if fence is not None and not fence_observed:
                            raise RuntimeStateError("write mutation not observed")
                        boundary = self._current_observation_boundary()
                        if boundary != initial_boundary:
                            raise ObservationGenerationContractError(
                                "observation boundary changed during capture"
                            )
                        source_identity = self.trusted_source_identity()
                        if (
                            boundary is not None
                            and boundary.source_identity_ref != source_identity
                        ):
                            raise ObservationGenerationContractError(
                                "observation boundary source identity is untrusted"
                            )
                        completed_at = _utc_now()
                        binding_pre_send_identity = None if boundary is None else boundary.pre_send_identity
                        binding_canary_run_id = None if boundary is None else boundary.canary_run_id
                        binding_fence_identity = None if boundary is None else boundary.fence_identity
                        binding_authority_epoch = None if boundary is None else boundary.authority_epoch
                        binding_fence_counter = None if boundary is None else boundary.fence_counter
                        binding_target_ref = None if boundary is None else boundary.target_ref
                        binding_requested_state = None if boundary is None else boundary.requested_state
                        binding_fingerprint = None if boundary is None else boundary.binding_fingerprint()
                        materializer_run_id = uuid.uuid4().hex
                        observed_state = None
                        verified_target_observation = None
                        if boundary is not None:
                            observed_state = boundary.requested_state
                            candidate_repository = SQLiteCandidateRepository.open_existing(staging)
                            try:
                                verified_target_observation = candidate_repository.observed_state_for_target(
                                    receipt.run_id,
                                    boundary.target_ref,
                                    boundary.requested_state,
                                )
                                observed_state = verified_target_observation.observed_state
                            finally:
                                candidate_repository.close()
                        reservation = self.ledger.reserve(
                            None if boundary is None else boundary.generation_floor,
                            PublicationReservation(
                                snapshot_id=receipt.snapshot_id,
                                run_id=receipt.run_id,
                                candidate_path=str(final),
                                candidate_schema_version=SCHEMA_VERSION,
                                candidate_schema_checksum=SCHEMA_CHECKSUM,
                                source_hash=receipt.source_hash,
                                source_set=SOURCE_SET_WAVE1,
                                source_identity_ref=source_identity,
                                pre_send_identity=binding_pre_send_identity,
                                canary_run_id=binding_canary_run_id,
                                fence_identity=binding_fence_identity,
                                authority_epoch=binding_authority_epoch,
                                fence_counter=binding_fence_counter,
                                target_ref=binding_target_ref,
                                requested_state=binding_requested_state,
                                captured_at=snapshot.captured_at,
                                observation_boundary_id=None if boundary is None else boundary.boundary_id,
                                observation_generation_floor=None if boundary is None else boundary.generation_floor,
                                observation_binding_fingerprint=binding_fingerprint,
                                materializer_run_id=materializer_run_id,
                                observed_state=observed_state,
                            ),
                        )
                        attestation_fingerprint = None
                        self._persist_observation_metadata(
                            staging,
                            receipt.run_id,
                            reservation,
                            boundary,
                            snapshot.captured_at,
                            completed_at,
                            materializer_run_id,
                            observed_state,
                            attestation_fingerprint,
                        )
                        self._verify_publication_candidate(
                            reservation, completed_at, str(staging)
                        )
                        os.replace(staging, final)
                        published = True
                        self._verify_publication_candidate(
                            reservation, completed_at
                        )
                        self.ledger.finalize(
                            reservation.observation_generation,
                            completed_at,
                            PublicationParity(
                                candidate_path=str(final),
                                candidate_schema_version=SCHEMA_VERSION,
                                candidate_schema_checksum=SCHEMA_CHECKSUM,
                                source_identity_ref=source_identity,
                                pre_send_identity=binding_pre_send_identity,
                                canary_run_id=binding_canary_run_id,
                                fence_identity=binding_fence_identity,
                                authority_epoch=binding_authority_epoch,
                                fence_counter=binding_fence_counter,
                                target_ref=binding_target_ref,
                                requested_state=binding_requested_state,
                                observation_binding_fingerprint=binding_fingerprint,
                                materializer_run_id=materializer_run_id,
                                observed_state=observed_state,
                                attestation_fingerprint=attestation_fingerprint,
                            ),
                        )
                        head_committed = True
                        publication = self.ledger.publish(
                            reservation.observation_generation,
                            completed_at,
                            PublicationParity(
                                candidate_path=str(final),
                                candidate_schema_version=SCHEMA_VERSION,
                                candidate_schema_checksum=SCHEMA_CHECKSUM,
                                source_identity_ref=source_identity,
                                pre_send_identity=binding_pre_send_identity,
                                canary_run_id=binding_canary_run_id,
                                fence_identity=binding_fence_identity,
                                authority_epoch=binding_authority_epoch,
                                fence_counter=binding_fence_counter,
                                target_ref=binding_target_ref,
                                requested_state=binding_requested_state,
                                observation_binding_fingerprint=binding_fingerprint,
                                materializer_run_id=materializer_run_id,
                                observed_state=observed_state,
                                attestation_fingerprint=attestation_fingerprint,
                            ),
                        )
                        self._verify_publication_candidate(publication)
                        if (
                            self._legacy_authority_store_factory is not None
                            and boundary is not None
                        ):
                            publication = self._acknowledge_published_authority(
                                publication
                            )
                            attestation_fingerprint = (
                                publication.attestation_fingerprint
                            )
                        generation_id = publication.generation_id
                        observation_generation = publication.observation_generation
                        current = {
                            "generation_id": generation_id,
                            "run_id": receipt.run_id,
                            "candidate_path": str(final),
                            "receipt_id": receipt.run_id,
                            "snapshot_id": receipt.snapshot_id,
                            "source_hash": receipt.source_hash,
                            "source_set": SOURCE_SET_WAVE1,
                            "source_identity_ref": source_identity,
                            "pre_send_identity": binding_pre_send_identity,
                            "canary_run_id": binding_canary_run_id,
                            "fence_identity": binding_fence_identity,
                            "authority_epoch": binding_authority_epoch,
                            "fence_counter": binding_fence_counter,
                            "target_ref": binding_target_ref,
                            "requested_state": binding_requested_state,
                            "schema_version": SCHEMA_VERSION,
                            "schema_checksum": SCHEMA_CHECKSUM,
                            "status": "verified",
                            "captured_at": snapshot.captured_at,
                            "completed_at": completed_at,
                            "observation_generation": observation_generation,
                            "observation_boundary_id": (
                                boundary.boundary_id if boundary is not None else None
                            ),
                            "observation_generation_floor": (
                                boundary.generation_floor if boundary is not None else None
                            ),
                            "observation_binding_fingerprint": binding_fingerprint,
                            "materializer_run_id": materializer_run_id,
                            "observed_state": observed_state,
                            "attestation_fingerprint": attestation_fingerprint,
                            "publication_generation_id": generation_id,
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
                        lease_owned = False
                        lease_released = True
                        _atomic_json(self.state_path, state)
                        result = True
            if lease_owned:
                lease_released = self._handoff_owner_lease(
                    lease_token,
                    lease_stop,
                    heartbeat,
                    (lambda: self.request_refresh(group)) if requeue_group else None,
                )
            else:
                self._stop_lease_heartbeat(lease_stop, heartbeat)
                if requeue_group:
                    self.request_refresh(group)
            if lease_released:
                self._schedule_shared_pending()
            return result
        except Exception:
            _remove_candidate(staging)
            if published and not head_committed:
                try:
                    durable_head = self.ledger.latest_available()
                    head_committed = (
                        durable_head is not None
                        and reservation is not None
                        and durable_head.observation_generation
                        == reservation.observation_generation
                        and durable_head.candidate_path == str(final)
                    )
                except ObservationLedgerError:
                    head_committed = False
                if not head_committed:
                    _remove_candidate(final)
            if not lease_release_deferred:
                self._mark_ineligible(group, "refresh_failed", timeout_seconds=0)
                if lease_token and lease_owned:
                    self._handoff_owner_lease(lease_token, lease_stop, heartbeat)
                else:
                    self._stop_lease_heartbeat(lease_stop, heartbeat)
            return False

    def state(self):
        """Return redacted runtime state for tests/diagnostics, never source data."""
        with self._mutex.hold():
            return self._ensure_generation_state(self._load_state())

    def current_observation_generation(self):
        """Return the fresh verified generation or fail closed."""
        with self._mutex.hold(timeout_seconds=0):
            state = self._ensure_generation_state(self._load_state())
            if not self._eligible(state, GROUP_MASTER_DATABASE):
                raise ObservationGenerationContractError(
                    "master database observation publication is not eligible"
                )
            current = state.get("current") or {}
            if current.get("source_identity_ref") != self.trusted_source_identity():
                raise ObservationGenerationContractError(
                    "published source identity is untrusted"
                )
            self._read_generation(state, "api/master")
            generation = self._last_observation_generation(state)
            if generation is None:
                raise ObservationGenerationContractError(
                    "verified observation generation is unavailable"
                )
            return generation

    def current_observation_publication(self):
        """Return the latest verified ledger publication or fail closed."""
        with self._mutex.hold(timeout_seconds=0):
            self._ensure_generation_state(self._load_state())
            publication = self.ledger.latest_available()
            if publication is None:
                raise ObservationGenerationContractError("verified publication is unavailable")
            if publication.state == "finalized":
                raise ObservationGenerationContractError(
                    "verified publication is not published"
                )
            if (
                publication.observation_boundary_id is not None
                and publication.state != "authority_acked"
            ):
                raise ObservationGenerationContractError(
                    "authority ACK is required for proving publication"
                )
            return publication

    def current_observation_source_set(self):
        """Return the source set bound to the verified ledger publication."""
        return self.current_observation_publication().source_set

    def trusted_source_identity(self):
        """Return the immutable configured source identity or fail closed."""
        if self._trusted_source_identity is None:
            raise ObservationGenerationContractError(
                "trusted runtime source identity is unavailable"
            )
        return self._trusted_source_identity

    @staticmethod
    def _last_observation_generation(state):
        current = state.get("current")
        if not isinstance(current, dict):
            return None
        value = current.get("observation_generation")
        if value is None:
            return None
        if type(value) is not int or value < 1:
            raise ObservationGenerationContractError(
                "current observation generation is invalid"
            )
        return value

    def _current_observation_boundary(self):
        if self._test_only_observation_boundary_provider is not None:
            boundary = self._test_only_observation_boundary_provider.current_boundary()
            if not isinstance(boundary, TrustedObservationBoundary):
                raise ObservationGenerationContractError(
                    "test boundary provider returned an invalid boundary"
                )
            return boundary
        if self._legacy_authority_store_factory is None:
            return None
        store = self._legacy_authority_store_factory()
        try:
            provider = DurableObservationBoundaryProvider(store)
            boundary = provider.current_boundary()
        finally:
            store.close()
        if boundary is not None and not isinstance(boundary, TrustedObservationBoundary):
            raise ObservationGenerationContractError(
                "observation boundary provider returned an invalid boundary"
            )
        return boundary

    @staticmethod
    def _persist_observation_metadata(
        candidate,
        run_id,
        reservation,
        boundary: TrustedObservationBoundary | None,
        captured_at,
        completed_at,
        materializer_run_id,
        observed_state,
        attestation_fingerprint,
    ):
        repository = SQLiteCandidateRepository.open_existing(candidate)
        try:
            with repository.transaction():
                repository.set_observation_generation(
                    run_id,
                    reservation.observation_generation,
                    reservation.generation_id,
                    reservation.source_set,
                    reservation.source_identity_ref,
                    None if boundary is None else boundary.boundary_id,
                    None if boundary is None else boundary.generation_floor,
                    captured_at,
                    completed_at,
                    reservation.pre_send_identity,
                    reservation.canary_run_id,
                    reservation.fence_identity,
                    reservation.authority_epoch,
                    reservation.fence_counter,
                    reservation.target_ref,
                    reservation.requested_state,
                    reservation.observation_binding_fingerprint,
                    materializer_run_id,
                    observed_state,
                    attestation_fingerprint,
                )
        finally:
            repository.close()


def _json_text(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
