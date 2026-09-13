from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from repositories.operational_sqlite import OperationalSQLiteRepository
from repositories.portable_store import BindingError, PortableDomainStore

from services.binding_authority_types import (
    AUTHORITY_ELIGIBILITY,
    AuthorityClock,
    AuthorityConfig,
    AuthorityRejected,
    BindingLease,
    BindingScope,
    FenceIdentity,
    RejectionEvidence,
)


def _correlation_id(request_id: str | None) -> str:
    if isinstance(request_id, str) and re.fullmatch(
        r"(?:corr-[0-9a-f]{16}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        request_id,
        re.IGNORECASE,
    ):
        return request_id
    return "corr-" + secrets.token_hex(8)


def _reject(correlation_id: str, reason_class: str) -> AuthorityRejected:
    return AuthorityRejected(RejectionEvidence(reason_class, correlation_id))


class BindingAuthorityCoordinator:
    def __init__(
        self,
        portable_path: Path | str,
        operational: OperationalSQLiteRepository,
        clock: AuthorityClock,
        config: AuthorityConfig,
    ):
        self.portable_path = Path(portable_path)
        self.operational = operational
        self.clock = clock
        self.config = config

    def establish_fresh_authority(self) -> FenceIdentity:
        with self.operational.transaction():
            epoch = secrets.token_hex(16)
            self.operational.connection.execute(
                "UPDATE authority_state SET authority_epoch=?, fence_counter=0, "
                "quarantined=0 WHERE singleton=1",
                (epoch,),
            )
            self.operational.connection.execute(
                "UPDATE fenced_leases SET state='EXPIRED', "
                "release_reason='coordinator_restart' "
                "WHERE state IN ('ACQUIRED', 'HEARTBEATING')"
            )
        self.operational.requires_fresh_bootstrap = False
        return FenceIdentity(epoch, 0)

    def acquire(
        self,
        scope: BindingScope,
        owner_id: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> BindingLease:
        correlation_id = _correlation_id(request_id)
        self._validate_request(scope, owner_id, idempotency_key, correlation_id)
        snapshot = self._require_snapshot(scope, correlation_id)
        now = self._now(correlation_id)
        expires = now + self.config.lease_ttl
        with self.operational.transaction():
            state = self._authority_state(correlation_id)
            if state["quarantined"]:
                raise _reject(correlation_id, "authority_quarantined")
            existing = self.operational.connection.execute(
                "SELECT * FROM fenced_leases WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if self._same_lease_request(existing, scope, owner_id):
                    current = self._current_lease_row(
                        self._lease_from_row(existing, correlation_id),
                        owner_id,
                        now,
                        correlation_id,
                        idempotency_key,
                    )
                    self._revalidate_snapshot(scope, snapshot, correlation_id)
                    return self._lease_from_row(current, correlation_id)
                raise _reject(correlation_id, "idempotency_key_conflict")
            live = self.operational.connection.execute(
                "SELECT expires_at FROM fenced_leases WHERE host_id=? AND profile_id=? "
                "AND binding_generation=? AND verified_identity_ref=? "
                "AND state IN ('ACQUIRED','HEARTBEATING')",
                self._scope_values(scope),
            ).fetchone()
            if live is not None:
                reason = (
                    "expired_lease_requires_reconciliation"
                    if live["expires_at"] <= _stamp(now)
                    else "binding_lease_held"
                )
                raise _reject(correlation_id, reason)
            counter = int(state["fence_counter"]) + 1
            self.operational.connection.execute(
                "UPDATE authority_state SET fence_counter=? WHERE singleton=1",
                (counter,),
            )
            lease_id = "lease-" + secrets.token_hex(16)
            self.operational.connection.execute(
                "INSERT INTO fenced_leases "
                "(lease_id, host_id, profile_id, binding_generation, "
                "verified_identity_ref, owner_id, authority_epoch, fence_counter, "
                "idempotency_key, state, acquired_at, heartbeat_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACQUIRED', ?, ?, ?)",
                (
                    lease_id, *self._scope_values(scope), owner_id,
                    state["authority_epoch"], counter, idempotency_key,
                    _stamp(now), _stamp(now), _stamp(expires),
                ),
            )
            row = self.operational.connection.execute(
                "SELECT * FROM fenced_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
            self._revalidate_snapshot(scope, snapshot, correlation_id)
        return self._lease_from_row(row, correlation_id)

    def heartbeat(
        self,
        lease: BindingLease,
        owner_id: str,
        request_id: str | None = None,
    ) -> BindingLease:
        correlation_id = _correlation_id(request_id)
        self._validate_request(
            lease.scope, owner_id, lease.idempotency_key, correlation_id
        )
        snapshot = self._require_snapshot(lease.scope, correlation_id)
        now = self._now(correlation_id)
        expires = now + self.config.lease_ttl
        with self.operational.transaction():
            row = self._current_lease_row(lease, owner_id, now, correlation_id)
            updated = self.operational.connection.execute(
                "UPDATE fenced_leases SET state='HEARTBEATING', heartbeat_at=?, expires_at=? "
                "WHERE lease_id=? AND state IN ('ACQUIRED','HEARTBEATING')",
                (_stamp(now), _stamp(expires), row["lease_id"]),
            ).rowcount
            if updated != 1:
                raise _reject(correlation_id, "heartbeat_fenced")
            row = self.operational.connection.execute(
                "SELECT * FROM fenced_leases WHERE lease_id=?", (lease.lease_id,)
            ).fetchone()
            self._revalidate_snapshot(lease.scope, snapshot, correlation_id)
        return self._lease_from_row(row, correlation_id)

    def release(
        self,
        lease: BindingLease,
        owner_id: str,
        reason: str,
        request_id: str | None = None,
    ) -> None:
        correlation_id = _correlation_id(request_id)
        if not isinstance(reason, str) or not reason.strip():
            raise _reject(correlation_id, "release_reason_required")
        self._validate_request(
            lease.scope, owner_id, lease.idempotency_key, correlation_id
        )
        snapshot = self._require_snapshot(lease.scope, correlation_id)
        now = self._now(correlation_id)
        with self.operational.transaction():
            self._current_lease_row(lease, owner_id, now, correlation_id)
            updated = self.operational.connection.execute(
                "UPDATE fenced_leases SET state='RELEASED', release_reason=? "
                "WHERE lease_id=? AND state IN ('ACQUIRED','HEARTBEATING')",
                (reason.strip(), lease.lease_id),
            ).rowcount
            if updated != 1:
                raise _reject(correlation_id, "release_fenced")
            self._revalidate_snapshot(lease.scope, snapshot, correlation_id)

    def reconcile_expired(
        self, lease: BindingLease, request_id: str | None = None
    ) -> None:
        """Explicitly invalidate an expired lease before a later acquisition."""
        correlation_id = _correlation_id(request_id)
        self._validate_request(
            lease.scope, lease.owner_id, lease.idempotency_key, correlation_id
        )
        snapshot = self._require_snapshot(lease.scope, correlation_id)
        now = self._now(correlation_id)
        with self.operational.transaction():
            state = self._authority_state(correlation_id)
            if state["quarantined"]:
                raise _reject(correlation_id, "authority_quarantined")
            row = self.operational.connection.execute(
                "SELECT * FROM fenced_leases WHERE lease_id=? AND owner_id=? "
                "AND host_id=? AND profile_id=? AND binding_generation=? "
                "AND verified_identity_ref=? AND authority_epoch=? AND fence_counter=? "
                "AND state IN ('ACQUIRED','HEARTBEATING')",
                (
                    lease.lease_id,
                    lease.owner_id,
                    *self._scope_values(lease.scope),
                    lease.fence.authority_epoch,
                    lease.fence.fence_counter,
                ),
            ).fetchone()
            if row is None:
                raise _reject(correlation_id, "lease_not_current")
            if self._lease_expiration(row, correlation_id) > now:
                raise _reject(correlation_id, "lease_not_expired")
            self._revalidate_snapshot(lease.scope, snapshot, correlation_id)
            updated = self.operational.connection.execute(
                "UPDATE fenced_leases SET state='EXPIRED', "
                "release_reason='explicit_reconciliation' WHERE lease_id=? "
                "AND state IN ('ACQUIRED','HEARTBEATING')",
                (lease.lease_id,),
            ).rowcount
            if updated != 1:
                raise _reject(correlation_id, "reconciliation_fenced")
            self._revalidate_snapshot(lease.scope, snapshot, correlation_id)

    def check_eligibility(
        self,
        lease: BindingLease,
        owner_id: str,
        idempotency_key: str,
        request_id: str | None = None,
    ) -> str:
        correlation_id = _correlation_id(request_id)
        self._validate_request(
            lease.scope, owner_id, idempotency_key, correlation_id
        )
        snapshot = self._require_snapshot(lease.scope, correlation_id)
        with self.operational.transaction(immediate=False):
            self._current_lease_row(
                lease, owner_id, self._now(correlation_id), correlation_id,
                idempotency_key,
            )
            self._revalidate_snapshot(lease.scope, snapshot, correlation_id)
        return AUTHORITY_ELIGIBILITY

    def _require_snapshot(
        self, scope: BindingScope, correlation_id: str
    ) -> dict[str, object]:
        try:
            store = PortableDomainStore.open(self.portable_path)
            try:
                snapshot = store.binding_snapshot(
                    scope.host_id, scope.profile_id, scope.binding_generation
                )
            finally:
                store.close()
        except (BindingError, OSError, ValueError, sqlite3.DatabaseError):
            raise _reject(
                correlation_id, "portable_binding_unavailable"
            ) from None
        if snapshot["state"] != "ACTIVE":
            raise _reject(correlation_id, "binding_not_authoritative")
        if snapshot["status"] not in {"VERIFIED", "ACTIVE"}:
            raise _reject(correlation_id, "profile_not_verified")
        if snapshot["verified_identity_ref"] != scope.verified_identity_ref:
            raise _reject(correlation_id, "verified_identity_mismatch")
        if not snapshot["origin_ref"] or not snapshot["account_ref"]:
            raise _reject(correlation_id, "binding_records_incomplete")
        return snapshot

    def _revalidate_snapshot(
        self,
        scope: BindingScope,
        initial: dict[str, object],
        correlation_id: str,
    ) -> None:
        current = self._require_snapshot(scope, correlation_id)
        if current != initial:
            raise _reject(correlation_id, "portable_binding_changed")

    def _authority_state(self, correlation_id: str):
        if self.operational.requires_fresh_bootstrap:
            raise _reject(correlation_id, "fresh_authority_bootstrap_required")
        row = self.operational.connection.execute(
            "SELECT authority_epoch, fence_counter, quarantined FROM authority_state "
            "WHERE singleton=1"
        ).fetchone()
        if row is None or not row["authority_epoch"]:
            raise _reject(correlation_id, "authority_state_unavailable")
        return row

    def _current_lease_row(
        self, lease: BindingLease, owner_id: str, now: datetime,
        correlation_id: str,
        idempotency_key: str | None = None,
    ):
        state = self._authority_state(correlation_id)
        if state["quarantined"]:
            raise _reject(correlation_id, "authority_quarantined")
        if (
            lease.fence.authority_epoch != state["authority_epoch"]
            or lease.fence.fence_counter < 1
        ):
            raise _reject(correlation_id, "lease_fence_epoch_stale")
        row = self.operational.connection.execute(
            "SELECT * FROM fenced_leases WHERE lease_id=? AND owner_id=? "
            "AND host_id=? AND profile_id=? AND binding_generation=? "
            "AND verified_identity_ref=? AND authority_epoch=? AND fence_counter=? "
            "AND state IN ('ACQUIRED','HEARTBEATING')",
            (
                lease.lease_id, owner_id, *self._scope_values(lease.scope),
                lease.fence.authority_epoch, lease.fence.fence_counter,
            ),
        ).fetchone()
        if row is None:
            raise _reject(correlation_id, "lease_not_current")
        if idempotency_key is not None and row["idempotency_key"] != idempotency_key:
            raise _reject(correlation_id, "idempotency_key_mismatch")
        if self._lease_expiration(row, correlation_id) <= now:
            raise _reject(correlation_id, "lease_expired_requires_reconciliation")
        current = self.operational.connection.execute(
            "SELECT lease_id FROM fenced_leases WHERE host_id=? AND profile_id=? "
            "AND binding_generation=? AND verified_identity_ref=? "
            "AND state IN ('ACQUIRED','HEARTBEATING')",
            self._scope_values(lease.scope),
        ).fetchall()
        if len(current) != 1 or current[0]["lease_id"] != lease.lease_id:
            raise _reject(correlation_id, "competing_lease_is_current")
        return row

    @staticmethod
    def _same_lease_request(row, scope: BindingScope, owner_id: str) -> bool:
        return (
            row["host_id"], row["profile_id"], row["binding_generation"],
            row["verified_identity_ref"], row["owner_id"],
        ) == (*BindingAuthorityCoordinator._scope_values(scope), owner_id)

    @staticmethod
    def _scope_values(scope: BindingScope) -> tuple[object, ...]:
        return (scope.host_id, scope.profile_id, scope.binding_generation,
                scope.verified_identity_ref)

    @staticmethod
    def _validate_request(
        scope, owner_id, idempotency_key, correlation_id: str
    ) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            scope.host_id, scope.profile_id, scope.verified_identity_ref,
            owner_id, idempotency_key,
        )):
            raise _reject(correlation_id, "authority_identifiers_required")
        if not isinstance(scope.binding_generation, int) or scope.binding_generation < 1:
            raise _reject(correlation_id, "binding_generation_invalid")

    def _now(self, correlation_id: str) -> datetime:
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise _reject(correlation_id, "authority_clock_invalid")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _lease_from_row(row, correlation_id: str) -> BindingLease:
        if row is None:
            raise _reject(correlation_id, "lease_row_unavailable")
        try:
            acquired_at = _parse_stamp(row["acquired_at"])
            heartbeat_at = _parse_stamp(row["heartbeat_at"])
            expires_at = _parse_stamp(row["expires_at"])
        except (TypeError, ValueError):
            raise _reject(correlation_id, "lease_timestamp_invalid") from None
        scope = BindingScope(
            row["host_id"], row["profile_id"], row["binding_generation"],
            row["verified_identity_ref"],
        )
        return BindingLease(
            row["lease_id"], scope, row["owner_id"],
            FenceIdentity(row["authority_epoch"], row["fence_counter"]),
            row["idempotency_key"], row["state"],
            acquired_at, heartbeat_at, expires_at,
        )

    @staticmethod
    def _lease_expiration(row, correlation_id: str) -> datetime:
        try:
            return _parse_stamp(row["expires_at"])
        except (TypeError, ValueError):
            raise _reject(correlation_id, "lease_timestamp_invalid") from None


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
