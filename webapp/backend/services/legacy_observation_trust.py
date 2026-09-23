from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import replace
from typing import Final

from repositories.legacy_authority_observation import (
    ObservationEvidence,
    ObservationMaterializerError,
)

from services.legacy_handoff_trust import HmacKeyProvider

_OBSERVATION_DOMAIN: Final = "lcr2d.observation-attestation.v1"


def observation_attestation(
    evidence: ObservationEvidence,
    materializer_identity: str,
    key_provider: HmacKeyProvider,
) -> str:
    if not materializer_identity or materializer_identity != materializer_identity.strip():
        raise ObservationMaterializerError("materializer identity is invalid")
    try:
        key = key_provider.key_for(materializer_identity)
    except KeyError as error:
        raise ObservationMaterializerError("trusted materializer key is unavailable") from error
    if type(key) is not bytes or not key:
        raise ObservationMaterializerError("trusted materializer key is invalid")
    pending = replace(evidence, attestation_fingerprint="pending")
    payload = json.dumps(
        {"domain": _OBSERVATION_DOMAIN, "payload": pending.projection()},
        separators=(",", ":"),
    ).encode("utf-8")
    return "hmac-sha256:" + hmac.new(key, payload, hashlib.sha256).hexdigest()


class DetachedObservationVerifier:
    def __init__(self, materializer_identity: str, key_provider: HmacKeyProvider) -> None:
        self._materializer_identity = materializer_identity
        self._key_provider = key_provider

    def verify(self, evidence: ObservationEvidence) -> str:
        expected = observation_attestation(
            evidence, self._materializer_identity, self._key_provider
        )
        if not hmac.compare_digest(evidence.attestation_fingerprint, expected):
            raise ObservationMaterializerError("detached observation attestation is invalid")
        return expected
