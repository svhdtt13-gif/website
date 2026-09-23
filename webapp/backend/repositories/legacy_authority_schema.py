from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Final

V3_SCHEMA_SQL: Final = """
CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE handoffs (
 handoff_id TEXT PRIMARY KEY, source_envelope_contract_version TEXT NOT NULL,
 transport_contract_version TEXT NOT NULL, pre_send_identity TEXT NOT NULL UNIQUE, canary_idempotency_key TEXT NOT NULL,
 envelope_fingerprint TEXT NOT NULL, canonical_envelope_json TEXT NOT NULL, operation_kind TEXT NOT NULL,
 target_ref TEXT NOT NULL, binding_generation INTEGER NOT NULL, verified_identity_ref TEXT NOT NULL,
 verified_identity_revision INTEGER NOT NULL, authority_epoch TEXT NOT NULL, fence_counter INTEGER NOT NULL,
 exporter_identity TEXT NOT NULL, exporter_attestation TEXT NOT NULL, UNIQUE(authority_epoch, fence_counter)
);
CREATE TABLE authorization_artifacts (
 pre_send_identity TEXT PRIMARY KEY REFERENCES handoffs(pre_send_identity), handoff_id TEXT NOT NULL UNIQUE REFERENCES handoffs(handoff_id),
  canary_idempotency_key TEXT NOT NULL, canary_run_id TEXT NOT NULL, fence_identity TEXT NOT NULL UNIQUE, authority_epoch TEXT NOT NULL, fence_counter INTEGER NOT NULL,
 source_identity_ref TEXT NOT NULL, artifact_producer_identity TEXT NOT NULL, source_envelope_contract_version TEXT NOT NULL,
 transport_contract_version TEXT NOT NULL, contract_version TEXT NOT NULL, envelope_exporter_identity TEXT NOT NULL,
 operation_kind TEXT NOT NULL, target_ref TEXT NOT NULL, requested_state TEXT NOT NULL,
 pre_operation_observation_generation_floor INTEGER NOT NULL, envelope_fingerprint TEXT NOT NULL,
  authorization_artifact_fingerprint TEXT NOT NULL UNIQUE, UNIQUE(canary_run_id), UNIQUE(authority_epoch, fence_counter)
);
CREATE TABLE fences (
 pre_send_identity TEXT PRIMARY KEY REFERENCES authorization_artifacts(pre_send_identity), canary_run_id TEXT NOT NULL,
 fence_identity TEXT NOT NULL, authority_epoch TEXT NOT NULL, fence_counter INTEGER NOT NULL, state TEXT NOT NULL,
 UNIQUE(fence_identity), UNIQUE(authority_epoch, fence_counter)
);
CREATE UNIQUE INDEX one_active_fence ON fences(1)
 WHERE state IN ('requested','acquired','receipt_accepted','mutation','receipt_terminal','observation_pending','abandoned');
CREATE TABLE fence_history (
 transition_id INTEGER PRIMARY KEY AUTOINCREMENT, pre_send_identity TEXT NOT NULL,
 canary_run_id TEXT NOT NULL, fence_identity TEXT NOT NULL, authority_epoch TEXT NOT NULL, fence_counter INTEGER NOT NULL,
 from_state TEXT, to_state TEXT NOT NULL
);
CREATE TABLE receipts (
 pre_send_identity TEXT PRIMARY KEY REFERENCES authorization_artifacts(pre_send_identity), envelope_fingerprint TEXT NOT NULL,
 canary_idempotency_key TEXT NOT NULL, handoff_id TEXT NOT NULL, canary_run_id TEXT NOT NULL,
 fence_identity TEXT NOT NULL, authority_epoch TEXT NOT NULL, fence_counter INTEGER NOT NULL, source_identity_ref TEXT NOT NULL,
 artifact_producer_identity TEXT NOT NULL, envelope_exporter_identity TEXT NOT NULL,
 source_envelope_contract_version TEXT NOT NULL, transport_contract_version TEXT NOT NULL, contract_version TEXT NOT NULL,
 target_ref TEXT NOT NULL, authorization_artifact_fingerprint TEXT NOT NULL,
 pre_operation_observation_generation_floor INTEGER NOT NULL, state TEXT NOT NULL,
 post_dispatch_observation_boundary_id TEXT, post_dispatch_observation_generation_floor INTEGER
);
CREATE TRIGGER handoffs_immutable_update BEFORE UPDATE ON handoffs BEGIN SELECT RAISE(ABORT, 'handoff immutable'); END;
CREATE TRIGGER handoffs_immutable_delete BEFORE DELETE ON handoffs BEGIN SELECT RAISE(ABORT, 'handoff immutable'); END;
CREATE TRIGGER artifacts_immutable_update BEFORE UPDATE ON authorization_artifacts BEGIN SELECT RAISE(ABORT, 'artifact immutable'); END;
CREATE TRIGGER artifacts_immutable_delete BEFORE DELETE ON authorization_artifacts BEGIN SELECT RAISE(ABORT, 'artifact immutable'); END;
CREATE TRIGGER fence_history_immutable_update BEFORE UPDATE ON fence_history BEGIN SELECT RAISE(ABORT, 'history append only'); END;
CREATE TRIGGER fence_history_immutable_delete BEFORE DELETE ON fence_history BEGIN SELECT RAISE(ABORT, 'history append only'); END;
CREATE TRIGGER fence_binding_immutable BEFORE UPDATE ON fences
  WHEN NEW.pre_send_identity IS NOT OLD.pre_send_identity OR NEW.canary_run_id IS NOT OLD.canary_run_id
  OR NEW.fence_identity IS NOT OLD.fence_identity
 OR NEW.authority_epoch IS NOT OLD.authority_epoch OR NEW.fence_counter IS NOT OLD.fence_counter
 BEGIN SELECT RAISE(ABORT, 'fence binding immutable'); END;
CREATE TRIGGER fence_delete_abandoned BEFORE DELETE ON fences WHEN OLD.state <> 'abandoned'
 BEGIN SELECT RAISE(ABORT, 'only abandoned fence may clear'); END;
CREATE TRIGGER receipt_lineage_immutable BEFORE UPDATE ON receipts
 WHEN NEW.pre_send_identity IS NOT OLD.pre_send_identity OR NEW.envelope_fingerprint IS NOT OLD.envelope_fingerprint
 OR NEW.canary_idempotency_key IS NOT OLD.canary_idempotency_key OR NEW.handoff_id IS NOT OLD.handoff_id
  OR NEW.canary_run_id IS NOT OLD.canary_run_id OR NEW.fence_identity IS NOT OLD.fence_identity
  OR NEW.authority_epoch IS NOT OLD.authority_epoch
 OR NEW.fence_counter IS NOT OLD.fence_counter OR NEW.source_identity_ref IS NOT OLD.source_identity_ref
 OR NEW.artifact_producer_identity IS NOT OLD.artifact_producer_identity
 OR NEW.envelope_exporter_identity IS NOT OLD.envelope_exporter_identity
 OR NEW.source_envelope_contract_version IS NOT OLD.source_envelope_contract_version
 OR NEW.transport_contract_version IS NOT OLD.transport_contract_version OR NEW.contract_version IS NOT OLD.contract_version
 OR NEW.target_ref IS NOT OLD.target_ref OR NEW.authorization_artifact_fingerprint IS NOT OLD.authorization_artifact_fingerprint
  OR NEW.pre_operation_observation_generation_floor IS NOT OLD.pre_operation_observation_generation_floor
  BEGIN SELECT RAISE(ABORT, 'receipt lineage immutable'); END;
CREATE TRIGGER receipt_boundary_immutable_update BEFORE UPDATE ON receipts
 WHEN OLD.post_dispatch_observation_boundary_id IS NOT NULL
  OR OLD.post_dispatch_observation_generation_floor IS NOT NULL
 BEGIN SELECT RAISE(ABORT, 'observation boundary immutable'); END;
CREATE TRIGGER receipt_immutable_delete BEFORE DELETE ON receipts BEGIN SELECT RAISE(ABORT, 'receipt immutable'); END;
"""

