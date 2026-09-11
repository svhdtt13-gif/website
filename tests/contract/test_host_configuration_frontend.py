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
    assert "Promise.allSettled([refreshProfiles(), refreshHosts()])" in source
    assert "Unsafe configuration response." in source


def test_post_failure_does_not_refresh_or_retry_mutation():
    source = read("js/host_configuration.js")
    submit = source[source.index("async function submit("):]

    assert "const MUTATION_FAILURE = 'ERROR — configuration write failed';" in source
    assert source.count("await post(request)") == 1
    assert submit.index("await post(request)") < submit.index("if (!await refreshProjections())")
    assert submit.index("return;") < submit.index("if (!await refreshProjections())")
    assert "status.dataset.state = 'error'; status.textContent = message;" in submit


def test_post_success_with_both_projections_fresh_reports_success():
    source = read("js/host_configuration.js")
    submit = source[source.index("async function submit("):]

    assert "const PROJECTIONS_REFRESHED = 'SUCCESS — write confirmed, projections refreshed';" in source
    assert "const results = await Promise.allSettled([refreshProfiles(), refreshHosts()]);" in source
    assert "result.status === 'fulfilled' && result.value === true" in source
    assert "status.dataset.state = 'success'; status.textContent = PROJECTIONS_REFRESHED;" in submit


def test_post_success_with_profile_host_or_both_projection_failures_reports_warning():
    source = read("js/host_configuration.js")
    submit = source[source.index("async function submit("):]

    assert "const PROJECTIONS_STALE = 'WARNING/STALE — write confirmed, projection refresh failed/stale';" in source
    assert "status.dataset.state = 'warning'; status.textContent = PROJECTIONS_STALE;" in submit
    assert "setMessage(error, '');" in submit
    assert source.count("refreshProfiles()") == 1
    assert source.count("refreshHosts()") == 1
    assert source.count("Promise.allSettled") == 1


def test_projection_refreshers_report_failure_to_the_mutation_flow():
    for name in ("js/profile_manager.js", "js/host_discovery.js"):
        source = read(name)
        assert "return true;" in source
        assert "return false;" in source


def test_configuration_surface_has_no_runtime_controls():
    source = read("js/host_configuration.js")
    forbidden = ("Start process", "Stop process", "Reconnect", "Remote login", "Activate", "Switch")
    assert all(label not in source for label in forbidden)
