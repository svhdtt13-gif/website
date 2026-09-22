import threading
from dataclasses import dataclass

from services.legacy_canary_coordinator import LegacyAuthorizationContext

TEST_EXPORTER_IDENTITY = "exporter:canary-test"
TEST_COORDINATOR_IDENTITY = "coordinator:legacy-test"


@dataclass(frozen=True, slots=True)
class HandoffHmacKeyProvider:
    keys: tuple[tuple[str, bytes], ...]

    def key_for(self, identity: str) -> bytes:
        for candidate, key in self.keys:
            if candidate == identity:
                return key
        raise KeyError(identity)


class DeterministicCanaryContextProvider:
    def __init__(self) -> None:
        self.allocations = 0
        self._lock = threading.Lock()

    def context_for(self, handoff, envelope) -> LegacyAuthorizationContext:
        del envelope
        with self._lock:
            self.allocations += 1
        suffix = handoff.handoff_id.removeprefix("handoff-v1-")[:24]
        return LegacyAuthorizationContext(
            canary_run_id="run-" + suffix,
            fence_identity=f"fence:{suffix}:{handoff.fence_counter}",
            source_identity_ref="legacy-source:" + handoff.target_ref,
            artifact_producer_identity=TEST_COORDINATOR_IDENTITY,
            pre_operation_observation_generation_floor=0,
            authority_epoch=handoff.authority_epoch,
            fence_counter=handoff.fence_counter,
        )
