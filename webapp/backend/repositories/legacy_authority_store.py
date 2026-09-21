from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Final, assert_never

from repositories.legacy_authority_fence import FenceLifecycleMixin, TransitionError
from repositories.legacy_authority_receipt import ReceiptLifecycleMixin
from repositories.legacy_authority_schema import (
    SCHEMA_CHECKSUM,
    SCHEMA_IDENTITY,
    SCHEMA_SQL,
    schema_identity,
)
from repositories.legacy_authority_store_types import (
    LegacyAuthorityStoreError,
    ReceiptConflictError,
    StoreQuarantinedError,
)
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    AuthorizationLookup,
    EnvelopeAuthorizationLookup,
    Handoff,
    HandoffAuthorizationLookup,
    LegacyValidationError,
    StoredAuthorization,
)

STORE_KIND: Final = "legacy_canary_authority"
SCHEMA_VERSION: Final = 3
DB_FILENAME: Final = "legacy_canary.sqlite3"

_HANDOFF_COLUMNS: Final = (
    "handoff_id", "source_envelope_contract_version",
    "transport_contract_version", "pre_send_identity", "canary_idempotency_key",
    "envelope_fingerprint", "canonical_envelope_json", "operation_kind", "target_ref",
    "binding_generation", "verified_identity_ref", "verified_identity_revision",
    "authority_epoch", "fence_counter", "exporter_identity", "exporter_attestation",
)
_ARTIFACT_COLUMNS: Final = (
    "handoff_id", "pre_send_identity", "canary_idempotency_key", "canary_run_id",
    "fence_identity", "authority_epoch", "fence_counter", "source_identity_ref",
    "artifact_producer_identity", "source_envelope_contract_version",
    "transport_contract_version", "contract_version", "envelope_exporter_identity",
    "operation_kind", "target_ref", "requested_state",
    "pre_operation_observation_generation_floor", "envelope_fingerprint",
    "authorization_artifact_fingerprint",
)


def legacy_store_path(runtime_root: PurePath) -> Path:
    rendered = str(runtime_root).replace("\\", "/")
    lowered = rendered.casefold()
    if lowered.startswith("d:/") or "p4_operational.sqlite3" in lowered:
        raise LegacyValidationError("LEGACY authority store path is forbidden")
    return Path(runtime_root) / "legacy" / DB_FILENAME


def _deny_cross_database(action: int, _arg1: str | None, _arg2: str | None, _db: str | None, _source: str | None) -> int:
    return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH) else sqlite3.SQLITE_OK