V4_UNIQUE_INDEX_SQL: Final = """
CREATE UNIQUE INDEX unique_handoffs_envelope_fingerprint
 ON handoffs(envelope_fingerprint);
"""
V4_SCHEMA_SQL: Final = V3_SCHEMA_SQL + V4_UNIQUE_INDEX_SQL

V5_OBSERVATION_SQL: Final = """
CREATE TABLE observation_boundaries (
 boundary_id TEXT PRIMARY KEY, pre_send_identity TEXT NOT NULL UNIQUE REFERENCES receipts(pre_send_identity),
 generation_floor INTEGER NOT NULL, allocated_after_terminal INTEGER NOT NULL CHECK (allocated_after_terminal = 1)
);
CREATE TABLE observation_materializer_acks (
 receipt_identity TEXT PRIMARY KEY REFERENCES receipts(pre_send_identity), boundary_id TEXT NOT NULL UNIQUE REFERENCES observation_boundaries(boundary_id),
 generation_floor INTEGER NOT NULL, generation_id INTEGER NOT NULL UNIQUE, snapshot_id TEXT NOT NULL, captured_at TEXT NOT NULL,
 materializer_run_id TEXT NOT NULL, source_hash TEXT NOT NULL, source_identity_ref TEXT NOT NULL, canary_run_id TEXT NOT NULL,
 fence_identity TEXT NOT NULL, fence_counter INTEGER NOT NULL, target_ref TEXT NOT NULL, requested_state TEXT NOT NULL,
 observed_state TEXT NOT NULL, attestation_fingerprint TEXT NOT NULL,
 CHECK (generation_id > generation_floor), CHECK (fence_counter > 0)
);
CREATE TABLE observation_generation_ledger (
 generation_id INTEGER PRIMARY KEY, generation_floor INTEGER NOT NULL, record_digest TEXT NOT NULL UNIQUE,
 previous_digest TEXT NOT NULL, attestation_fingerprint TEXT NOT NULL, CHECK (generation_id > generation_floor)
);
CREATE TABLE observation_generation_sequence (
 key TEXT PRIMARY KEY, last_generation_id INTEGER NOT NULL CHECK (last_generation_id >= 0), last_record_digest TEXT NOT NULL
);
CREATE TABLE observation_boundary_sequence (
 key TEXT PRIMARY KEY, last_generation_floor INTEGER NOT NULL CHECK (last_generation_floor >= 0)
);
CREATE TRIGGER observation_boundary_immutable_update BEFORE UPDATE ON observation_boundaries
 BEGIN SELECT RAISE(ABORT, 'observation boundary immutable'); END;
CREATE TRIGGER observation_boundary_immutable_delete BEFORE DELETE ON observation_boundaries
 BEGIN SELECT RAISE(ABORT, 'observation boundary immutable'); END;
CREATE TRIGGER observation_ack_immutable_update BEFORE UPDATE ON observation_materializer_acks
 BEGIN SELECT RAISE(ABORT, 'observation ACK immutable'); END;
CREATE TRIGGER observation_ack_immutable_delete BEFORE DELETE ON observation_materializer_acks
 BEGIN SELECT RAISE(ABORT, 'observation ACK immutable'); END;
CREATE TRIGGER observation_generation_immutable_update BEFORE UPDATE ON observation_generation_ledger
 BEGIN SELECT RAISE(ABORT, 'observation generation immutable'); END;
CREATE TRIGGER observation_generation_immutable_delete BEFORE DELETE ON observation_generation_ledger
 BEGIN SELECT RAISE(ABORT, 'observation generation immutable'); END;
CREATE TRIGGER observation_ack_requires_generation BEFORE INSERT ON observation_materializer_acks
 WHEN NOT EXISTS (SELECT 1 FROM observation_generation_ledger AS ledger
   WHERE ledger.generation_id = NEW.generation_id
   AND ledger.generation_floor = NEW.generation_floor
   AND ledger.attestation_fingerprint = NEW.attestation_fingerprint)
 BEGIN SELECT RAISE(ABORT, 'observation ACK requires generation ledger'); END;
"""
SCHEMA_SQL: Final = V4_SCHEMA_SQL + V5_OBSERVATION_SQL

