from __future__ import annotations

import secrets
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
)


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
        return FenceIdentity(epoch, 0)

    def acquire(
        self, scope: BindingScope, owner_id: str, idempotency_key: str
    ) -> BindingLease:
        self._validate_request(scope, owner_id, idempotency_key)
        self._require_snapshot(scope)
        now = self._now()
        expires = now + self.config.lease_ttl
        with self.operational.transaction():
            state = self._authority_state()
            if state["quarantined"]:
                raise AuthorityRejected("authority is quarantined")
            existing = self.operational.connection.execute(
                "SELECT * FROM fenced_leases WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                if self._same_lease_request(existing, scope, owner_id):
                    return self._lease_from_row(existing)
                raise AuthorityRejected("idempotency key is bound to another lease")
            self.operational.connection.execute(
                "UPDATE fenced_leases SET state='EXPIRED', release_reason='lease_expired' "
                "WHERE host_id=? AND profile_id=? AND binding_generation=? "
                "AND verified_identity_ref=? AND state IN ('ACQUIRED','HEARTBEATING') "
                "AND expires_at<=?",
                (*self._scope_values(scope), _stamp(now)),
            )
            live = self.operational.connection.execute(
                "SELECT 1 FROM fenced_leases WHERE host_id=? AND profile_id=? "
                "AND binding_generation=? AND verified_identity_ref=? "
                "AND state IN ('ACQUIRED','HEARTBEATING')",
                self._scope_values(scope),
            ).fetchone()
            if live is not None:
                raise AuthorityRejected("binding lease is already held")
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
        return self._lease_from_row(row)

    def heartbeat(self, lease: BindingLease, owner_id: str) -> BindingLease:
        self._validate_request(lease.scope, owner_id, lease.idempotency_key)
        self._require_snapshot(lease.scope)
        now = self._now()
        expires = now + self.config.lease_ttl
        with self.operational.transaction():
            row = self._current_lease_row(lease, owner_id, now)
            updated = self.operational.connection.execute(
                "UPDATE fenced_leases SET state='HEARTBEATING', heartbeat_at=?, expires_at=? "
                "WHERE lease_id=? AND state IN ('ACQUIRED','HEARTBEATING')",
                (_stamp(now), _stamp(expires), row["lease_id"]),
            ).rowcount
            if updated != 1:
                raise AuthorityRejected("lease heartbeat was fenced")
            row = self.operational.connection.execute(
                "SELECT * FROM fenced_leases WHERE lease_id=?", (lease.lease_id,)
            ).fetchone()
        return self._lease_from_row(row)

    def release(self, lease: BindingLease, owner_id: str, reason: str) -> None:
        if not isinstance(reason, str) or not reason.strip():
            raise AuthorityRejected("release reason is required")
        self._validate_request(lease.scope, owner_id, lease.idempotency_key)
        self._require_snapshot(lease.scope)
        now = self._now()
        with self.operational.transaction():
            self._current_lease_row(lease, owner_id, now)
            updated = self.operational.connection.execute(
                "UPDATE fenced_leases SET state='RELEASED', release_reason=? "
                "WHERE lease_id=? AND state IN ('ACQUIRED','HEARTBEATING')",
                (reason.strip(), lease.lease_id),
            ).rowcount
            if updated != 1:
                raise AuthorityRejected("lease release was fenced")

    def check_eligibility(
        self, lease: BindingLease, owner_id: str, idempotency_key: str
    ) -> str:
        self._validate_request(lease.scope, owner_id, idempotency_key)
        self._require_snapshot(lease.scope)
        with self.operational.transaction(immediate=False):
            self._current_lease_row(lease, owner_id, self._now(), idempotency_key)
        return AUTHORITY_ELIGIBILITY

    def _require_snapshot(self, scope: BindingScope) -> dict[str, object]:
        try:
            store = PortableDomainStore.open(self.portable_path)
            try:
                snapshot = store.binding_snapshot(
                    scope.host_id, scope.profile_id, scope.binding_generation
                )
            finally:
                store.close()
        except (BindingError, OSError, ValueError) as error:
            raise AuthorityRejected("portable binding snapshot is unavailable") from error
        if snapshot["state"] != "ACTIVE":
            raise AuthorityRejected("binding is not authoritative")
        if snapshot["status"] not in {"VERIFIED", "ACTIVE"}:
            raise AuthorityRejected("profile is not verified")
        if snapshot["verified_identity_ref"] != scope.verified_identity_ref:
            raise AuthorityRejected("verified identity does not match binding")
        if not snapshot["origin_ref"] or not snapshot["account_ref"]:
            raise AuthorityRejected("binding records are incomplete")
        return snapshot

    def _authority_state(self):
        row = self.operational.connection.execute(
            "SELECT authority_epoch, fence_counter, quarantined FROM authority_state "
            "WHERE singleton=1"
        ).fetchone()
        if row is None or not row["authority_epoch"]:
            raise AuthorityRejected("authority state is unavailable")
        return row

    def _current_lease_row(
        self, lease: BindingLease, owner_id: str, now: datetime,
        idempotency_key: str | None = None,
    ):
        state = self._authority_state()
        if state["quarantined"]:
            raise AuthorityRejected("authority is quarantined")
        if (
            lease.fence.authority_epoch != state["authority_epoch"]
            or lease.fence.fence_counter < 1
        ):
            raise AuthorityRejected("lease fence epoch is stale")
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
            raise AuthorityRejected("lease fence does not match current holder")
        if idempotency_key is not None and row["idempotency_key"] != idempotency_key:
            raise AuthorityRejected("idempotency key does not match lease")
        if row["expires_at"] <= _stamp(now):
            self.operational.connection.execute(
                "UPDATE fenced_leases SET state='EXPIRED', release_reason='lease_expired' "
                "WHERE lease_id=?", (lease.lease_id,)
            )
            raise AuthorityRejected("lease is expired")
        current = self.operational.connection.execute(
            "SELECT lease_id FROM fenced_leases WHERE host_id=? AND profile_id=? "
            "AND binding_generation=? AND verified_identity_ref=? "
            "AND state IN ('ACQUIRED','HEARTBEATING')",
            self._scope_values(lease.scope),
        ).fetchall()
        if len(current) != 1 or current[0]["lease_id"] != lease.lease_id:
            raise AuthorityRejected("competing lease is current")
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
    def _validate_request(scope, owner_id, idempotency_key) -> None:
        if not all(isinstance(value, str) and value.strip() for value in (
            scope.host_id, scope.profile_id, scope.verified_identity_ref,
            owner_id, idempotency_key,
        )):
            raise AuthorityRejected("binding authority identifiers are required")
        if not isinstance(scope.binding_generation, int) or scope.binding_generation < 1:
            raise AuthorityRejected("binding_generation must be positive")

    def _now(self) -> datetime:
        now = self.clock.now()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise AuthorityRejected("coordinator clock must return aware UTC time")
        return now.astimezone(timezone.utc)

    @staticmethod
    def _lease_from_row(row) -> BindingLease:
        if row is None:
            raise AuthorityRejected("lease row disappeared")
        scope = BindingScope(
            row["host_id"], row["profile_id"], row["binding_generation"],
            row["verified_identity_ref"],
        )
        return BindingLease(
            row["lease_id"], scope, row["owner_id"],
            FenceIdentity(row["authority_epoch"], row["fence_counter"]),
            row["idempotency_key"], row["state"],
            _parse_stamp(row["acquired_at"]), _parse_stamp(row["heartbeat_at"]),
            _parse_stamp(row["expires_at"]),
        )


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)
