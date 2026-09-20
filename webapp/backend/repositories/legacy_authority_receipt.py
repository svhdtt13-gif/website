from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Protocol

from repositories.legacy_authority_fence import TransitionError
from repositories.legacy_authority_store_types import ReceiptConflictError
from repositories.legacy_authority_types import (
    FenceState,
    Receipt,
    ReceiptDecision,
    ReceiptState,
)


class _ReceiptHost(Protocol):
    connection: sqlite3.Connection

    def _immediate(self) -> AbstractContextManager[None]: ...

    def _artifact(self, identity: str) -> sqlite3.Row: ...

    def get_fence(self, identity: str) -> sqlite3.Row | None: ...

    def _transition_fence(self, identity: str, state: FenceState) -> None: ...

    def _receipt_row(self, identity: str) -> sqlite3.Row | None: ...

    def get_receipt(self, identity: str) -> Receipt: ...

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Receipt: ...


class ReceiptLifecycleMixin:
    def accept_receipt(self: _ReceiptHost, identity: str, fingerprint: str) -> ReceiptDecision:
        with self._immediate():
            existing = self._receipt_row(identity)
            if existing is not None:
                if existing["envelope_fingerprint"] != fingerprint:
                    raise ReceiptConflictError("receipt identity has a conflicting fingerprint")
                return ReceiptDecision(self._receipt(existing), False)
            artifact = self._artifact(identity)
            if artifact["envelope_fingerprint"] != fingerprint:
                raise ReceiptConflictError("artifact fingerprint binding conflict")
            fence = self.get_fence(identity)
            if fence is None or fence["state"] != FenceState.ACQUIRED.value:
                raise TransitionError("receipt requires an acquired fence")
            self.connection.execute(
                "INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, fingerprint, artifact["canary_idempotency_key"], artifact["handoff_id"], artifact["canary_run_id"], artifact["authority_epoch"], artifact["fence_counter"], artifact["source_identity_ref"], artifact["artifact_producer_identity"], artifact["envelope_exporter_identity"], artifact["source_envelope_contract_version"], artifact["transport_contract_version"], artifact["contract_version"], artifact["target_ref"], artifact["authorization_artifact_fingerprint"], artifact["pre_operation_observation_generation_floor"], ReceiptState.ACCEPTED.value, None, None),
            )
            self._transition_fence(identity, FenceState.RECEIPT_ACCEPTED)
            return ReceiptDecision(self.get_receipt(identity), True)

    def begin_dispatch(self: _ReceiptHost, decision: ReceiptDecision) -> Receipt:
        if not decision.mutation_allowed:
            raise TransitionError("receipt replay has no mutation permission")
        receipt = decision.receipt
        with self._immediate():
            fence = self.get_fence(receipt.pre_send_identity)
            if fence is None or fence["state"] != FenceState.RECEIPT_ACCEPTED.value:
                raise TransitionError("dispatch requires an accepted fence")
            changed = self.connection.execute(
                "UPDATE receipts SET state=? WHERE pre_send_identity=? AND envelope_fingerprint=? AND state=?",
                (ReceiptState.DISPATCHING.value, receipt.pre_send_identity, receipt.envelope_fingerprint, ReceiptState.ACCEPTED.value),
            ).rowcount
            if changed != 1:
                raise TransitionError("dispatch CAS lost")
            self._transition_fence(receipt.pre_send_identity, FenceState.MUTATION)
        return self.get_receipt(receipt.pre_send_identity)

    def terminal_receipt(self: _ReceiptHost, receipt: Receipt, state: ReceiptState, observation_boundary_id: str | None, observation_generation: int | None) -> Receipt:
        if state not in (ReceiptState.APPLIED, ReceiptState.NOT_APPLIED_PROVEN, ReceiptState.UNKNOWN):
            raise TransitionError("invalid terminal receipt state")
        if state is ReceiptState.APPLIED and receipt.state is not ReceiptState.DISPATCHING:
            raise TransitionError("applied receipt requires dispatching state")
        if state is ReceiptState.APPLIED and (not observation_boundary_id or observation_generation is None or observation_generation <= receipt.observation_generation_floor):
            raise TransitionError("applied receipt requires a post-dispatch observation boundary")
        with self._immediate():
            fence = self.get_fence(receipt.pre_send_identity)
            expected_fence = FenceState.MUTATION if receipt.state is ReceiptState.DISPATCHING else FenceState.RECEIPT_ACCEPTED
            if fence is None or fence["state"] != expected_fence.value:
                raise TransitionError("terminal receipt fence CAS lost")
            changed = self.connection.execute(
                "UPDATE receipts SET state=?,observation_boundary_id=?,observation_generation=? WHERE pre_send_identity=? AND envelope_fingerprint=? AND canary_run_id=? AND authority_epoch=? AND fence_counter=? AND state=?",
                (state.value, observation_boundary_id, observation_generation, receipt.pre_send_identity, receipt.envelope_fingerprint, receipt.canary_run_id, receipt.authority_epoch, receipt.fence_counter, receipt.state.value),
            ).rowcount
            if changed != 1 or receipt.state not in (ReceiptState.ACCEPTED, ReceiptState.DISPATCHING):
                raise TransitionError("terminal receipt CAS lost")
            self._transition_fence(receipt.pre_send_identity, FenceState.RECEIPT_TERMINAL)
        return self.get_receipt(receipt.pre_send_identity)

    def mark_observation_pending(self: _ReceiptHost, identity: str) -> None:
        with self._immediate():
            self._transition_fence(identity, FenceState.OBSERVATION_PENDING)

    def close_fence(self: _ReceiptHost, identity: str) -> None:
        with self._immediate():
            self._transition_fence(identity, FenceState.CLOSED)

    def get_receipt(self: _ReceiptHost, identity: str) -> Receipt:
        row = self._receipt_row(identity)
        if row is None:
            raise TransitionError("receipt is missing")
        return self._receipt(row)

    def _receipt_row(self: _ReceiptHost, identity: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM receipts WHERE pre_send_identity=?", (identity,)).fetchone()

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Receipt:
        return Receipt(row["pre_send_identity"], row["envelope_fingerprint"], row["canary_run_id"], row["authority_epoch"], row["fence_counter"], ReceiptState(row["state"]), row["observation_generation_floor"], row["observation_boundary_id"], row["observation_generation"])
