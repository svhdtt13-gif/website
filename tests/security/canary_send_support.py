import contextlib
import http.client
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from repositories.legacy_authority_store import LegacyAuthorityStore, legacy_store_path
from services.binding_authority import AuthorityConfig  # noqa: F401
from services.canary_arming import CanaryArmingService
from services.canary_handoff_export import CanaryHandoffExporter
from services.canary_send import CanarySingleShotService
from services.canary_send_transport import SingleShotCanaryTransport
from services.canary_transport import RecordingCanaryTransport
from services.legacy_canary_coordinator import (
    LegacyCanaryCoordinator,
    LegacyCoordinatorTrust,
)
from services.legacy_handoff_trust import TrustedAckVerifier, TrustedHandoffVerifier
from services.shadow_dispatch_transport import RecordingShadowTransport

from tests.security.canary_handoff_support import (
    TEST_COORDINATOR_IDENTITY,
    TEST_EXPORTER_IDENTITY,
    DeterministicCanaryContextProvider,
    HandoffHmacKeyProvider,
)
from tests.security.shadow_dispatch_support import ShadowDispatchFixture

TEST_CREDENTIAL = "test-credential-9f3a"


class StubBehavior:
    def __init__(self, mode="success", delay=0.0, status=200):
        self.mode = mode
        self.delay = delay
        self.status = status


class CanaryStubHandler(BaseHTTPRequestHandler):
    server_version = "CanaryStub/1"

    def log_message(self, *args):
        pass

    def _write(self, payload):
        try:
            self.wfile.write(payload)
        except (OSError, http.client.HTTPException, ValueError):
            self.server.write_errors.append(self.path)

    def do_POST(self):
        server = self.server
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = self.rfile.read(length) if length else b""
        except (OSError, http.client.HTTPException, ValueError):
            body = b""
        with server.lock:
            server.hits.append(
                {
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": body,
                }
            )
            behavior = server.behavior
        import time

        if behavior.delay:
            time.sleep(behavior.delay)
        if behavior.mode == "success":
            payload = json.dumps({"ok": True}).encode()
            self.send_response(behavior.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write(payload)
        elif behavior.mode == "reset_after_read":
            with contextlib.suppress(OSError):
                self.connection.shutdown(2)
            with contextlib.suppress(OSError):
                self.connection.close()
        elif behavior.mode == "error_status":
            payload = json.dumps({"ok": False}).encode()
            self.send_response(behavior.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self._write(payload)
        elif behavior.mode == "redirect":
            self.send_response(302)
            self.send_header("Location", "/canary-elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()


class CanaryStubServer:
    def __init__(self, behavior=None):
        self.behavior = behavior or StubBehavior()
        self.lock = threading.Lock()
        self.hits = []
        self.write_errors = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CanaryStubHandler)
        self.server.lock = self.lock
        self.server.hits = self.hits
        self.server.write_errors = self.write_errors
        self.server.behavior = self.behavior
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/canary"

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()

    def hit_count(self):
        with self.lock:
            return len(self.hits)


class CanarySendFixture(ShadowDispatchFixture):
    def setUp(self):
        super().setUp()
        self.legacy_stores = []
        self.handoff_keys = HandoffHmacKeyProvider(
            (
                (TEST_EXPORTER_IDENTITY, b"deterministic-test-exporter-key"),
                (TEST_COORDINATOR_IDENTITY, b"deterministic-test-coordinator-key"),
            )
        )
        self.handoff_exporter = CanaryHandoffExporter(
            TEST_EXPORTER_IDENTITY, self.handoff_keys
        )
        self.handoff_verifier = TrustedHandoffVerifier(
            TEST_EXPORTER_IDENTITY, self.handoff_keys
        )
        self.ack_verifier = TrustedAckVerifier(
            TEST_COORDINATOR_IDENTITY, self.handoff_keys
        )
        self.context_provider = DeterministicCanaryContextProvider()
        self.canary = CanaryArmingService(
            self.portable_path, self.operational, self.coordinator
        )
        self.sender = self.trusted_sender(
            self.operational,
            self.coordinator,
            arming=self.canary,
            committed_wait_seconds=0.3,
            context_provider=self.context_provider,
        )
        self.stubs = []

    def tearDown(self):
        for stub in self.stubs:
            stub.stop()
        self.stubs = []
        for store in self.legacy_stores:
            store.close()
        self.legacy_stores = []
        super().tearDown()

    def execution(self, key="target-key"):
        target = self.targets.record_target(
            self.scope, self.lease, "group_on", "profile-a", "operator-a", key
        )
        claimed = self.targets.claim_target(
            target["target_id"], self.lease, "agent-a"
        )
        return self.executions.create_execution(
            claimed["target_id"], self.lease, "agent-a"
        )

    def trusted_sender(
        self,
        operational,
        coordinator,
        arming=None,
        committed_wait_seconds=2.0,
        context_provider=None,
    ):
        path = legacy_store_path(self.runtime)
        store = (
            LegacyAuthorityStore.open(path)
            if path.exists()
            else LegacyAuthorityStore.create(path)
        )
        self.legacy_stores.append(store)
        provider = context_provider or DeterministicCanaryContextProvider()
        legacy_coordinator = LegacyCanaryCoordinator(
            store,
            provider,
            LegacyCoordinatorTrust(
                self.handoff_verifier,
                TEST_COORDINATOR_IDENTITY,
                self.handoff_keys,
            ),
        )
        return CanarySingleShotService(
            self.portable_path,
            operational,
            coordinator,
            arming=arming,
            committed_wait_seconds=committed_wait_seconds,
            handoff_exporter=self.handoff_exporter,
            legacy_coordinator=legacy_coordinator,
            ack_verifier=self.ack_verifier,
        )

    def close_trusted_sender(self, sender):
        store = sender.legacy_coordinator._store
        self.legacy_stores.remove(store)
        store.close()

    def legacy_artifact_count(self):
        return self.legacy_stores[0].connection.execute(
            "SELECT COUNT(*) FROM authorization_artifacts"
        ).fetchone()[0]

    def stub(self, behavior=None):
        server = CanaryStubServer(behavior).start()
        self.stubs.append(server)
        return server

    def armed_candidate(self, key="target-key"):
        _execution, intent = self.prepared(key)
        shadow = self.shadow.evaluate(
            intent["intent_id"], self.lease, "agent-a", "shadow-" + key,
            RecordingShadowTransport(),
        )
        return self.canary.arm(
            shadow["shadow_evaluation_id"], self.lease, "agent-a",
            RecordingCanaryTransport(),
        )

    def transport(self, stub, timeout=5.0, credential=True):
        return SingleShotCanaryTransport(
            stub.url,
            timeout=timeout,
            credential_provider=(lambda: TEST_CREDENTIAL) if credential else None,
        )

    def send_intent_row(self, pre_send_identity):
        row = self.operational.connection.execute(
            "SELECT * FROM authority_bound_canary_send_intents "
            "WHERE pre_send_identity=?",
            (pre_send_identity,),
        ).fetchone()
        return dict(row) if row is not None else None

    def candidate_row(self, canary_candidate_id):
        return dict(
            self.operational.connection.execute(
                "SELECT * FROM authority_bound_canary_candidates "
                "WHERE canary_candidate_id=?",
                (canary_candidate_id,),
            ).fetchone()
        )
