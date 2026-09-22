from __future__ import annotations

import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import LegacyAuthorityStore, legacy_store_path
from repositories.legacy_authority_types import (
    AuthorizationArtifact,
    StoredAuthorization,
)

from tests.contract.test_legacy_authority_store import valid_artifact, valid_handoff


class LegacyAuthorityAtomicTests(unittest.TestCase):
    def test_two_store_factories_allocate_once_inside_immediate_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = legacy_store_path(Path(temporary))
            created = LegacyAuthorityStore.create(path)
            created.close()
            barrier = threading.Barrier(2)
            allocation_lock = threading.Lock()
            allocations = 0

            def authorize(_index: int) -> StoredAuthorization:
                nonlocal allocations
                store = LegacyAuthorityStore.open(path)

                def artifact_factory() -> AuthorizationArtifact:
                    nonlocal allocations
                    self.assertTrue(store.connection.in_transaction)
                    with allocation_lock:
                        allocations += 1
                    return valid_artifact()

                try:
                    barrier.wait()
                    return store.get_or_create_authorization(
                        valid_handoff(), artifact_factory
                    )
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                stored = list(executor.map(authorize, range(2)))

            inspection = LegacyAuthorityStore.open(path)
            try:
                self.assertEqual(allocations, 1)
                self.assertEqual(stored[0], stored[1])
                self.assertEqual(
                    inspection.connection.execute(
                        "SELECT COUNT(*) FROM handoffs"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    inspection.connection.execute(
                        "SELECT COUNT(*) FROM authorization_artifacts"
                    ).fetchone()[0],
                    1,
                )
            finally:
                inspection.close()


if __name__ == "__main__":
    unittest.main()
