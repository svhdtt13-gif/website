#!/usr/bin/env python3
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "webapp" / "frontend"


def read(name: str) -> str:
    return (FRONTEND / name).read_text(encoding="utf-8")


def test_mount_keeps_configuration_out_of_read_only_dashboard_markup():
    main = read("js/main.js")
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert "./host_configuration.js" in main
    assert 'id="hostConfiguration"' not in index


def test_requests_match_exact_backend_contracts():
    source = read("js/host_configuration.js")

    assert "const HOST_REGISTRATION_ENDPOINT = '/up/api/host_registration';" in source
    assert "const HOST_BINDING_ENDPOINT = '/up/api/host_binding';" in source
    assert "payload: { host_id: hostId, display_name: displayName }" in source
    assert "payload: { host_id: hostId, profile_id: profileId }" in source
    assert "runtime_effect: RUNTIME_EFFECT" not in source
    assert "runtime_authority: RUNTIME_AUTHORITY" not in source
    assert "state: BINDING_STATE" not in source


def test_write_authorization_is_prompted_once_and_request_scoped():
    source = read("js/host_configuration.js")
    helper = re.search(
        r"function authorizationHeader\(\) \{(?P<body>.*?)\n\}", source, re.DOTALL
    )

    assert helper is not None
    assert helper.group("body").count("window.prompt(") == 1
    assert "`Bearer ${operatorToken}`" in helper.group("body")
    assert "Authorization: authorization" in source
    assert source.index("authorizationHeader()") < source.index("fetch(request.endpoint")


def test_form_values_are_sent_without_frontend_normalization():
    source = read("js/host_configuration.js")

    assert "new FormData(form).get(name)" in source
    assert ".trim()" not in source
    assert ".toLowerCase()" not in source
    assert ".toUpperCase()" not in source


def test_operator_token_has_no_storage_dom_url_message_or_log_path():
    source = read("js/host_configuration.js")
    helper = re.search(
        r"function authorizationHeader\(\) \{(?P<body>.*?)\n\}", source, re.DOTALL
    )

    assert helper is not None
    assert "operatorToken" not in source.replace(helper.group(0), "")
    for forbidden in (
        "localStorage",
        "sessionStorage",
        "console.",
        "location.",
        "URLSearchParams",
        "document.cookie",
    ):
        assert forbidden not in source
    for dom_sink in ("textContent", "innerHTML", "insertAdjacentHTML", "setMessage"):
        assert dom_sink not in helper.group("body")


def test_success_requires_server_owned_safety_response():
    source = read("js/host_configuration.js")

    assert "data.runtime_effect !== 'NONE'" in source
    assert "data.runtime_authority?.mode !== 'LEGACY'" in source
    assert "active_runtime_owner !== null" in source
    assert "data.result?.state === 'OFFLINE'" in source
    assert "Promise.all([refreshProfiles(), refreshHosts()])" in source
    assert "Unsafe configuration response." in source


def test_configuration_surface_has_no_runtime_controls():
    source = read("js/host_configuration.js")
    forbidden = ("Start process", "Stop process", "Reconnect", "Remote login", "Activate", "Switch")
    assert all(label not in source for label in forbidden)
