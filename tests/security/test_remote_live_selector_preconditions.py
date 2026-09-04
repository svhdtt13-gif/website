#!/usr/bin/env python3
"""Slice 10 live-verification precondition planning tests.

These tests deliberately exercise the verifier guard, not a production mutation path.
A blocked precondition must return before the selector action recorder is invoked.
"""
import sys

SKIPPED = "SKIPPED/BLOCKED BY PRECONDITION"
PASS = FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print("PASS: " + name)
    else:
        FAIL += 1
        print("FAIL: " + name + " " + detail)


def plan_live_selector(snapshot):
    """Return a safe verification plan without emitting a selector action."""
    selected_idx = snapshot.get("selectedClientIdx")
    clients = snapshot.get("clients") or []
    selected = next((client for client in clients if client.get("idx") == selected_idx), None)
    if selected_idx is None or selected is None:
        return {"status": SKIPPED, "reason": "selected identity is not reversible"}
    alternates = [client for client in clients if client.get("idx") != selected_idx]
    if not alternates:
        return {"status": SKIPPED, "reason": "no alternate client exists"}
    return {"status": "READY", "reason": "reversible alternate exists"}


def run_verification_plan(snapshot):
    """Model the live verifier boundary; blocked plans never call selector_action."""
    selector_actions = []
    plan = plan_live_selector(snapshot)
    if plan["status"] == "READY":
        selector_actions.append("would-call-selector")
    return plan, selector_actions


def main():
    null_selected = {
        "selectedClientIdx": None,
        "clients": [{"idx": 0, "id": "0:client-a"}, {"idx": 1, "id": "0:client-b"}],
    }
    plan, actions = run_verification_plan(null_selected)
    check("selectedClientIdx=null -> SKIPPED/BLOCKED BY PRECONDITION",
          plan == {"status": SKIPPED, "reason": "selected identity is not reversible"})
    check("selectedClientIdx=null -> zero selector action", actions == [])

    no_alternate = {
        "selectedClientIdx": 0,
        "clients": [{"idx": 0, "id": "0:only-client"}],
    }
    plan, actions = run_verification_plan(no_alternate)
    check("no alternate client -> SKIPPED/BLOCKED BY PRECONDITION",
          plan == {"status": SKIPPED, "reason": "no alternate client exists"})
    check("no alternate client -> zero selector action", actions == [])

    ready = {
        "selectedClientIdx": 0,
        "clients": [{"idx": 0, "id": "0:client-a"}, {"idx": 1, "id": "0:client-b"}],
    }
    plan, actions = run_verification_plan(ready)
    check("selected target plus alternate -> READY", plan["status"] == "READY")
    check("READY plan is the only branch that may emit selector action",
          actions == ["would-call-selector"])

    print(f"\nSUMMARY: {PASS} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
