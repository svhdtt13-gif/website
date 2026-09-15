import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.operational_sqlite import OperationalSQLiteRepository
from repositories.portable_store import PortableDomainStore
from services.authority_bound_targets import AuthorityBoundTargetService
from services.authority_execution import AuthorityExecutionService
from services.binding_authority import (
    AuthorityConfig,
    BindingAuthorityCoordinator,
    BindingScope,
)
from services.dispatch_preparation import DispatchPreparationService
from services.shadow_dispatch import DryRunShadowDispatcher

START = datetime(2026, 9, 12, 5, 0, tzinfo=timezone.utc)


class FixedClock:
    def __init__(self):
        self.current = START

    def now(self):
        return self.current


class ShadowDispatchFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name)
        self.portable_path = self.runtime / "portable.sqlite3"
        portable = PortableDomainStore.create(self.portable_path)
        portable.add_host("host-a", "Host A", "origin-a", START.isoformat())
        portable.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", START.isoformat())
        revision = portable.record_verified_identity(
            "profile-a", "identity-a", START.isoformat()
        )
        portable.bind_profile(
            "binding-a", "host-a", "profile-a", "account-a", 1, "ACTIVE", START.isoformat()
        )
        portable.close()
        self.operational = OperationalSQLiteRepository.create(self.runtime)
        self.clock = FixedClock()
        self.coordinator = BindingAuthorityCoordinator(
            self.portable_path,
            self.operational,
            self.clock,
            AuthorityConfig(lease_ttl=timedelta(seconds=30)),
        )
        self.scope = BindingScope("host-a", "profile-a", 1, "identity-a", revision)
        self.targets = AuthorityBoundTargetService(
            self.portable_path, self.operational, self.coordinator
        )
        self.executions = AuthorityExecutionService(
            self.portable_path, self.operational, self.coordinator
        )
        self.preparation = DispatchPreparationService(
            self.portable_path, self.operational, self.coordinator
        )
        self.shadow = DryRunShadowDispatcher(
            self.portable_path, self.operational, self.coordinator
        )
        self.lease = self.coordinator.acquire(self.scope, "agent-a", "lease-key")

    def tearDown(self):
        self.operational.close()
        self.temp.cleanup()

    def execution(self, key="target-key"):
        target = self.targets.record_target(
            self.scope, self.lease, "refresh", "profile-a", "operator-a", key
        )
        claimed = self.targets.claim_target(target["target_id"], self.lease, "agent-a")
        return self.executions.create_execution(
            claimed["target_id"], self.lease, "agent-a"
        )

    def prepared(self, key="target-key"):
        execution = self.execution(key)
        intent = self.preparation.prepare_intent(
            execution["execution_id"], self.lease, "agent-a"
        )
        return execution, intent

    def shadow_row(self, intent_id):
        return self.operational.rows(
            "SELECT * FROM authority_bound_shadow_evaluations WHERE intent_id=?",
            (intent_id,),
        )[0]
