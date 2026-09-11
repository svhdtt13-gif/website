#!/usr/bin/env python3
"""Focused authorization and exact-value regressions for host configuration routes."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "webapp" / "backend"))

config = importlib.import_module("config")
create_app = importlib.import_module("app").create_app
PortableDomainStore = importlib.import_module(
    "repositories.portable_store"
).PortableDomainStore

NOW = "2026-09-10T00:00:00+00:00"


class NoopRuntime:
    pass


class HostConfigurationRouteRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "host-root"
        self.root.mkdir()
        self.path = Path(self.temp.name) / "portable.sqlite3"
        store = PortableDomainStore.create(self.path)
        store.add_profile("profile-a", "Profile A", "account-a", "VERIFIED", NOW)
        store.close()

    def tearDown(self):
        self.temp.cleanup()

    def request(self, endpoint, body, token: str | None = "write-secret"):
        with patch.multiple(
            config,
            PORTABLE_STORE_PATH=self.path,
            HOST_AGENT_HOST_ID="host-a",
            HOST_DISCOVERY_ROOT=self.root,
            WEBAPP_WRITE_TOKEN="write-secret",
        ):
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = f"Bearer {token}"
            app = create_app(runtime=NoopRuntime())
            return app.test_client().post(endpoint, data=body, headers=headers)

    @staticmethod
    def registration_body(host_id="host-a", display_name="Host A"):
        return json.dumps({"host_id": host_id, "display_name": display_name}).encode()

    @staticmethod
    def binding_body(host_id="host-a", profile_id="profile-a"):
        return json.dumps({"host_id": host_id, "profile_id": profile_id}).encode()

    def test_binding_requires_bearer_and_does_not_write_on_rejection(self):
        self.request("/up/api/host_registration", self.registration_body())
        before = self.path.read_bytes()
        for token in (None, "wrong"):
            with self.subTest(token=token):
                response = self.request(
                    "/up/api/host_binding", self.binding_body(), token=token
                )
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.get_json(), {"error": "write authentication required"}
                )
                self.assertEqual(response.headers.get("WWW-Authenticate"), "Bearer")
                self.assertEqual(self.path.read_bytes(), before)

    def test_whitespace_aliases_are_rejected_without_write(self):
        before = self.path.read_bytes()
        registration_values = (
            self.registration_body(host_id=" host-a"),
            self.registration_body(host_id="host-a "),
            self.registration_body(display_name=" Host A"),
            self.registration_body(display_name="Host A "),
        )
        for body in registration_values:
            with self.subTest(endpoint="registration", body=body):
                response = self.request("/up/api/host_registration", body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)

        registration = self.request(
            "/up/api/host_registration", self.registration_body()
        )
        self.assertEqual(registration.status_code, 200)
        before = self.path.read_bytes()
        binding_values = (
            self.binding_body(host_id=" host-a"),
            self.binding_body(host_id="host-a "),
            self.binding_body(profile_id=" profile-a"),
            self.binding_body(profile_id="profile-a "),
        )
        for body in binding_values:
            with self.subTest(endpoint="binding", body=body):
                response = self.request("/up/api/host_binding", body)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
