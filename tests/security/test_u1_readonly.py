#!/usr/bin/env python3
"""Static contract checks for the U1 frontend safety boundary."""
import os
import sys

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

    source = "\n".join((index, api, store, main_js, views))
    check("U1 frontend has no remote-live selector", "remote_live" not in source)
    check("U1 frontend has no write request methods", not any(
        token in source for token in ("method: 'POST'", "method: 'PUT'",
                                      "method: 'PATCH'", "method: 'DELETE'")
    ))
    check("U1 index exposes no form controls", not any(
        token in index.lower() for token in ("<button", "<form", "<input", "<select")
    ))
    for panel in ("cycle", "sync", "aiFix", "settings", "backups"):
        check(f"{panel} panel is present", f'data-panel="{panel}"' in index)
    for label in ("Auto Sync", "AI fix", "Public settings", "Cycle Backups"):
        check(f"{label} label is present", label in index)
    for context_id in ("contextHost", "contextProfile", "contextAccount", "contextIdentity", "contextAuthority", "contextScope"):
        check(f"{context_id} context field is present", f'id="{context_id}"' in index)
    check("context is sourced from a future profile-aware read field",
          "profile_context" in views and "READ ONLY / FAIL CLOSED" in index)
    check("mixed profile context fails closed", "CONTEXT CONFLICT / FAIL CLOSED" in views
          and "Conflicting host/profile context" in views)
    check("cycle stop intent remains visible", "cycle_stopped" not in source
          and "Cycle stop intent" in views and "STOP REQUESTED" in views)
    check("failed reads clear stale panel data", "Promise.allSettled" in store
          and "store[key] = null" in store)
    check("golden db.html boundary is visible", "tools/db.html" in index)
    check("refresh loop is bounded to ten seconds", "setInterval" in main_js and "10000" in main_js)

    print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
