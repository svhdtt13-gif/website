import contextlib
import http.client
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

from services.binding_authority import AuthorityConfig  # noqa: F401
from services.canary_arming import CanaryArmingService
from services.canary_send import CanarySingleShotService
from services.canary_send_transport import SingleShotCanaryTransport
from services.canary_transport import RecordingCanaryTransport
from services.shadow_dispatch_transport import RecordingShadowTransport

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
        self.canary = CanaryArmingService(
            self.portable_path, self.operational, self.coordinator
        )
        self.sender = CanarySingleShotService(
            self.portable_path, self.operational, self.coordinator,
            arming=self.canary, committed_wait_seconds=0.3,
        )
        self.stubs = []

    def tearDown(self):
        for stub in self.stubs:
            stub.stop()
        self.stubs = []
        super().tearDown()

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
