#!/usr/bin/env python3
"""Static contract checks for the U1 frontend safety boundary."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FRONTEND = os.path.join(ROOT, "webapp", "frontend")

PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + " " + detail[:300])


def read(relative):
    with open(os.path.join(FRONTEND, relative), encoding="utf-8") as source:
        return source.read()


def main():
    index = read("index.html")
    api = read(os.path.join("js", "api.js"))
    store = read(os.path.join("js", "store.js"))
    main_js = read(os.path.join("js", "main.js"))
    views = read(os.path.join("js", "views.js"))
    context = read(os.path.join("js", "profile_context.js"))
    policy = read(os.path.join("js", "runtime_policy.js"))

    expected_endpoints = {
        "cycle": "/up/api/cycle/status",
        "cycleSimple": "/up/api/cycle_status",
        "sync": "/up/api/sync_status",
        "general": "/up/api/status",
        "aiFix": "/up/api/ai_fix/status",
        "backups": "/up/api/cycle/backup",
        "settings": "/up/api/settings",
    }
    check("all U1 endpoint paths are present",
          all(path in api for path in expected_endpoints.values()))
    check("endpoint map has no remote-live source", "remote_live" not in api)
    check("endpoint map has no write verb", not any(
        token in api for token in ("POST", "PUT", "PATCH", "DELETE")
    ))
    check("fetch is cache-free and same-origin", "cache: 'no-store'" in api
          and "credentials: 'same-origin'" in api)
    check("frontend has one read-only fetch helper", api.count("fetch(path") == 1)

    source = "\n".join((index, api, store, main_js, views, context, policy))
    check("U1 frontend has no remote-live selector", "remote_live" not in source)
    check("U1 frontend has no write request methods", not any(
        token in source for token in ("method: 'POST'", "method: 'PUT'",
                                      "method: 'PATCH'", "method: 'DELETE'")
    ))
    check("profile manager exposes only a local viewed-profile selector",
          '<select id="profileViewSelect"' in index and "<form" not in index
          and "<input" not in index)
    check("profile manager runtime controls are disabled", "disabled>" in index
          and "Switch runtime account" in index)
    for panel in ("cycle", "sync", "aiFix", "settings", "backups"):
        check(f"{panel} panel is present", f'data-panel="{panel}"' in index)
        check(f"{panel} panel renders provenance", "data-panel-provenance" in index)
    for label in ("Auto Sync", "AI fix", "Public settings", "Cycle Backups"):
        check(f"{label} label is present", label in index)
    for scope_id in ("scopeCycle", "scopeSync", "scopeAiFix", "scopeBackups", "scopeSettings"):
        check(f"{scope_id} provenance field is present", f'id="{scope_id}"' in index)
    check("context is resolved per panel source", "panelContext" in views
          and "PANEL_SOURCES" in views and "sourceKeys" in context)
    check("account participates in completeness and conflict checks",
          "account" in context and "const keys = ['hostId', 'profileId', 'account'" in context)
    check("provenance includes host/profile/account/identity", "provenanceText" in views
          and "account=${valueOrDash(context.account)}" in views)
    check("missing context never borrows another panel", "UNSCOPED READ" in context
          and "PROFILE-SCOPED READ" in context)
    check("mixed profile or account context fails closed", "CONTEXT CONFLICT / FAIL CLOSED" in context
          and "Data suppressed" in context)
    check("cycle stop is separate from sync read permission", "cycleStopRequested" in policy
          and "syncReadAllowed" in policy
          and "Cycle stop effect" in views
          and "Read-only observation/reconciliation" in views)
    check("failed reads clear stale panel data", "Promise.allSettled" in store
          and "store[key] = null" in store)
    check("golden db.html boundary is visible", "tools/db.html" in index)
    check("refresh loop is bounded to ten seconds", "setInterval" in main_js and "10000" in main_js)

    print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
