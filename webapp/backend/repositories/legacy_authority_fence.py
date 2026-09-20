from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Final, Protocol

from repositories.legacy_authority_store_types import LegacyAuthorityStoreError
from repositories.legacy_authority_types import FenceState


class TransitionError(LegacyAuthorityStoreError):
    pass


class _FenceHost(Protocol):
    connection: sqlite3.Connection

    def _immediate(self) -> AbstractContextManager[None]: ...

    def _artifact(self, identity: str) -> sqlite3.Row: ...

    def get_fence(self, identity: str) -> sqlite3.Row | None: ...

    def _transition_fence(self, identity: str, state: FenceState) -> None: ...


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
        with self._immediate():
            existing = self.get_fence(identity)
            if existing is not None:
                if existing["state"] == FenceState.REQUESTED.value:
                    return
                raise TransitionError("fence request already progressed")
            artifact = self._artifact(identity)
            values = (identity, artifact["canary_run_id"], artifact["authority_epoch"], artifact["fence_counter"])
            self.connection.execute("INSERT INTO fences VALUES (?,?,?,?,?)", (*values, FenceState.REQUESTED.value))
            self.connection.execute(
                "INSERT INTO fence_history(pre_send_identity,canary_run_id,authority_epoch,fence_counter,from_state,to_state) VALUES (?,?,?,?,?,?)",
                (*values, None, FenceState.REQUESTED.value),
            )

    def transition_fence(self: _FenceHost, identity: str, state: FenceState) -> None:
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
        with self._immediate():
            row = self.get_fence(identity)
            if row is None or row["state"] != FenceState.ABANDONED.value:
                raise TransitionError("only an abandoned fence may resolve_clear")
            self.connection.execute(
                "INSERT INTO fence_history(pre_send_identity,canary_run_id,authority_epoch,fence_counter,from_state,to_state) VALUES (?,?,?,?,?,?)",
                (identity, row["canary_run_id"], row["authority_epoch"], row["fence_counter"], row["state"], "resolve_clear"),
            )
            self.connection.execute("DELETE FROM fences WHERE pre_send_identity=?", (identity,))

    def _transition_fence(self: _FenceHost, identity: str, state: FenceState) -> None:
        row = self.get_fence(identity)
        if row is None:
            raise TransitionError("fence is missing")
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
            "INSERT INTO fence_history(pre_send_identity,canary_run_id,authority_epoch,fence_counter,from_state,to_state) VALUES (?,?,?,?,?,?)",
            (identity, row["canary_run_id"], row["authority_epoch"], row["fence_counter"], row["state"], state.value),
        )
