#!/usr/bin/env python3
"""Acceptance tests for guarded SQLite generation runtime behavior."""
import multiprocessing
import os
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))
sys.path.insert(0, str(ROOT / "tests" / "security"))

from repositories.aitool import UpstreamError
from repositories.legacy_authority_store import (
    LegacyAuthorityStore,
    LegacyAuthorityStoreFactory,
    legacy_store_path,
)
from repositories.legacy_authority_types import FenceState, ReceiptState
from repositories.sqlite import SQLiteCandidateRepository
from services import sqlite_runtime
from services.legacy_observation_materializer import ObservationMaterializerError
from services.runtime_observation_generation import (
    ObservationGenerationContractError,
    TrustedObservationBoundary,
)
from services.sqlite_runtime import (
    GROUP_MASTER_DATABASE,
    GROUP_PUBLIC_SETTINGS,
    MUTEX_NAME,
    NamedGenerationMutex,
    RuntimeStateError,
    SQLiteRuntimeCoordinator,
)
from test_legacy_observation_materializer import TestKeyProvider, artifact, handoff
from test_sqlite_import import FakeSource, fixture_values, replace_json


def enabled_runtime(
    directory,
    background_refresh=False,
    mutex_name=MUTEX_NAME,
    boundary_provider=None,
    trusted_source_identity="legacy-sole-reader:v1",
):
    return SQLiteRuntimeCoordinator(
        runtime_dir=directory,
        read_enabled=True,
        group_enabled={
            GROUP_MASTER_DATABASE: True,
            GROUP_PUBLIC_SETTINGS: True,
        },
        background_refresh=background_refresh,
        mutex_name=mutex_name,
        source_factory=lambda: FakeSource(fixture_values()),
        trusted_source_identity=trusted_source_identity,
        test_only_observation_boundary_provider=boundary_provider,
    )


class FixedBoundaryProvider:
    def __init__(self, boundary):
        self.boundary = boundary

    def current_boundary(self):
        return self.boundary


def mutex_worker(name, entered, release):
    with NamedGenerationMutex(name, timeout_seconds=5).hold():
        entered.put(os.getpid())
        release.wait(5)


def authority_runtime(directory):
    authority = LegacyAuthorityStore.create(
        legacy_store_path(Path(directory) / "authority")
    )
    authority_factory = LegacyAuthorityStoreFactory(
        "materializer:one", TestKeyProvider()
    )
    authority.add_authorization(
        replace(handoff(), target_ref="client:client_1"),
        replace(artifact(), target_ref="client:client_1"),
    )
    authority.request_fence("send-1")
    authority.transition_fence("send-1", FenceState.ACQUIRED)
    accepted = authority.accept_receipt("send-1", "sha256:envelope-1")
    dispatching = authority.begin_dispatch(accepted)
    terminal = authority.terminal_receipt(dispatching, ReceiptState.APPLIED)
    boundary_receipt = authority.append_observation_boundary(
        terminal, "boundary-42"
    )
    runtime = SQLiteRuntimeCoordinator(
        runtime_dir=directory,
        read_enabled=True,
        group_enabled={GROUP_MASTER_DATABASE: True},
        background_refresh=False,
        source_factory=lambda: FakeSource(fixture_values()),
        legacy_authority_store_factory=lambda: authority_factory.open(authority.path),
        trusted_source_identity="legacy-source:one",
    )
    return authority, authority_factory, runtime, boundary_receipt


