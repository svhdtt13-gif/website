from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import legacy_store_path
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    Handoff,
    LegacyValidationError,
)


def valid_handoff() -> Handoff:
    return Handoff(
        handoff_id="handoff-1",
        canary_run_id="run-1",
        source_envelope_contract_version="is3b1.v1",
        transport_contract_version="is3b2.v1",
        pre_send_identity="send-1",
        canary_idempotency_key="idem-1",
        envelope_fingerprint="sha256:envelope-1",
        canonical_envelope_json='{"operation_kind":"group_on","target_ref":"client:one"}',
        operation_kind="group_on",
        target_ref="client:one",
        binding_generation=7,
        verified_identity_ref="identity:one",
        verified_identity_revision=3,
        authority_epoch="epoch-1",
        fence_counter=11,
        exporter_identity="exporter:one",
        exporter_attestation="attestation:one",
    )


def valid_artifact() -> AuthorizationArtifact:
    return AuthorizationArtifact(
        handoff_id="handoff-1",
        pre_send_identity="send-1",
        canary_idempotency_key="idem-1",
        canary_run_id="run-1",
        fence_identity="fence:run-1:11",
        authority_epoch="epoch-1",
        fence_counter=11,
        source_identity_ref="legacy-source:one",
        artifact_producer_identity="producer:one",
        source_envelope_contract_version="is3b1.v1",
        transport_contract_version="is3b2.v1",
        contract_version="is3b2.v1",
        envelope_exporter_identity="exporter:one",
        operation_kind="group_on",
        target_ref="client:one",
        requested_state="running",
        pre_operation_observation_generation_floor=41,
        envelope_fingerprint="sha256:envelope-1",
        authorization_artifact_fingerprint="sha256:artifact-1",
    )


class LegacyAuthorityValidationTests(unittest.TestCase):
    def test_path_is_explicit_and_isolated_from_p4(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = Path(temporary)

            result = legacy_store_path(runtime)

            self.assertEqual(result, runtime / "legacy" / "legacy_canary.sqlite3")
            self.assertNotIn("p4_operational.sqlite3", str(result).lower())

    def test_d_drive_and_operational_paths_fail_closed(self) -> None:
        for path in (
            PureWindowsPath("D:/runtime"),
            Path("runtime") / "p4_operational.sqlite3",
        ):
            with self.subTest(path=path), self.assertRaises(LegacyValidationError):
                legacy_store_path(path)

    def test_handoff_rejects_secret_material_and_wrong_contract(self) -> None:
        values = asdict(valid_handoff())
        cases = (
            {**values, "verified_identity_ref": "token:secret-value"},
            {**values, "source_envelope_contract_version": "is3b1.v2"},
            {**values, "operation_kind": "group_off"},
            {**values, "target_ref": "fixed:always-on"},
        )
        for fields in cases:
            with self.subTest(fields=fields), self.assertRaises(LegacyValidationError):
                Handoff(**fields)

    def test_artifact_rejects_binding_mismatch(self) -> None:
        handoff = valid_handoff()
        values = asdict(valid_artifact())
        artifact = AuthorizationArtifact(**{**values, "target_ref": "client:two"})

        with self.assertRaises(LegacyValidationError):
            artifact.require_handoff(handoff)

    def test_requested_state_accepts_running_and_rejects_on(self) -> None:
        AuthorizationArtifact(**asdict(valid_artifact()))

        with self.assertRaises(LegacyValidationError):
            AuthorizationArtifact(**{**asdict(valid_artifact()), "requested_state": "on"})
        with self.assertRaises(LegacyValidationError):
            AuthorizationArtifact(**{**asdict(valid_artifact()), "operation_kind": "group_off"})

    def test_source_identity_is_independent_from_verified_target_identity(self) -> None:
        handoff = valid_handoff()
        artifact = valid_artifact()

        artifact.require_handoff(handoff)

        self.assertNotEqual(artifact.source_identity_ref, handoff.verified_identity_ref)


if __name__ == "__main__":
    unittest.main()