class LegacyAuthorityStore(FenceLifecycleMixin, ReceiptLifecycleMixin):
    def __init__(self, connection: sqlite3.Connection, path: Path, quarantined: bool = False) -> None:
        self.connection = connection
        self.path = path
        self._quarantined = quarantined

    @staticmethod
    def _connect(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(str(path), timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.set_authorizer(_deny_cross_database)
        connection.execute("PRAGMA foreign_keys = ON")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).casefold() != "wal":
            connection.close()
            raise LegacyAuthorityStoreError("LEGACY authority store requires WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @classmethod
    def create(cls, path: Path) -> LegacyAuthorityStore:
        cls._require_safe_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = cls._connect(path)
        store = cls(connection, path)
        try:
            connection.executescript(SCHEMA_SQL)
            connection.executemany(
                "INSERT INTO schema_meta(key, value) VALUES (?, ?)",
                (
                    ("store_kind", STORE_KIND),
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("schema_checksum", SCHEMA_CHECKSUM),
                    ("restore_state", "canonical"),
                    ("restore_marker", ""),
                ),
            )
            store._validate()
        except (sqlite3.DatabaseError, LegacyAuthorityStoreError):
            connection.close()
            raise
        return store

    @classmethod
    def open(cls, path: Path) -> LegacyAuthorityStore:
        cls._require_safe_path(path)
        store = cls(cls._connect(path), path)
        try:
            store._validate()
        except (sqlite3.DatabaseError, LegacyAuthorityStoreError):
            store.close()
            raise
        store._quarantined = store.schema_metadata()["restore_state"] == "quarantined"
        return store

    @classmethod
    def open_restored(cls, path: Path, restore_marker: str) -> LegacyAuthorityStore:
        if not restore_marker or restore_marker != restore_marker.strip():
            raise LegacyValidationError("restore_marker must be a non-empty canonical value")
        store = cls.open(path)
        if store._quarantined:
            store.close()
            raise StoreQuarantinedError("LEGACY authority store is already quarantined")
        with store._immediate():
            store.connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='restore_state'",
                ("quarantined",),
            )
            store.connection.execute(
                "UPDATE schema_meta SET value=? WHERE key='restore_marker'",
                (restore_marker,),
            )
        store._quarantined = True
        return store

    @staticmethod
    def _require_safe_path(path: Path) -> None:
        rendered = str(path).replace("\\", "/").casefold()
        if rendered.startswith("d:/") or path.name != DB_FILENAME or path.parent.name.casefold() != "legacy" or "p4_operational" in rendered:
            raise LegacyValidationError("invalid LEGACY authority store path")

    def close(self) -> None:
        self.connection.close()

    def _validate(self) -> None:
        metadata = self.schema_metadata()
        required_metadata = {
            "store_kind": STORE_KIND,
            "schema_version": str(SCHEMA_VERSION),
            "schema_checksum": SCHEMA_CHECKSUM,
        }
        if any(metadata.get(key) != value for key, value in required_metadata.items()):
            raise LegacyAuthorityStoreError("LEGACY authority schema metadata mismatch")
        if metadata.get("restore_state") not in ("canonical", "quarantined"):
            raise LegacyAuthorityStoreError("LEGACY authority restore state mismatch")
        if schema_identity(self.connection) != SCHEMA_IDENTITY:
            raise LegacyAuthorityStoreError("LEGACY authority schema identity mismatch")
        if self.connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise LegacyAuthorityStoreError("LEGACY authority integrity check failed")
        if self.connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise LegacyAuthorityStoreError("LEGACY authority foreign key check failed")

    def schema_metadata(self) -> Mapping[str, str]:
        return {row["key"]: row["value"] for row in self.connection.execute("SELECT key, value FROM schema_meta")}

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:  # noqa: RUF100  # noqa: BROAD_EXCEPT_OK - rollback must cover system-level failures too
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def add_authorization(self, handoff: Handoff, artifact: AuthorizationArtifact) -> None:
        self._require_live()
        artifact.require_handoff(handoff)
        with self._immediate():
            existing_handoff = self.connection.execute(
                "SELECT * FROM handoffs WHERE handoff_id=? OR pre_send_identity=? "
                "OR envelope_fingerprint=?",
                (
                    handoff.handoff_id,
                    handoff.pre_send_identity,
                    handoff.envelope_fingerprint,
                ),
            ).fetchone()
            if existing_handoff is not None:
                existing_artifact = self.connection.execute(
                    "SELECT * FROM authorization_artifacts WHERE pre_send_identity=?",
                    (existing_handoff["pre_send_identity"],),
                ).fetchone()
                if (
                    tuple(existing_handoff[column] for column in _HANDOFF_COLUMNS) == handoff.projection()
                    and existing_artifact is not None
                    and tuple(existing_artifact[column] for column in _ARTIFACT_COLUMNS) == artifact.projection()
                ):
                    return
                raise ReceiptConflictError("handoff or artifact identity conflict")
            self.connection.execute(
                "INSERT INTO handoffs (handoff_id, source_envelope_contract_version, "
                "transport_contract_version, pre_send_identity, canary_idempotency_key, "
                "envelope_fingerprint, canonical_envelope_json, operation_kind, target_ref, "
                "binding_generation, verified_identity_ref, verified_identity_revision, "
                "authority_epoch, fence_counter, exporter_identity, exporter_attestation) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    handoff.handoff_id,
                    handoff.source_envelope_contract_version,
                    handoff.transport_contract_version,
                    handoff.pre_send_identity,
                    handoff.canary_idempotency_key,
                    handoff.envelope_fingerprint,
                    handoff.canonical_envelope_json,
                    handoff.operation_kind,
                    handoff.target_ref,
                    handoff.binding_generation,
                    handoff.verified_identity_ref,
                    handoff.verified_identity_revision,
                    handoff.authority_epoch,
                    handoff.fence_counter,
                    handoff.exporter_identity,
                    handoff.exporter_attestation,
                ),
            )
            self.connection.execute(
                "INSERT INTO authorization_artifacts (pre_send_identity, handoff_id, "
                "canary_idempotency_key, canary_run_id, fence_identity, authority_epoch, fence_counter, "
                "source_identity_ref, artifact_producer_identity, "
                "source_envelope_contract_version, transport_contract_version, "
                "contract_version, envelope_exporter_identity, operation_kind, target_ref, "
                "requested_state, pre_operation_observation_generation_floor, "
                "envelope_fingerprint, authorization_artifact_fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.pre_send_identity,
                    artifact.handoff_id,
                    artifact.canary_idempotency_key,
                    artifact.canary_run_id,
                    artifact.fence_identity,
                    artifact.authority_epoch,
                    artifact.fence_counter,
                    artifact.source_identity_ref,
                    artifact.artifact_producer_identity,
                    artifact.source_envelope_contract_version,
                    artifact.transport_contract_version,
                    artifact.contract_version,
                    artifact.envelope_exporter_identity,
                    artifact.operation_kind,
                    artifact.target_ref,
                    artifact.requested_state,
                    artifact.pre_operation_observation_generation_floor,
                    artifact.envelope_fingerprint,
                    artifact.authorization_artifact_fingerprint,
                ),
            )

    def lookup_authorization(
        self,
        lookup: AuthorizationLookup
        | HandoffAuthorizationLookup
        | EnvelopeAuthorizationLookup,
    ) -> StoredAuthorization | None:
        self._require_live()
        match lookup:
            case AuthorizationLookup(pre_send_identity=value):
                column = "pre_send_identity"
            case HandoffAuthorizationLookup(handoff_id=value):
                column = "handoff_id"
            case EnvelopeAuthorizationLookup(envelope_fingerprint=value):
                column = "envelope_fingerprint"
            case unreachable:
                assert_never(unreachable)
        row = self.connection.execute(
            "SELECT "
            + ",".join(f"h.{column} AS handoff_{column}" for column in _HANDOFF_COLUMNS)
            + ","
            + ",".join(f"a.{column} AS artifact_{column}" for column in _ARTIFACT_COLUMNS)
            + " FROM handoffs h JOIN authorization_artifacts a "
            f"ON a.pre_send_identity=h.pre_send_identity WHERE h.{column}=?",
            (value,),
        ).fetchone()
        if row is None:
            return None
        handoff = Handoff(
            handoff_id=row["handoff_handoff_id"],
            source_envelope_contract_version=row["handoff_source_envelope_contract_version"],
            transport_contract_version=row["handoff_transport_contract_version"],
            pre_send_identity=row["handoff_pre_send_identity"],
            canary_idempotency_key=row["handoff_canary_idempotency_key"],
            envelope_fingerprint=row["handoff_envelope_fingerprint"],
            canonical_envelope_json=row["handoff_canonical_envelope_json"],
            operation_kind=row["handoff_operation_kind"],
            target_ref=row["handoff_target_ref"],
            binding_generation=row["handoff_binding_generation"],
            verified_identity_ref=row["handoff_verified_identity_ref"],
            verified_identity_revision=row["handoff_verified_identity_revision"],
            authority_epoch=row["handoff_authority_epoch"],
            fence_counter=row["handoff_fence_counter"],
            exporter_identity=row["handoff_exporter_identity"],
            exporter_attestation=row["handoff_exporter_attestation"],
        )
        artifact = AuthorizationArtifact(
            handoff_id=row["artifact_handoff_id"],
            pre_send_identity=row["artifact_pre_send_identity"],
            canary_idempotency_key=row["artifact_canary_idempotency_key"],
            canary_run_id=row["artifact_canary_run_id"],
            fence_identity=row["artifact_fence_identity"],
            authority_epoch=row["artifact_authority_epoch"],
            fence_counter=row["artifact_fence_counter"],
            source_identity_ref=row["artifact_source_identity_ref"],
            artifact_producer_identity=row["artifact_artifact_producer_identity"],
            source_envelope_contract_version=row["artifact_source_envelope_contract_version"],
            transport_contract_version=row["artifact_transport_contract_version"],
            contract_version=row["artifact_contract_version"],
            envelope_exporter_identity=row["artifact_envelope_exporter_identity"],
            operation_kind=row["artifact_operation_kind"],
            target_ref=row["artifact_target_ref"],
            requested_state=row["artifact_requested_state"],
            pre_operation_observation_generation_floor=row[
                "artifact_pre_operation_observation_generation_floor"
            ],
            envelope_fingerprint=row["artifact_envelope_fingerprint"],
            authorization_artifact_fingerprint=row[
                "artifact_authorization_artifact_fingerprint"
            ],
        )
        return StoredAuthorization(handoff, artifact)

    def lookup_authorization_by_pre_send_identity(
        self, pre_send_identity: str
    ) -> StoredAuthorization | None:
        return self.lookup_authorization(AuthorizationLookup(pre_send_identity))

    def _artifact(self, identity: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM authorization_artifacts WHERE pre_send_identity=?", (identity,)).fetchone()
        if row is None:
            raise TransitionError("authorization artifact is missing")
        return row

    def _require_live(self) -> None:
        if self._quarantined:
            raise StoreQuarantinedError("LEGACY authority store is quarantined")
