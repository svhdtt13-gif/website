from __future__ import annotations

import hashlib
import re


class ShadowTransportReceipt:
    def __init__(self, fingerprint: str):
        self.fingerprint = fingerprint


class NullShadowTransport:
    kind = "null"

    def record(self, envelope_json: str) -> None:
        return None


class RecordingShadowTransport:
    kind = "recording"

    def __init__(self, reported_fingerprint: str | None = None):
        self.records: list[str] = []
        self.reported_fingerprint = reported_fingerprint

    def record(self, envelope_json: str) -> ShadowTransportReceipt:
        self.records.append(envelope_json)
        if self.reported_fingerprint is None:
            fingerprint = hashlib.sha256(envelope_json.encode("utf-8")).hexdigest()
        elif re.fullmatch(r"[0-9a-fA-F]{64}", str(self.reported_fingerprint)):
            fingerprint = str(self.reported_fingerprint).lower()
        else:
            fingerprint = hashlib.sha256(
                str(self.reported_fingerprint).encode("utf-8")
            ).hexdigest()
        return ShadowTransportReceipt(fingerprint)
