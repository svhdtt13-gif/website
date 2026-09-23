from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Final, Protocol

from repositories.legacy_authority_observation import (
    ObservationEvidence,
    _evidence_from_ack_row,
)
from repositories.legacy_authority_store_types import LegacyAuthorityStoreError
from repositories.legacy_authority_types import REQUESTED_STATE, FenceState


class TransitionError(LegacyAuthorityStoreError):
    pass


class _FenceHost(Protocol):
    connection: sqlite3.Connection

    def _immediate(self) -> AbstractContextManager[None]: ...

    def _artifact(self, identity: str) -> sqlite3.Row: ...

    def _require_live(self) -> None: ...

    def get_fence(self, identity: str) -> sqlite3.Row | None: ...

    def _transition_fence(self, identity: str, state: FenceState) -> None: ...

    def _verify_generation_locked(self) -> None: ...

    def _verify_observation_attestation(self, evidence: ObservationEvidence) -> str: ...


_ALLOWED_TRANSITIONS: Final = {
    FenceState.REQUESTED: (FenceState.ACQUIRED,),
    FenceState.ACQUIRED: (FenceState.RECEIPT_ACCEPTED,),
    FenceState.RECEIPT_ACCEPTED: (FenceState.MUTATION, FenceState.RECEIPT_TERMINAL),
    FenceState.MUTATION: (FenceState.RECEIPT_TERMINAL,),
    FenceState.RECEIPT_TERMINAL: (FenceState.OBSERVATION_PENDING,),
    FenceState.OBSERVATION_PENDING: (FenceState.CLOSED, FenceState.ABANDONED),
}


class FenceLifecycleMixin:
    def request_fence(self: _FenceHost, identity: str) -> None:
        self._require_live()
        with self._immediate():
            existing = self.get_fence(identity)
            if existing is not None:
                if existing["state"] == FenceState.REQUESTED.value:
                    return
                raise TransitionError("fence request already progressed")
            artifact = self._artifact(identity)
            values = (
                identity,
                artifact["canary_run_id"],
                artifact["fence_identity"],
                artifact["authority_epoch"],
                artifact["fence_counter"],
            )
            self.connection.execute("INSERT INTO fences VALUES (?,?,?,?,?,?)", (*values, FenceState.REQUESTED.value))
            self.connection.execute(
                "INSERT INTO fence_history(pre_send_identity,canary_run_id,fence_identity,authority_epoch,fence_counter,from_state,to_state) VALUES (?,?,?,?,?,?,?)",
                (*values, None, FenceState.REQUESTED.value),
            )

    def transition_fence(self: _FenceHost, identity: str, state: FenceState) -> None:
        self._require_live()
        with self._immediate():
            self._transition_fence(identity, state)

    def get_fence(self: _FenceHost, identity: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM fences WHERE pre_send_identity=?", (identity,)).fetchone()

    def fence_history(self: _FenceHost, identity: str) -> list[str]:
        return [
            row[0]
            for row in self.connection.execute(
                "SELECT to_state FROM fence_history WHERE pre_send_identity=? ORDER BY transition_id",
                (identity,),
            )
        ]

    def resolve_clear(self: _FenceHost, identity: str) -> None:
        self._require_live()
        with self._immediate():
            row = self.get_fence(identity)
            if row is None or row["state"] != FenceState.ABANDONED.value:
                raise TransitionError("only an abandoned fence may resolve_clear")
            self.connection.execute(
                "INSERT INTO fence_history(pre_send_identity,canary_run_id,fence_identity,authority_epoch,fence_counter,from_state,to_state) VALUES (?,?,?,?,?,?,?)",
                (identity, row["canary_run_id"], row["fence_identity"], row["authority_epoch"], row["fence_counter"], row["state"], "resolve_clear"),
            )
            self.connection.execute("DELETE FROM fences WHERE pre_send_identity=?", (identity,))

    def _transition_fence(self: _FenceHost, identity: str, state: FenceState) -> None:
        row = self.get_fence(identity)
        if row is None:
            raise TransitionError("fence is missing")
        if state is FenceState.CLOSED:
            receipt = self.connection.execute(
                "SELECT * FROM receipts WHERE pre_send_identity=?",
                (identity,),
            ).fetchone()
            if row["state"] == FenceState.OBSERVATION_PENDING.value and receipt is not None and receipt["state"] == "not_applied_proven":
                raise TransitionError("observation-pending fence cannot close a proven non-applied receipt")
            if receipt is not None and receipt["state"] == "applied" and (
                receipt["post_dispatch_observation_boundary_id"] is None
                or receipt["post_dispatch_observation_generation_floor"] is None
                or self.connection.execute(
                    "SELECT 1 FROM observation_materializer_acks WHERE receipt_identity=?",
                    (identity,),
                ).fetchone() is None
            ):
                raise TransitionError("applied receipt requires observation evidence before closure")
            if receipt is not None and receipt["state"] == "applied":
                self._verify_generation_locked()
                ack = self.connection.execute(
                    "SELECT boundary_id,generation_floor FROM observation_materializer_acks WHERE receipt_identity=?",
                    (identity,),
                ).fetchone()
                if ack is None or ack[0] != receipt["post_dispatch_observation_boundary_id"] or ack[1] != receipt["post_dispatch_observation_generation_floor"]:
                    raise TransitionError("applied receipt observation evidence binding is invalid")
                evidence = self.connection.execute(
                    "SELECT * FROM observation_materializer_acks WHERE receipt_identity=?",
                    (identity,),
                ).fetchone()
                if evidence is None:
                    raise TransitionError("applied receipt observation evidence is missing")
                evidence_value = _evidence_from_ack_row(evidence)
                if (
                    evidence_value.source_identity_ref,
                    evidence_value.canary_run_id,
                    evidence_value.fence_identity,
                    evidence_value.fence_counter,
                    evidence_value.target_ref,
                    evidence_value.requested_state,
                    evidence_value.observed_state,
                ) != (
                    receipt["source_identity_ref"],
                    receipt["canary_run_id"],
                    receipt["fence_identity"],
                    receipt["fence_counter"],
                    receipt["target_ref"],
                    REQUESTED_STATE,
                    REQUESTED_STATE,
                ):
                    raise TransitionError("applied receipt observation lineage is invalid")
                try:
                    self._verify_observation_attestation(evidence_value)
                except (LegacyAuthorityStoreError, ValueError) as error:
                    raise TransitionError("applied receipt observation attestation is invalid") from error
        current = FenceState(row["state"])
        if state not in _ALLOWED_TRANSITIONS.get(current, ()):
            raise TransitionError("invalid fence transition")
        changed = self.connection.execute(
            "UPDATE fences SET state=? WHERE pre_send_identity=? AND state=?",
            (state.value, identity, row["state"]),
        ).rowcount
        if changed != 1:
            raise TransitionError("fence CAS lost")
        self.connection.execute(
            "INSERT INTO fence_history(pre_send_identity,canary_run_id,fence_identity,authority_epoch,fence_counter,from_state,to_state) VALUES (?,?,?,?,?,?,?)",
            (identity, row["canary_run_id"], row["fence_identity"], row["authority_epoch"], row["fence_counter"], row["state"], state.value),
        )
