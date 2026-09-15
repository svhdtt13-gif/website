from __future__ import annotations

import hashlib
from dataclasses import dataclass

from services.canary_envelope import CANARY_CONTRACT_VERSION


@dataclass(frozen=True, slots=True)
class CanaryTransportReceipt:
    fingerprint: str


class NullCanaryTransport:
    kind = "null"

    def __init__(self, contract_version: str = CANARY_CONTRACT_VERSION):
        self.contract_version = contract_version

    def observe(self, envelope_json: str) -> None:
        return None


class RecordingCanaryTransport:
    kind = "recording"

    def __init__(self, contract_version: str = CANARY_CONTRACT_VERSION):
        self.contract_version = contract_version
        self.observations: list[str] = []

    def observe(self, envelope_json: str) -> CanaryTransportReceipt:
        self.observations.append(envelope_json)
        return CanaryTransportReceipt(
            hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        )


class ContractProbeTransport:
    kind = "contract_probe"

    def __init__(self, contract_version: str = CANARY_CONTRACT_VERSION):
        self.contract_version = contract_version
        self.observations: list[str] = []

    def observe(self, envelope_json: str) -> CanaryTransportReceipt:
        self.observations.append(envelope_json)
        return CanaryTransportReceipt(
            hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        )