TRANSITIONAL_INLINE_UNIQUE_SCHEMA_SQL: Final = V3_SCHEMA_SQL.replace(
    "envelope_fingerprint TEXT NOT NULL, canonical_envelope_json",
    "envelope_fingerprint TEXT NOT NULL UNIQUE, canonical_envelope_json",
)


def schema_identity(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    return json.dumps([tuple(row) for row in rows], separators=(",", ":"))


def _canonical_sql(sql: str) -> str:
    return " ".join(line.strip() for line in sql.splitlines() if line.strip())


def _schema_checksum(sql: str) -> str:
    return hashlib.sha256(_canonical_sql(sql).encode()).hexdigest()


def _expected_schema_identity(sql: str) -> str:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(sql)
        return schema_identity(connection)


V3_SCHEMA_CHECKSUM: Final = _schema_checksum(V3_SCHEMA_SQL)
V3_SCHEMA_IDENTITY: Final = _expected_schema_identity(V3_SCHEMA_SQL)
V4_SCHEMA_CHECKSUM: Final = _schema_checksum(V4_SCHEMA_SQL)
V4_SCHEMA_IDENTITY: Final = _expected_schema_identity(V4_SCHEMA_SQL)
TRANSITIONAL_INLINE_UNIQUE_SCHEMA_CHECKSUM: Final = _schema_checksum(
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_SQL
)
TRANSITIONAL_INLINE_UNIQUE_SCHEMA_IDENTITY: Final = _expected_schema_identity(
    TRANSITIONAL_INLINE_UNIQUE_SCHEMA_SQL
)
SCHEMA_CHECKSUM: Final = _schema_checksum(SCHEMA_SQL)
SCHEMA_IDENTITY: Final = _expected_schema_identity(SCHEMA_SQL)
