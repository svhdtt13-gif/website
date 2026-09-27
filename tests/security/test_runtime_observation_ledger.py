"""Security tests for the append-only runtime observation ledger."""

import sqlite3
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from services.runtime_observation_generation import observation_binding_fingerprint
from services.runtime_observation_ledger import (
    ObservationLedgerError,
    PublicationParity,
    PublicationReservation,
    RuntimeObservationPublicationLedger,
    ledger_path,
)


def reservation(path: Path, generation_id: str | None = None) -> PublicationReservation:
    return PublicationReservation(
        snapshot_id="snapshot-1",
        run_id="run-1",
        candidate_path=str(path / "candidate.sqlite3"),
        candidate_schema_version=4,
        candidate_schema_checksum="schema-sha",
        source_hash="source-sha",
        source_set="wave1",
        source_identity_ref="legacy-sole-reader:v1",
        pre_send_identity="send-1",
        canary_run_id="run-1",
        fence_identity="fence-1",
        authority_epoch="epoch-1",
        fence_counter=1,
        target_ref="client:one",
        requested_state="running",
        captured_at="2026-09-24T00:00:00+00:00",
        observation_boundary_id="boundary-42",
        observation_generation_floor=42,
        observation_binding_fingerprint=observation_binding_fingerprint(
            "boundary-42", "send-1", "run-1", "fence-1", "epoch-1", 1,
            "legacy-sole-reader:v1", "client:one", "running", 42,
        ),
        materializer_run_id="materializer-1",
        observed_state="running",
        attestation_fingerprint=None,
        generation_id=generation_id,
    )


class RuntimeObservationLedgerTests(unittest.TestCase):
    def test_reserved_generation_is_aborted_and_never_reused_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = RuntimeObservationPublicationLedger(ledger_path(root))
            reserved = first.reserve(42, reservation(root, "generation-1"))
            first.close()

            second = RuntimeObservationPublicationLedger(ledger_path(root))
            next_reserved = second.reserve(42, reservation(root, "generation-2"))

            self.assertEqual(reserved.observation_generation, 43)
            self.assertEqual(next_reserved.observation_generation, 44)
            connection = sqlite3.connect(str(ledger_path(root)))
            states = connection.execute(
                "SELECT observation_generation,event_kind FROM observation_publications "
                "ORDER BY observation_generation"
            ).fetchall()
            connection.close()
            self.assertEqual(
                [(row[0], row[1]) for row in states],
                [(43, "reserved"), (43, "aborted"), (44, "reserved")],
            )
            second.close()

    def test_authority_ack_event_is_required_for_proving_and_candidate_parity_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = RuntimeObservationPublicationLedger(ledger_path(root))
            reserved = ledger.reserve(42, reservation(root, "generation-1"))
            self.assertIsNone(ledger.latest_published())
            published = ledger.publish(
                reserved.observation_generation,
                "2026-09-24T00:00:01+00:00",
                PublicationParity(
                    candidate_path=reserved.candidate_path,
                    candidate_schema_version=reserved.candidate_schema_version,
                    candidate_schema_checksum=reserved.candidate_schema_checksum,
                    source_identity_ref=reserved.source_identity_ref,
                    pre_send_identity=reserved.pre_send_identity,
                    canary_run_id=reserved.canary_run_id,
                    fence_identity=reserved.fence_identity,
                    authority_epoch=reserved.authority_epoch,
                    fence_counter=reserved.fence_counter,
                    target_ref=reserved.target_ref,
                    requested_state=reserved.requested_state,
                    observation_binding_fingerprint=reserved.observation_binding_fingerprint,
                    materializer_run_id=reserved.materializer_run_id,
                    observed_state=reserved.observed_state,
                    attestation_fingerprint=reserved.attestation_fingerprint,
                ),
            )
            self.assertEqual(published.state, "published")
            self.assertIsNone(published.attestation_fingerprint)
            self.assertEqual(ledger.latest_published().record_digest, published.record_digest)
            self.assertIsNone(ledger.latest_proving())
            proving = ledger.acknowledge(
                reserved.observation_generation, "hmac-sha256:test"
            )
            self.assertEqual(proving.state, "authority_acked")
            self.assertEqual(
                proving.attestation_fingerprint, "hmac-sha256:test"
            )
            self.assertEqual(
                ledger.latest_proving().record_digest, proving.record_digest
            )
            ledger.close()

    def test_tampered_record_digest_fails_closed_before_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = RuntimeObservationPublicationLedger(ledger_path(root))
            ledger.reserve(42, reservation(root, "generation-1"))
            ledger.close()
            connection = sqlite3.connect(str(ledger_path(root)))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE observation_publications SET record_digest='tampered'"
                )
            connection.close()
            self.assertIsNotNone(RuntimeObservationPublicationLedger(ledger_path(root)))

    def test_binding_fingerprint_mismatch_cannot_be_published(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = RuntimeObservationPublicationLedger(ledger_path(root))
            forged = replace(reservation(root), target_ref="client:two")
            reserved = ledger.reserve(42, forged)
            with self.assertRaises(ObservationLedgerError):
                ledger.publish(
                    reserved.observation_generation,
                    "2026-09-24T00:00:01+00:00",
                    PublicationParity(
                        candidate_path=reserved.candidate_path,
                        candidate_schema_version=reserved.candidate_schema_version,
                        candidate_schema_checksum=reserved.candidate_schema_checksum,
                        source_identity_ref=reserved.source_identity_ref,
                        pre_send_identity=reserved.pre_send_identity,
                        canary_run_id=reserved.canary_run_id,
                        fence_identity=reserved.fence_identity,
                        authority_epoch=reserved.authority_epoch,
                        fence_counter=reserved.fence_counter,
                        target_ref=reserved.target_ref,
                        requested_state=reserved.requested_state,
                        observation_binding_fingerprint=reserved.observation_binding_fingerprint,
                        materializer_run_id=reserved.materializer_run_id,
                        observed_state=reserved.observed_state,
                        attestation_fingerprint=reserved.attestation_fingerprint,
                    ),
                )
            ledger.close()

    def test_persisted_trusted_identity_is_rechecked_before_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = RuntimeObservationPublicationLedger(
                ledger_path(root), trusted_source_identity="legacy-sole-reader:v1"
            )
            connection = sqlite3.connect(str(ledger_path(root)))
            connection.execute(
                "UPDATE ledger_meta SET value=? WHERE key='trusted_source_identity'",
                ("legacy-sole-reader:v2",),
            )
            connection.commit()
            connection.close()

            with self.assertRaises(ObservationLedgerError):
                ledger.reserve(42, reservation(root, "generation-1"))
            ledger.close()


if __name__ == "__main__":
    unittest.main()