class SQLiteRuntimeTests(unittest.TestCase):
    def test_verified_generation_reconstructs_normalized_route_shapes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            master = runtime.read(
                "api/master", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            database = runtime.read(
                "client_database.json", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            self.assertEqual(master[0], fixture_values()["api/master"].body)
            self.assertEqual(master[1], 200)
            self.assertEqual(database[0], fixture_values()["client_database.json"].body)
            self.assertEqual(database[1], 200)
            state = runtime.state()
            self.assertEqual(
                state["groups"][GROUP_MASTER_DATABASE]["generation_id"],
                state["current"]["generation_id"],
            )
            self.assertIn("meta", master[0].decode("utf-8"))
            candidate = Path(state["current"]["candidate_path"])
            repository = SQLiteCandidateRepository.open_read_only(candidate)
            self.assertEqual(repository.rows("PRAGMA query_only")[0][0], 1)
            self.assertEqual(
                repository.rows(
                    "SELECT raw_json FROM master_meta WHERE run_id=?",
                    (state["current"]["run_id"],),
                )[0][0],
                '{"source":"test"}',
            )
            repository.close()

    def test_observation_generation_is_distinct_from_opaque_publication_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            current = runtime.state()["current"]
            self.assertIsInstance(current["generation_id"], str)
            self.assertIsInstance(current["observation_generation"], int)
            self.assertNotEqual(
                current["generation_id"], str(current["observation_generation"])
            )
            self.assertEqual(runtime.current_observation_generation(), 1)

    def test_failed_build_does_not_advance_observation_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = SQLiteRuntimeCoordinator(
                directory,
                read_enabled=True,
                group_enabled={GROUP_MASTER_DATABASE: True},
                background_refresh=False,
                source_factory=lambda: FakeSource(fixture_values()),
                trusted_source_identity="legacy-sole-reader:v1",
            )
            original_importer = runtime._importer
            runtime._importer = lambda snapshot, path: type(
                "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
            )()
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            runtime._importer = original_importer
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertEqual(runtime.current_observation_generation(), 1)

    def test_boundary_publication_is_strictly_after_trusted_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            boundary = TrustedObservationBoundary(
                "boundary-42", "send-1", 42, "run-1", "fence-1",
                "source-1", "client:client_1", "epoch-1", 1, "running",
            )
            runtime = enabled_runtime(
                directory,
                background_refresh=False,
                boundary_provider=FixedBoundaryProvider(boundary),
                trusted_source_identity="source-1",
            )
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            current = runtime.state()["current"]
            self.assertEqual(current["observation_generation"], 43)
            self.assertEqual(current["observation_boundary_id"], "boundary-42")
            self.assertEqual(current["observation_generation_floor"], 42)

    def test_production_boundary_wiring_loads_durable_authority_store(self):
        with tempfile.TemporaryDirectory() as directory:
            authority = LegacyAuthorityStore.create(
                legacy_store_path(Path(directory) / "authority")
            )
            try:
                authority_factory = LegacyAuthorityStoreFactory(
                    "materializer:one", TestKeyProvider()
                )
                authority.add_authorization(
                    replace(handoff(), target_ref="client:client_1"),
                    replace(artifact(), target_ref="client:client_1"),
                )
                authority.request_fence("send-1")
                authority.transition_fence("send-1", FenceState.ACQUIRED)
                accepted = authority.accept_receipt("send-1", "sha256:envelope-1")
                dispatching = authority.begin_dispatch(accepted)
                terminal = authority.terminal_receipt(dispatching, ReceiptState.APPLIED)
                boundary_receipt = authority.append_observation_boundary(
                    terminal, "boundary-42"
                )
                runtime = SQLiteRuntimeCoordinator(
                    runtime_dir=directory,
                    read_enabled=True,
                    group_enabled={GROUP_MASTER_DATABASE: True},
                    background_refresh=False,
                    source_factory=lambda: FakeSource(fixture_values()),
                    legacy_authority_store_factory=lambda: authority_factory.open(authority.path),
                    trusted_source_identity="legacy-source:one",
                )
                self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
                self.assertEqual(runtime.current_observation_generation(), 43)
                self.assertEqual(
                    runtime.state()["current"]["observation_boundary_id"],
                    "boundary-42",
                )
                publication = runtime.current_observation_publication()
                self.assertEqual(publication.target_ref, "client:client_1")
                self.assertEqual(publication.requested_state, "running")
                self.assertEqual(publication.observed_state, "running")
                verifying_authority = authority_factory.open(authority.path)
                try:
                    closed_receipt = verifying_authority.get_receipt(
                        boundary_receipt.pre_send_identity
                    )
                    evidence = verifying_authority.require_observation_evidence(
                        closed_receipt
                    )
                finally:
                    verifying_authority.close()
                self.assertEqual(evidence.evidence.snapshot_generation_id, 43)
                self.assertEqual(evidence.evidence.target_ref, "client:client_1")
                self.assertEqual(
                    authority.get_fence("send-1")["state"], FenceState.CLOSED.value
                )
            finally:
                authority.close()

    def test_newer_boundaryless_publication_supersedes_older_proving_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            authority, authority_factory, bounded_runtime, _receipt = authority_runtime(
                directory
            )
            try:
                self.assertTrue(bounded_runtime.refresh_now(GROUP_MASTER_DATABASE))
                ordinary_runtime = SQLiteRuntimeCoordinator(
                    runtime_dir=directory,
                    read_enabled=True,
                    group_enabled={GROUP_MASTER_DATABASE: True},
                    background_refresh=False,
                    source_factory=lambda: FakeSource(fixture_values()),
                    legacy_authority_store_factory=lambda: authority_factory.open(
                        authority.path
                    ),
                    trusted_source_identity="legacy-source:one",
                )
                self.assertTrue(
                    ordinary_runtime.refresh_now(GROUP_MASTER_DATABASE, force=True)
                )

                publication = ordinary_runtime.current_observation_publication()
                self.assertEqual(publication.observation_generation, 44)
                self.assertIsNone(publication.observation_boundary_id)
            finally:
                authority.close()

    def test_publication_precedes_authority_ack_and_proving_event(self):
        with tempfile.TemporaryDirectory() as directory:
            authority, _factory, runtime, _receipt = authority_runtime(directory)
            try:
                events = []
                publish = runtime.ledger.publish
                record_ack = sqlite_runtime.ObservationMaterializer.record_ack
                acknowledge = runtime.ledger.acknowledge

                def tracked_publish(*args, **kwargs):
                    publication = publish(*args, **kwargs)
                    events.append("published")
                    return publication

                def tracked_record_ack(materializer, receipt, evidence):
                    events.append("authority_ack")
                    return record_ack(materializer, receipt, evidence)

                def tracked_acknowledge(generation, attestation_fingerprint):
                    events.append("authority_acked")
                    return acknowledge(generation, attestation_fingerprint)

                with patch.object(runtime.ledger, "publish", side_effect=tracked_publish), patch.object(
                    sqlite_runtime.ObservationMaterializer,
                    "record_ack",
                    new=tracked_record_ack,
                ), patch.object(
                    runtime.ledger, "acknowledge", side_effect=tracked_acknowledge
                ):
                    self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
                self.assertEqual(
                    events, ["published", "authority_ack", "authority_acked"]
                )
            finally:
                authority.close()

    def test_failure_immediately_before_publish_leaves_no_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            authority, authority_factory, runtime, boundary_receipt = authority_runtime(
                directory
            )
            try:
                with patch.object(
                    runtime.ledger,
                    "publish",
                    side_effect=sqlite_runtime.ObservationLedgerError("simulated crash"),
                ):
                    self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
                self.assertIsNone(runtime.ledger.latest_published())
                self.assertIsNone(runtime.ledger.latest_proving())
                verifying = authority_factory.open(authority.path)
                try:
                    with self.assertRaises(ObservationMaterializerError):
                        verifying.require_observation_evidence(boundary_receipt)
                finally:
                    verifying.close()
            finally:
                authority.close()

    def test_failure_after_publish_before_ack_remains_non_proving_and_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            authority, authority_factory, runtime, boundary_receipt = authority_runtime(
                directory
            )
            try:
                with patch.object(
                    sqlite_runtime.ObservationMaterializer,
                    "record_ack",
                    side_effect=ObservationMaterializerError("simulated crash"),
                ):
                    self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
                self.assertEqual(
                    runtime.ledger.latest_published().observation_generation, 43
                )
                self.assertIsNone(runtime.ledger.latest_proving())
                verifying = authority_factory.open(authority.path)
                try:
                    with self.assertRaises(ObservationMaterializerError):
                        verifying.require_observation_evidence(boundary_receipt)
                finally:
                    verifying.close()
                recovered = SQLiteRuntimeCoordinator(
                    runtime_dir=directory,
                    read_enabled=True,
                    group_enabled={GROUP_MASTER_DATABASE: True},
                    background_refresh=False,
                    source_factory=lambda: FakeSource(fixture_values()),
                    legacy_authority_store_factory=lambda: authority_factory.open(authority.path),
                    trusted_source_identity="legacy-source:one",
                )
                self.assertEqual(
                    recovered.state()["current"]["observation_generation"], 43
                )
                self.assertEqual(
                    recovered.ledger.latest_proving().observation_generation, 43
                )
            finally:
                authority.close()

    def test_crash_after_ack_before_authority_acked_recovers_exact_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            authority, authority_factory, runtime, boundary_receipt = authority_runtime(
                directory
            )
            try:
                with patch.object(
                    runtime.ledger,
                    "acknowledge",
                    side_effect=sqlite_runtime.ObservationLedgerError("simulated crash"),
                ):
                    self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
                self.assertEqual(
                    runtime.ledger.latest_published().observation_generation, 43
                )
                self.assertIsNone(runtime.ledger.latest_proving())
                verifying = authority_factory.open(authority.path)
                try:
                    original_evidence = verifying.require_observation_evidence(
                        boundary_receipt
                    ).evidence
                finally:
                    verifying.close()
                recovered = SQLiteRuntimeCoordinator(
                    runtime_dir=directory,
                    read_enabled=True,
                    group_enabled={GROUP_MASTER_DATABASE: True},
                    background_refresh=False,
                    source_factory=lambda: FakeSource(fixture_values()),
                    legacy_authority_store_factory=lambda: authority_factory.open(authority.path),
                    trusted_source_identity="legacy-source:one",
                )
                recovered.state()
                proving = recovered.ledger.latest_proving()
                self.assertEqual(proving.observation_generation, 43)
                verifying = authority_factory.open(authority.path)
                try:
                    closed_receipt = verifying.get_receipt(
                        boundary_receipt.pre_send_identity
                    )
                    recovered_evidence = verifying.require_observation_evidence(
                        closed_receipt
                    ).evidence
                finally:
                    verifying.close()
                self.assertEqual(recovered_evidence, original_evidence)
            finally:
                authority.close()

    def test_authority_lineage_change_after_publish_remains_non_proving(self):
        with tempfile.TemporaryDirectory() as directory:
            authority, authority_factory, runtime, boundary_receipt = authority_runtime(
                directory
            )
            try:
                published = False
                publish = runtime.ledger.publish
                get_receipt = LegacyAuthorityStore.get_receipt

                def publish_then_change(*args, **kwargs):
                    nonlocal published
                    publication = publish(*args, **kwargs)
                    published = True
                    return publication

                def changed_receipt(store, identity):
                    receipt = get_receipt(store, identity)
                    if published:
                        return replace(receipt, authority_epoch="changed-after-publish")
                    return receipt

                with patch.object(
                    runtime.ledger, "publish", side_effect=publish_then_change
                ), patch.object(
                    LegacyAuthorityStore, "get_receipt", new=changed_receipt
                ):
                    self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
                self.assertEqual(
                    runtime.ledger.latest_published().observation_generation, 43
                )
                self.assertIsNone(runtime.ledger.latest_proving())
                verifying = authority_factory.open(authority.path)
                try:
                    with self.assertRaises(ObservationMaterializerError):
                        verifying.require_observation_evidence(boundary_receipt)
                finally:
                    verifying.close()
            finally:
                authority.close()

    def test_observation_gate_rejects_master_database_status_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            candidate = Path(state["current"]["candidate_path"])
            repository = SQLiteCandidateRepository.open_existing(candidate)
            try:
                repository.connection.execute(
                    "UPDATE database_clients SET status='offline' WHERE run_id=? AND client_id=?",
                    (state["current"]["run_id"], "client_1"),
                )
                repository.connection.commit()
                with self.assertRaises(ValueError):
                    repository.observed_state_for_target(
                        state["current"]["run_id"], "client:client_1", "running"
                    )
            finally:
                repository.close()

    def test_observation_gate_requires_both_projection_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            candidate = Path(state["current"]["candidate_path"])
            repository = SQLiteCandidateRepository.open_existing(candidate)
            try:
                repository.connection.execute(
                    "DELETE FROM database_clients WHERE run_id=? AND client_id=?",
                    (state["current"]["run_id"], "client_1"),
                )
                repository.connection.commit()
                with self.assertRaises(ValueError):
                    repository.observed_state_for_target(
                        state["current"]["run_id"], "client:client_1", "running"
                    )
            finally:
                repository.close()

    def test_observation_gate_rejects_missing_master_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            repository = SQLiteCandidateRepository.open_existing(
                Path(state["current"]["candidate_path"])
            )
            try:
                repository.connection.execute(
                    "DELETE FROM database_clients WHERE run_id=? AND client_id=?",
                    (state["current"]["run_id"], "client_1"),
                )
                repository.connection.execute(
                    "DELETE FROM master_clients WHERE run_id=? AND client_id=?",
                    (state["current"]["run_id"], "client_1"),
                )
                repository.connection.commit()
                with self.assertRaises(ValueError):
                    repository.observed_state_for_target(
                        state["current"]["run_id"], "client:client_1", "running"
                    )
            finally:
                repository.close()

    def test_observation_gate_rejects_state_that_does_not_match_request(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            repository = SQLiteCandidateRepository.open_read_only(
                Path(state["current"]["candidate_path"])
            )
            try:
                with self.assertRaises(ValueError):
                    repository.observed_state_for_target(
                        state["current"]["run_id"], "client:client_1", "offline"
                    )
            finally:
                repository.close()

    def test_restart_preserves_observation_generation_order(self):
        with tempfile.TemporaryDirectory() as directory:
            first = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(first.refresh_now(GROUP_MASTER_DATABASE))
            first_generation = first.current_observation_generation()
            second = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(second.refresh_now(GROUP_MASTER_DATABASE, force=True))
            self.assertGreater(
                second.current_observation_generation(), first_generation
            )

    def test_deleted_current_projection_recovers_without_resetting_head(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            first_generation = runtime.current_observation_generation()
            runtime.state_path.unlink()
            recovered = enabled_runtime(directory, background_refresh=False)
            self.assertEqual(
                recovered.state()["current"]["observation_generation"], first_generation
            )
            with self.assertRaises(ObservationGenerationContractError):
                recovered.current_observation_generation()
            self.assertTrue(recovered.refresh_now(GROUP_MASTER_DATABASE, force=True))
            self.assertEqual(recovered.current_observation_generation(), first_generation + 1)

    def test_corrupt_current_projection_recovers_from_head(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            runtime.state_path.write_text("not-json", encoding="utf-8")
            recovered = enabled_runtime(directory, background_refresh=False)
            self.assertEqual(recovered.state()["current"]["observation_generation"], 1)
            with self.assertRaises(ObservationGenerationContractError):
                recovered.current_observation_generation()

    def test_corrupt_current_projection_recovers_from_publication_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            runtime.state_path.write_text("not-json", encoding="utf-8")
            recovered = enabled_runtime(directory, background_refresh=False)
            self.assertEqual(recovered.state()["current"]["observation_generation"], 1)
            with self.assertRaises(ObservationGenerationContractError):
                recovered.current_observation_generation()

    def test_deleted_projection_never_reactivates_fenced_master_group(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            with runtime.write_fence(GROUP_MASTER_DATABASE):
                pass
            runtime.state_path.unlink()
            recovered = enabled_runtime(directory, background_refresh=False)
            self.assertFalse(
                recovered.state()["groups"][GROUP_MASTER_DATABASE]["eligible"]
            )
            with self.assertRaises(ObservationGenerationContractError):
                recovered.current_observation_generation()

    def test_production_wiring_rejects_generic_boundary_store_and_missing_source_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeStateError):
                SQLiteRuntimeCoordinator(
                    directory,
                    legacy_authority_store_factory=object(),
                )
            runtime = SQLiteRuntimeCoordinator(
                directory,
                read_enabled=True,
                group_enabled={GROUP_MASTER_DATABASE: True},
                background_refresh=False,
                source_factory=lambda: FakeSource(fixture_values()),
            )
            with self.assertRaises(ObservationGenerationContractError):
                runtime.trusted_source_identity()

    def test_trusted_source_identity_rejects_secret_bearing_reference(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(
            RuntimeStateError
        ):
            enabled_runtime(
                directory,
                trusted_source_identity="https://legacy/session?token=raw-secret",
            )

    def test_restart_with_different_trusted_source_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            restarted = enabled_runtime(
                directory,
                background_refresh=False,
                trusted_source_identity="legacy-sole-reader:v2",
            )
            with self.assertRaises(ObservationGenerationContractError):
                restarted.state()

    def test_current_projection_mismatch_recovers_from_durable_head(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["observation_generation"] = 99
            sqlite_runtime._atomic_json(runtime.state_path, state)
            response = runtime.read(
                "api/master", lambda: (_ for _ in ()).throw(AssertionError("fallback"))
            )
            self.assertEqual(response[1], 200)
            self.assertEqual(runtime.state()["current"]["observation_generation"], 1)

    def test_old_candidate_without_observation_metadata_cannot_prove(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            candidate = Path(state["current"]["candidate_path"])
            repository = SQLiteCandidateRepository.open_existing(candidate)
            repository.connection.execute(
                "DROP TRIGGER import_run_observation_metadata_immutable_update"
            )
            repository.connection.execute(
                "UPDATE import_runs SET observation_generation=NULL, "
                "publication_generation_id=NULL, observation_source_set=NULL, "
                "source_identity_ref=NULL "
                "WHERE run_id=?",
                (state["current"]["run_id"],),
            )
            repository.connection.commit()
            repository.close()
            fallback = (b"fallback", 200, "text/plain")
            self.assertEqual(runtime.read("api/master", lambda: fallback), fallback)

    def test_settings_uses_separate_group_but_published_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_PUBLIC_SETTINGS))
            response = runtime.read(
                "api/settings", lambda: (_ for _ in ()).throw(AssertionError("HTTP fallback"))
            )
            self.assertEqual(response[0], fixture_values()["api/settings"].body)
            self.assertEqual(response[1], 200)
            state = runtime.state()
            self.assertEqual(
                state["groups"][GROUP_PUBLIC_SETTINGS]["generation_id"],
                state["current"]["generation_id"],
            )

    def test_failed_staging_is_never_current(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            runtime._importer = lambda snapshot, path: type(
                "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
            )()
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIsNone(runtime.state()["current"])
            self.assertFalse(list(Path(directory).glob("*.staging")))

    def test_checkpoint_failure_never_publishes(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)

            def fail_close(repository):
                try:
                    raise sqlite3.DatabaseError("checkpoint failed")
                finally:
                    repository.connection.close()

            with patch.object(
                SQLiteCandidateRepository,
                "close",
                autospec=True,
                side_effect=fail_close,
            ):
                self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIsNone(runtime.state()["current"])
            self.assertFalse(
                [
                    path
                    for path in Path(directory).glob("*.sqlite3")
                    if path.name != "runtime_observation_publications.sqlite3"
                ]
            )

    def test_refresh_lease_prevents_parallel_importers_past_nominal_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            runtime.refresh_timeout_seconds = 1
            started = threading.Event()
            release = threading.Event()
            calls = []

            def slow_import(snapshot, path):
                calls.append(True)
                started.set()
                release.wait(2)
                return type(
                    "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
                )()

            runtime._importer = slow_import
            first_result = []
            first = threading.Thread(
                target=lambda: first_result.append(
                    runtime.refresh_now(GROUP_MASTER_DATABASE)
                )
            )
            first.start()
            self.assertTrue(started.wait(2))
            time.sleep(1.1)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertEqual(len(calls), 1)
            release.set()
            first.join(3)
            self.assertEqual(first_result, [False])

    def test_cross_process_pending_group_survives_active_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            state = runtime.state()
            state["refresh_lease"] = {
                "token": "other-process",
                "owner_pid": 987654,
                "started_at": "2026-09-06T10:00:00+00:00",
                "heartbeat_at": "2026-09-06T10:00:01+00:00",
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
            sqlite_runtime._atomic_json(runtime.state_path, state)
            self.assertTrue(runtime.refresh_now(GROUP_PUBLIC_SETTINGS))
            self.assertEqual(
                runtime.state()["refresh_pending"], [GROUP_PUBLIC_SETTINGS]
            )
            with patch.object(runtime, "request_refresh", return_value=True) as request:
                runtime._schedule_shared_pending()
            self.assertEqual(runtime.state()["refresh_pending"], [])
            request.assert_called_once_with(GROUP_PUBLIC_SETTINGS)

    def test_failed_owner_releases_lease_before_draining_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            runtime._importer = lambda snapshot, path: type(
                "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
            )()
            state = runtime.state()
            state["refresh_pending"] = [GROUP_PUBLIC_SETTINGS]
            sqlite_runtime._atomic_json(runtime.state_path, state)
            with patch.object(runtime, "request_refresh", return_value=True) as request:
                self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            self.assertIsNone(state["refresh_lease"])
            self.assertEqual(state["refresh_pending"], [])
            request.assert_called_once_with(GROUP_PUBLIC_SETTINGS)

    def test_cleanup_retries_until_generation_mutex_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            started = threading.Event()
            release_build = threading.Event()

            def failing_import(snapshot, path):
                started.set()
                release_build.wait(2)
                return type(
                    "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
                )()

            runtime._importer = failing_import
            state = runtime.state()
            state["refresh_pending"] = [GROUP_PUBLIC_SETTINGS]
            sqlite_runtime._atomic_json(runtime.state_path, state)
            result = []
            pending_scheduled = threading.Event()

            def schedule_pending(_group):
                pending_scheduled.set()
                return True

            owner = threading.Thread(
                target=lambda: result.append(
                    runtime.refresh_now(GROUP_MASTER_DATABASE)
                )
            )
            with patch.object(
                runtime, "request_refresh", side_effect=schedule_pending
            ) as request:
                owner.start()
                self.assertTrue(started.wait(2))
                with runtime._mutex.hold():
                    release_build.set()
                    owner.join(2)
                    self.assertEqual(result, [False])
                    self.assertIsNotNone(runtime.state()["refresh_lease"])
                self.assertTrue(pending_scheduled.wait(3))
                self.assertIsNone(runtime.state()["refresh_lease"])
                self.assertEqual(runtime.state()["refresh_pending"], [])
                request.assert_called_once_with(GROUP_PUBLIC_SETTINGS)

    def test_publication_revalidation_exit_releases_owner_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)

            def stale_build(group, staging, deadline):
                state = runtime.state()
                state["current"] = {"generation_id": "newer"}
                sqlite_runtime._atomic_json(runtime.state_path, state)
                return object(), object()

            runtime._build_candidate = stale_build
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIsNone(runtime.state()["refresh_lease"])

    def test_read_falls_back_while_write_fence_holds_mutex(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            entered = threading.Event()
            release = threading.Event()

            def writer():
                with runtime.write_fence(GROUP_MASTER_DATABASE):
                    entered.set()
                    release.wait(2)

            worker = threading.Thread(target=writer)
            worker.start()
            self.assertTrue(entered.wait(2))
            fallback = (b"http", 200, "application/json")
            self.assertEqual(runtime.read("api/master", lambda: fallback), fallback)
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())

    def test_state_recovery_waits_for_generation_mutex(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            started = threading.Event()
            finished = threading.Event()

            def read_state():
                started.set()
                runtime.state()
                finished.set()

            with runtime._mutex.hold():
                worker = threading.Thread(target=read_state)
                worker.start()
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.wait(0.1))
            worker.join(2)
            self.assertTrue(finished.is_set())

    def test_timeout_before_future_wait_uses_deferred_lease_handoff(self):
        with tempfile.TemporaryDirectory() as directory:
            clock_values = iter((0.0, 0.0, 2.0))
            runtime = SQLiteRuntimeCoordinator(
                runtime_dir=directory,
                read_enabled=True,
                group_enabled={GROUP_MASTER_DATABASE: True},
                refresh_timeout_seconds=1,
                background_refresh=False,
                clock=lambda: next(clock_values),
                source_factory=lambda: FakeSource(fixture_values()),
                trusted_source_identity="legacy-sole-reader:v1",
            )
            started = threading.Event()
            release = threading.Event()

            def slow_build(group, staging, deadline):
                started.set()
                release.wait(2)
                return object(), object()

            runtime._build_candidate = slow_build
            result = []
            owner = threading.Thread(
                target=lambda: result.append(
                    runtime.refresh_now(GROUP_MASTER_DATABASE)
                )
            )
            owner.start()
            self.assertTrue(started.wait(2))
            owner.join(2)
            self.assertEqual(result, [False])
            self.assertIsNotNone(runtime.state()["refresh_lease"])
            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and runtime.state()["refresh_lease"]:
                time.sleep(0.05)
            self.assertIsNone(runtime.state()["refresh_lease"])

    def test_captured_at_is_freshness_anchor(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["captured_at"] = "2000-01-01T00:00:00+00:00"
            self.assertFalse(runtime._eligible(state, GROUP_MASTER_DATABASE))

    def test_mutex_timeout_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            runtime.refresh_timeout_seconds = 0.05
            result = []
            with runtime._mutex.hold():
                worker = threading.Thread(
                    target=lambda: result.append(
                        runtime.refresh_now(GROUP_MASTER_DATABASE)
                    )
                )
                worker.start()
                worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(result, [False])

    def test_local_build_timeout_does_not_hold_mutex(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            runtime.refresh_timeout_seconds = 0.05

            def slow_import(snapshot, path):
                time.sleep(0.2)
                return type(
                    "FailedReceipt", (), {"status": "failed", "checks": {"ok": False}}
                )()

            runtime._importer = slow_import
            started = time.monotonic()
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertLess(time.monotonic() - started, 0.15)
            time.sleep(0.25)

    def test_write_fence_requires_observed_master_and_database_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            expected = {
                "endpoint": "api/master",
                "changes": [{"client": "client_2", "name": "Second-new"}],
            }
            with runtime.write_fence(GROUP_MASTER_DATABASE, expected):
                pass
            self.assertFalse(runtime.refresh_now(GROUP_MASTER_DATABASE))
            self.assertIn(GROUP_MASTER_DATABASE, runtime.state()["fences"])

            values = fixture_values()
            replace_json(
                values,
                "api/master",
                lambda payload: payload["clients"][0].update(name="Second-new"),
            )
            replace_json(
                values,
                "client_database.json",
                lambda payload: payload["clients"][0].update(name="Second-new"),
            )
            runtime._source_factory = lambda: FakeSource(values)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            self.assertNotIn(GROUP_MASTER_DATABASE, state["fences"])
            self.assertTrue(state["groups"][GROUP_MASTER_DATABASE]["eligible"])

    def test_normalized_corruption_falls_back_for_each_wave_one_table(self):
        cases = (
            (
                "master_clients",
                "UPDATE master_clients SET raw_json=?",
                ('{"client":"client_2","name":"tampered","group":"fixed","selected":true}',),
                "api/master",
            ),
            (
                "database_clients",
                "UPDATE database_clients SET raw_json=?",
                ('{"idx":9,"client":"client_2","name":"tampered","status":"offline","group":"fixed","selected":true}',),
                "client_database.json",
            ),
            (
                "public_settings",
                "UPDATE public_settings SET auto_telegram=1",
                (),
                "api/settings",
            ),
        )
        for table, statement, args, route in cases:
            with self.subTest(table=table), tempfile.TemporaryDirectory() as directory:
                runtime = enabled_runtime(directory, background_refresh=False)
                self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
                candidate = Path(runtime.state()["current"]["candidate_path"])
                repository = SQLiteCandidateRepository.open_existing(candidate)
                with repository.transaction():
                    repository.connection.execute(statement, args)
                repository.close()
                fallback = (b"http", 200, "application/json")
                self.assertEqual(runtime.read(route, lambda fallback=fallback: fallback), fallback)

    def test_projection_schema_mismatch_recovers_from_head(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["schema_version"] = 999
            sqlite_runtime._atomic_json(runtime.state_path, state)
            response = runtime.read(
                "api/master", lambda: (_ for _ in ()).throw(AssertionError("fallback"))
            )
            self.assertEqual(response[1], 200)

    def test_reader_rebuilds_projection_from_ledger_without_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=False)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            state = runtime.state()
            state["current"]["observation_generation"] = 0
            sqlite_runtime._atomic_json(runtime.state_path, state)
            self.assertEqual(runtime.current_observation_generation(), 1)

    def test_startup_refresh_queues_each_missing_group_while_active(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            runtime._refresh_active = True
            started = runtime.startup_refresh()
            self.assertEqual(
                [group for group, result in started],
                [GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS],
            )
            self.assertEqual(
                set(runtime._refresh_pending),
                {GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS},
            )
            runtime._refresh_pending.clear()
            runtime._refresh_active = False

    def test_startup_refresh_requests_each_enabled_missing_group(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory, background_refresh=True)
            with patch.object(runtime, "request_refresh", return_value=True) as refresh:
                started = runtime.startup_refresh()
            self.assertEqual(
                [group for group, result in started],
                [GROUP_MASTER_DATABASE, GROUP_PUBLIC_SETTINGS],
            )
            self.assertEqual(refresh.call_count, 2)

    def test_pre_dispatch_fence_survives_restart_and_blocks_stale_read(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            with runtime.write_fence(GROUP_MASTER_DATABASE):
                state = runtime.state()
                self.assertFalse(state["groups"][GROUP_MASTER_DATABASE]["eligible"])
                self.assertEqual(
                    state["fences"][GROUP_MASTER_DATABASE]["reason"],
                    "pre_dispatch_write",
                )
            restarted = enabled_runtime(directory)
            self.assertFalse(
                restarted.state()["groups"][GROUP_MASTER_DATABASE]["eligible"]
            )
            self.assertRaises(
                UpstreamError,
                restarted.read,
                "api/master",
                lambda: (_ for _ in ()).throw(
                    UpstreamError(502, b'{"error":"typed http failure"}')
                ),
            )

    def test_ineligible_http_error_never_stale_serves(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            self.assertTrue(runtime.refresh_now(GROUP_MASTER_DATABASE))
            with runtime.write_fence(GROUP_MASTER_DATABASE), self.assertRaises(
                UpstreamError
            ) as error:
                runtime.read(
                    "api/master",
                    lambda: (_ for _ in ()).throw(
                        UpstreamError(503, b'{"error":"http unavailable"}')
                    ),
                )
            self.assertEqual(error.exception.status, 503)

    def test_runtime_eligibility_does_not_change_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = enabled_runtime(directory)
            with runtime.write_fence(GROUP_PUBLIC_SETTINGS):
                pass
            self.assertTrue(runtime.read_enabled)
            self.assertTrue(runtime.group_enabled[GROUP_PUBLIC_SETTINGS])
            self.assertFalse(
                runtime.state()["groups"][GROUP_PUBLIC_SETTINGS]["eligible"]
            )

    @unittest.skipUnless(os.name == "nt", "requires Windows named mutex")
    def test_two_process_named_mutex_is_single_flight(self):
        name = "Local\\WebsiteSQLiteGenerationMutexTest" + str(os.getpid())
        context = multiprocessing.get_context("spawn")
        entered = context.Queue()
        release = context.Event()
        first = context.Process(target=mutex_worker, args=(name, entered, release))
        second = context.Process(target=mutex_worker, args=(name, entered, release))
        first.start()
        first_pid = entered.get(timeout=5)
        self.assertEqual(first_pid, first.pid)
        second.start()
        with self.assertRaises(queue.Empty):
            entered.get(timeout=0.3)
        release.set()
        second_pid = entered.get(timeout=5)
        self.assertIn(second_pid, (first.pid, second.pid))
        first.join(5)
        second.join(5)
        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)


class FlaskRouteTests(unittest.TestCase):
    def _runtime_stub(self):
        class RuntimeStub:
            def __init__(self):
                self.routes = []

            def startup_refresh(self):
                return []

            def configured_enabled(self, _group):
                return False

            def read(self, route, fallback):
                if route not in sqlite_runtime.ROUTE_ENDPOINTS:
                    return fallback()
                self.routes.append(route)
                return fallback()

        return RuntimeStub()

    def test_sqlite_runtime_is_only_called_for_wave_one_routes(self):
        import app as app_module
        from app import create_app

        runtime = self._runtime_stub()
        app = create_app(runtime=runtime)
        app.testing = True
        with patch.dict(
            app_module.READ_HANDLERS,
            {
                "api/master": Mock(
                    return_value=(b'{"meta":{},"clients":[],"schedule":[]}', 200, "application/json")
                ),
                "clients_master.json": Mock(
                    return_value=(b'{"clients":[],"schedule":[]}', 200, "application/json")
                ),
                "client_database.json": Mock(
                    return_value=(b'{"lastUpdated":"x","clients":[],"schedule":[]}', 200, "application/json")
                ),
                "api/settings": Mock(return_value=(b"{}", 200, "application/json")),
            },
        ):
            client = app.test_client()
            expected = {
                "/up/api/master": b'{"meta":{},"clients":[],"schedule":[]}',
                "/up/client_database.json": b'{"lastUpdated":"x","clients":[],"schedule":[]}',
                "/up/api/settings": b"{}",
                "/up/clients_master.json": b'{"clients":[],"schedule":[]}',
            }
            for path, body in expected.items():
                response = client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, body)
                self.assertIn("application/json", response.content_type)
        self.assertEqual(
            runtime.routes,
            ["api/master", "client_database.json", "api/settings"],
        )

    def test_wave_one_typed_errors_preserve_status_and_json_contract(self):
        import app as app_module
        from app import create_app

        cases = (
            ("api/master", "master_service.get_api_master"),
            ("client_database.json", "master_service.get_database"),
            ("api/settings", "settings_service.get_settings"),
            ("clients_master.json", "master_service.get_master"),
        )
        for route, target in cases:
            with self.subTest(route=route):
                app = create_app(runtime=self._runtime_stub())
                app.testing = True
                with patch.dict(
                    app_module.READ_HANDLERS,
                    {
                        route: Mock(
                            side_effect=UpstreamError(503, b'{"error":"typed"}')
                        )
                    },
                ):
                    response = app.test_client().get("/up/" + route)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.data, b'{"error":"typed"}')
                self.assertIn("application/json", response.content_type)

    def test_default_off_write_path_has_no_sqlite_fence(self):
        class RuntimeStub:
            def startup_refresh(self):
                return []

            def configured_enabled(self, _group):
                return False

            def read(self, _route, fallback):
                return fallback()

        import app as app_module
        from app import create_app

        captured = {}

        def handler(_body, _content_type, before_upstream_write=None):
            captured["fence"] = before_upstream_write
            return b"{}", 200, "application/json"

        app = create_app(runtime=RuntimeStub())
        app.testing = True
        with patch("app._write_authorized", return_value=True), patch.dict(
            app_module.WRITE_HANDLERS, {"api/master": handler}, clear=False
        ):
            response = app.test_client().post(
                "/up/api/master",
                data=b"{}",
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(captured["fence"])


if __name__ == "__main__":
    unittest.main()
