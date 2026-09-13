from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Protocol

AUTHORITY_ELIGIBILITY: Final = "AUTHORITY_ELIGIBILITY / FENCED_BINDING_MATCH"


class AuthorityClock(Protocol):
    def now(self) -> datetime:
        ...


@dataclass(frozen=True, slots=True)
class AuthorityConfig:
    lease_ttl: timedelta = timedelta(seconds=30)
    max_lease_ttl: timedelta = timedelta(minutes=5)

    def __post_init__(self) -> None:
        if self.lease_ttl <= timedelta(0) or self.lease_ttl > self.max_lease_ttl:
            raise ValueError("lease_ttl must be positive and bounded")


@dataclass(frozen=True, slots=True)
class BindingScope:
    host_id: str
    profile_id: str
    binding_generation: int
    verified_identity_ref: str


@dataclass(frozen=True, slots=True)
class FenceIdentity:
    authority_epoch: str
    fence_counter: int


@dataclass(frozen=True, slots=True)
class BindingLease:
    lease_id: str
    scope: BindingScope
    owner_id: str
    fence: FenceIdentity
    idempotency_key: str
    state: str
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class RejectionEvidence:
    reason_class: str
    correlation_id: str


@dataclass(slots=True)
class AuthorityRejected(RuntimeError):
    evidence: RejectionEvidence

    def __str__(self) -> str:
        return (
            f"authority rejected: {self.evidence.reason_class} "
            f"(correlation_id={self.evidence.correlation_id})"
        )
