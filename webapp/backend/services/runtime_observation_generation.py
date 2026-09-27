from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Final, Protocol

from repositories.legacy_authority_types import FenceState, Receipt, ReceiptState


class ObservationGenerationContractError(ValueError):
    """Raised when a runtime publication cannot prove causal ordering."""


_SECRET_SOURCE_MARKERS: Final = (
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "session",
    "token",
    "http://",
    "https://",
)


def require_non_secret_source_identity(value: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ObservationGenerationContractError(
            "source_identity_ref must be a canonical logical reference"
        )
    if any(marker in value.casefold() for marker in _SECRET_SOURCE_MARKERS):
        raise ObservationGenerationContractError(
            "source_identity_ref must be a non-secret logical reference"
        )
    return value


def observation_binding_fingerprint(
    boundary_id: str,
    pre_send_identity: str,
    canary_run_id: str,
    fence_identity: str,
    authority_epoch: str,
    fence_counter: int,
    source_identity_ref: str,
    target_ref: str,
    requested_state: str,
    generation_floor: int,
) -> str:
    """Digest the canonical authority lineage projection."""
    projection = (
        "lcr2d.runtime-observation-binding.v1",
        boundary_id,
        pre_send_identity,
        canary_run_id,
        fence_identity,
        authority_epoch,
        fence_counter,
        source_identity_ref,
        target_ref,
        requested_state,
        generation_floor,
    )
    payload = json.dumps(projection, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TrustedObservationBoundary:
    """Read-only authority lineage required for a boundary-bound publication."""

    boundary_id: str
    pre_send_identity: str
    generation_floor: int
    canary_run_id: str
    fence_identity: str
    source_identity_ref: str
    target_ref: str
    authority_epoch: str
    fence_counter: int
    requested_state: str

    def __post_init__(self) -> None:
        for name in (
            "boundary_id", "pre_send_identity", "canary_run_id", "fence_identity",
            "source_identity_ref", "target_ref",
            "authority_epoch", "requested_state",
        ):
            value = getattr(self, name)
            if type(value) is not str or not value or value != value.strip():
                raise ObservationGenerationContractError(
                    f"{name} must be a canonical authority reference"
                )
        require_non_secret_source_identity(self.source_identity_ref)
        if type(self.generation_floor) is not int or self.generation_floor < 1:
            raise ObservationGenerationContractError(
                "generation_floor must be positive"
            )
        if type(self.fence_counter) is not int or self.fence_counter < 1:
            raise ObservationGenerationContractError("fence_counter must be positive")

    def binding_projection(self) -> tuple[str | int, ...]:
        return (
            "lcr2d.runtime-observation-binding.v1",
            self.boundary_id,
            self.pre_send_identity,
            self.canary_run_id,
            self.fence_identity,
            self.authority_epoch,
            self.fence_counter,
            self.source_identity_ref,
            self.target_ref,
            self.requested_state,
            self.generation_floor,
        )

    def binding_fingerprint(self) -> str:
        return observation_binding_fingerprint(
            self.boundary_id,
            self.pre_send_identity,
            self.canary_run_id,
            self.fence_identity,
            self.authority_epoch,
            self.fence_counter,
            self.source_identity_ref,
            self.target_ref,
            self.requested_state,
            self.generation_floor,
        )

    @classmethod
    def from_receipt(cls, receipt: Receipt) -> TrustedObservationBoundary:
        """Parse a boundary from a durable applied receipt, never from reader data."""
        if receipt.state is not ReceiptState.APPLIED:
            raise ObservationGenerationContractError(
                "observation boundary requires an applied receipt"
            )
        if receipt.fence_state not in (
            FenceState.RECEIPT_TERMINAL,
            FenceState.OBSERVATION_PENDING,
        ):
            raise ObservationGenerationContractError(
                "observation boundary is not open for publication"
            )
        boundary_id = receipt.post_dispatch_observation_boundary_id
        generation_floor = receipt.post_dispatch_observation_generation_floor
        if boundary_id is None or generation_floor is None:
            raise ObservationGenerationContractError(
                "durable observation boundary is unavailable"
            )
        return cls(
            boundary_id=boundary_id,
            pre_send_identity=receipt.pre_send_identity,
            generation_floor=generation_floor,
            canary_run_id=receipt.canary_run_id,
            fence_identity=receipt.fence_identity,
            source_identity_ref=receipt.source_identity_ref,
            target_ref=receipt.target_ref,
            authority_epoch=receipt.authority_epoch,
            fence_counter=receipt.fence_counter,
            requested_state=receipt.requested_state,
        )


class AuthorityReceiptReader(Protocol):
    def get_receipt(self, identity: str) -> Receipt: ...

    def current_observation_receipt(self) -> Receipt | None: ...


class ObservationBoundaryProvider(Protocol):
    def current_boundary(self) -> TrustedObservationBoundary | None: ...


@dataclass(frozen=True, slots=True)
class DurableObservationBoundaryProvider:
    """Load the current boundary from the single durable LEGACY authority store."""

    store: AuthorityReceiptReader

    def current_boundary(self) -> TrustedObservationBoundary | None:
        """Return the authority-owned boundary or fail closed."""
        receipt = self.store.current_observation_receipt()
        return None if receipt is None else TrustedObservationBoundary.from_receipt(receipt)


def next_observation_generation(
    last_observation_generation: int | None,
    boundary: TrustedObservationBoundary | None,
) -> int:
    """Allocate the next proving generation from the shared causal domain."""
    last = 0 if last_observation_generation is None else last_observation_generation
    if type(last) is not int or last < 0:
        raise ObservationGenerationContractError(
            "last observation generation is invalid"
        )
    floor = 0 if boundary is None else boundary.generation_floor
    candidate = max(last, floor) + 1
    if boundary is not None and candidate <= boundary.generation_floor:
        raise ObservationGenerationContractError(
            "observation generation is not later than boundary floor"
        )
    return candidate
