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

    def _require_live(self) -> None: ...

    def _artifact(self, identity: str) -> sqlite3.Row: ...

    def get_fence(self, identity: str) -> sqlite3.Row | None: ...

    def _transition_fence(self, identity: str, state: FenceState) -> None: ...

    def _receipt_row(self, identity: str) -> sqlite3.Row | None: ...

    def get_receipt(self, identity: str) -> Receipt: ...

    @staticmethod
    def _receipt(row: sqlite3.Row) -> Receipt: ...


class ReceiptLifecycleMixin:
    def accept_receipt(self: _ReceiptHost, identity: str, fingerprint: str) -> ReceiptDecision:
        self._require_live()
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
                "INSERT INTO receipts (pre_send_identity,envelope_fingerprint,canary_idempotency_key,handoff_id,canary_run_id,fence_identity,authority_epoch,fence_counter,source_identity_ref,artifact_producer_identity,envelope_exporter_identity,source_envelope_contract_version,transport_contract_version,contract_version,target_ref,authorization_artifact_fingerprint,pre_operation_observation_generation_floor,state,post_dispatch_observation_boundary_id,post_dispatch_observation_generation_floor) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (identity, fingerprint, artifact["canary_idempotency_key"], artifact["handoff_id"], artifact["canary_run_id"], artifact["fence_identity"], artifact["authority_epoch"], artifact["fence_counter"], artifact["source_identity_ref"], artifact["artifact_producer_identity"], artifact["envelope_exporter_identity"], artifact["source_envelope_contract_version"], artifact["transport_contract_version"], artifact["contract_version"], artifact["target_ref"], artifact["authorization_artifact_fingerprint"], artifact["pre_operation_observation_generation_floor"], ReceiptState.ACCEPTED.value, None, None),
            )
            self._transition_fence(identity, FenceState.RECEIPT_ACCEPTED)
            return ReceiptDecision(self.get_receipt(identity), True)

    def begin_dispatch(self: _ReceiptHost, decision: ReceiptDecision) -> Receipt:
        self._require_live()
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

    def terminal_receipt(self: _ReceiptHost, receipt: Receipt, state: ReceiptState) -> Receipt:
        if state not in (ReceiptState.APPLIED, ReceiptState.NOT_APPLIED_PROVEN, ReceiptState.UNKNOWN):
            raise TransitionError("invalid terminal receipt state")
        allowed_sources = {
            ReceiptState.APPLIED: (ReceiptState.DISPATCHING,),
            ReceiptState.NOT_APPLIED_PROVEN: (ReceiptState.ACCEPTED,),
            ReceiptState.UNKNOWN: (ReceiptState.ACCEPTED, ReceiptState.DISPATCHING),
        }
        if receipt.state not in allowed_sources[state]:
            raise TransitionError("invalid terminal receipt transition")
        self._require_live()
        with self._immediate():
            fence = self.get_fence(receipt.pre_send_identity)
            expected_fence = FenceState.MUTATION if receipt.state is ReceiptState.DISPATCHING else FenceState.RECEIPT_ACCEPTED
            if fence is None or fence["state"] != expected_fence.value:
                raise TransitionError("terminal receipt fence CAS lost")
            changed = self.connection.execute(
                "UPDATE receipts SET state=? WHERE pre_send_identity=? AND envelope_fingerprint=? AND canary_run_id=? AND fence_identity=? AND authority_epoch=? AND fence_counter=? AND state=?",
                (state.value, receipt.pre_send_identity, receipt.envelope_fingerprint, receipt.canary_run_id, receipt.fence_identity, receipt.authority_epoch, receipt.fence_counter, receipt.state.value),
            ).rowcount
            if changed != 1:
                raise TransitionError("terminal receipt CAS lost")
            self._transition_fence(receipt.pre_send_identity, FenceState.RECEIPT_TERMINAL)
        return self.get_receipt(receipt.pre_send_identity)

    def append_observation_boundary(self: _ReceiptHost, receipt: Receipt, boundary_id: str, generation_floor: int) -> Receipt:
        self._require_live()
        if receipt.state is not ReceiptState.APPLIED:
            raise TransitionError("observation boundary requires an applied receipt")
        if not boundary_id or boundary_id != boundary_id.strip() or generation_floor <= receipt.pre_operation_observation_generation_floor:
            raise TransitionError("invalid post-dispatch observation boundary")
        with self._immediate():
            existing = self._receipt_row(receipt.pre_send_identity)
            if existing is None:
                raise TransitionError("receipt is missing")
            fence = self.get_fence(receipt.pre_send_identity)
            if fence is None or fence["state"] not in (
                FenceState.RECEIPT_TERMINAL.value,
                FenceState.OBSERVATION_PENDING.value,
            ):
                raise TransitionError("observation boundary requires an open post-terminal fence")
            if existing["post_dispatch_observation_boundary_id"] is not None:
                if existing["post_dispatch_observation_boundary_id"] == boundary_id and existing["post_dispatch_observation_generation_floor"] == generation_floor:
                    return self._receipt(existing)
                raise ReceiptConflictError("observation boundary conflict")
            changed = self.connection.execute(
                "UPDATE receipts SET post_dispatch_observation_boundary_id=?,post_dispatch_observation_generation_floor=? WHERE pre_send_identity=? AND envelope_fingerprint=? AND canary_run_id=? AND fence_identity=? AND fence_counter=? AND state=? AND post_dispatch_observation_boundary_id IS NULL AND post_dispatch_observation_generation_floor IS NULL",
                (boundary_id, generation_floor, receipt.pre_send_identity, receipt.envelope_fingerprint, receipt.canary_run_id, receipt.fence_identity, receipt.fence_counter, ReceiptState.APPLIED.value),
            ).rowcount
            if changed != 1:
                raise TransitionError("observation boundary CAS lost")
        return self.get_receipt(receipt.pre_send_identity)

    def mark_observation_pending(self: _ReceiptHost, identity: str) -> None:
        self._require_live()
        with self._immediate():
            self._transition_fence(identity, FenceState.OBSERVATION_PENDING)

    def close_fence(self: _ReceiptHost, identity: str) -> None:
        self._require_live()
        with self._immediate():
            receipt = self._receipt_row(identity)
            if receipt is not None and receipt["state"] == ReceiptState.APPLIED.value and (
                receipt["post_dispatch_observation_boundary_id"] is None
                or receipt["post_dispatch_observation_generation_floor"] is None
            ):
                raise TransitionError("applied receipt requires an observation boundary before closure")
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
        return Receipt(row["pre_send_identity"], row["envelope_fingerprint"], row["canary_run_id"], row["fence_identity"], row["authority_epoch"], row["fence_counter"], ReceiptState(row["state"]), row["pre_operation_observation_generation_floor"], row["post_dispatch_observation_boundary_id"], row["post_dispatch_observation_generation_floor"])
