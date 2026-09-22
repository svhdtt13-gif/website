from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Final, Protocol, assert_never

from repositories.legacy_authority_store_types import (
    LegacyAuthorityStoreError,
    ReceiptConflictError,
)
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    AuthorizationLookup,
    EnvelopeAuthorizationLookup,
    Handoff,
    HandoffAuthorizationLookup,
    StoredAuthorization,
)

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


class _AuthorizationHost(Protocol):
    connection: sqlite3.Connection

    def _immediate(self) -> AbstractContextManager[None]: ...

    def _require_live(self) -> None: ...

    def _authorization_for_handoff_locked(
        self, handoff: Handoff
    ) -> StoredAuthorization | None: ...

    def _insert_authorization_locked(
        self, handoff: Handoff, artifact: AuthorizationArtifact
    ) -> None: ...

    def _lookup_authorization_locked(
        self,
        lookup: AuthorizationLookup
        | HandoffAuthorizationLookup
        | EnvelopeAuthorizationLookup,
    ) -> StoredAuthorization | None: ...

    def lookup_authorization(
        self,
        lookup: AuthorizationLookup
        | HandoffAuthorizationLookup
        | EnvelopeAuthorizationLookup,
    ) -> StoredAuthorization | None: ...


class AuthorizationPersistenceMixin:
    def add_authorization(
        self: _AuthorizationHost,
        handoff: Handoff,
        artifact: AuthorizationArtifact,
    ) -> None:
        self._require_live()
        artifact.require_handoff(handoff)
        with self._immediate():
            existing = self._authorization_for_handoff_locked(handoff)
            if existing is not None:
                if existing.artifact.projection() == artifact.projection():
                    return
                raise ReceiptConflictError("handoff or artifact identity conflict")
            self._insert_authorization_locked(handoff, artifact)

    def get_or_create_authorization(
        self: _AuthorizationHost,
        handoff: Handoff,
        artifact_factory: Callable[[], AuthorizationArtifact],
    ) -> StoredAuthorization:
        self._require_live()
        with self._immediate():
            existing = self._authorization_for_handoff_locked(handoff)
            if existing is not None:
                return existing
            artifact = artifact_factory()
            artifact.require_handoff(handoff)
            self._insert_authorization_locked(handoff, artifact)
            durable = self._lookup_authorization_locked(
                AuthorizationLookup(handoff.pre_send_identity)
            )
            if durable is None:
                raise LegacyAuthorityStoreError(
                    "durable authorization is unavailable after insert"
                )
            return durable

    def _authorization_for_handoff_locked(
        self: _AuthorizationHost, handoff: Handoff
    ) -> StoredAuthorization | None:
        existing_handoff = self.connection.execute(
            "SELECT * FROM handoffs WHERE handoff_id=? OR pre_send_identity=? "
            "OR envelope_fingerprint=?",
            (
                handoff.handoff_id,
                handoff.pre_send_identity,
                handoff.envelope_fingerprint,
            ),
        ).fetchone()
        if existing_handoff is None:
            return None
        if tuple(
            existing_handoff[column] for column in _HANDOFF_COLUMNS
        ) != handoff.projection():
            raise ReceiptConflictError("handoff or artifact identity conflict")
        existing = self._lookup_authorization_locked(
            AuthorizationLookup(handoff.pre_send_identity)
        )
        if existing is None:
            raise ReceiptConflictError("handoff authorization artifact is missing")
        return existing

    def _insert_authorization_locked(
        self: _AuthorizationHost,
        handoff: Handoff,
        artifact: AuthorizationArtifact,
    ) -> None:
        self.connection.execute(
            "INSERT INTO handoffs (handoff_id, source_envelope_contract_version, "
            "transport_contract_version, pre_send_identity, canary_idempotency_key, "
            "envelope_fingerprint, canonical_envelope_json, operation_kind, target_ref, "
            "binding_generation, verified_identity_ref, verified_identity_revision, "
            "authority_epoch, fence_counter, exporter_identity, exporter_attestation) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            handoff.projection(),
        )
        self.connection.execute(
            "INSERT INTO authorization_artifacts (handoff_id, pre_send_identity, "
            "canary_idempotency_key, canary_run_id, fence_identity, authority_epoch, fence_counter, "
            "source_identity_ref, artifact_producer_identity, "
            "source_envelope_contract_version, transport_contract_version, "
            "contract_version, envelope_exporter_identity, operation_kind, target_ref, "
            "requested_state, pre_operation_observation_generation_floor, "
            "envelope_fingerprint, authorization_artifact_fingerprint) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            artifact.projection(),
        )

    def lookup_authorization(
        self: _AuthorizationHost,
        lookup: AuthorizationLookup
        | HandoffAuthorizationLookup
        | EnvelopeAuthorizationLookup,
    ) -> StoredAuthorization | None:
        self._require_live()
        return self._lookup_authorization_locked(lookup)

    def _lookup_authorization_locked(
        self: _AuthorizationHost,
        lookup: AuthorizationLookup
        | HandoffAuthorizationLookup
        | EnvelopeAuthorizationLookup,
    ) -> StoredAuthorization | None:
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
        self: _AuthorizationHost, pre_send_identity: str
    ) -> StoredAuthorization | None:
        return self.lookup_authorization(AuthorizationLookup(pre_send_identity))
