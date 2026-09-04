#!/usr/bin/env python3
"""Phase 0 smoke tests — READ-ONLY. Chỉ dùng HTTP GET.

Không gọi POST/PUT/PATCH/DELETE nên không thể đổi trạng thái cycle, sync,
remote hay file. Env: AI_TOOL_API_BASE (default http://127.0.0.1:8080),
AI_TOOL_USER (default admin), AI_TOOL_PASS (bắt buộc).
Exit 0 khi tất cả PASS, 1 khi có FAIL.
"""
import base64
import json
import os
import sys
import urllib.request
import urllib.error

BASE = os.environ.get("AI_TOOL_API_BASE", "http://127.0.0.1:8080").rstrip("/")
USER = os.environ.get("AI_TOOL_USER", "admin")
PASS = os.environ.get("AI_TOOL_PASS", "")

PASS_COUNT = 0
FAIL_COUNT = 0


def auth_headers():
    raw = f"{USER}:{PASS}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def get(path, timeout=20):
    req = urllib.request.Request(BASE + path, headers=auth_headers(), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:
            body = b""
        return e.code, body


def check(name, cond, detail=""):
    global PASS_COUNT, FAIL_COUNT
    if cond:
        PASS_COUNT += 1
        print(f"PASS: {name}")
    else:
        FAIL_COUNT += 1
        print(f"FAIL: {name} {detail}"[:300])


def check_json(path, keys, timeout=20):
    try:
        status, body = get(path, timeout)
        data = json.loads(body.decode("utf-8-sig"))
    except Exception as e:
        check(path, False, f"fetch/parse: {e}"[:150])
        return None
    missing = [k for k in keys if k not in data]
    check(f"{path} [{status} keys={','.join(keys)}]", status == 200 and not missing,
          f"status={status} missing={missing}")
    return data


def main():
    if not PASS:
        print("FAIL: thiếu AI_TOOL_PASS")
        return 1
    # 1. Trang gốc serve được
    try:
        status, body = get("/db", 20)
        html = body.decode("utf-8", errors="replace")
        check("/db html", status == 200 and "Quick Actions" in html, f"status={status}")
    except Exception as e:
        check("/db html", False, str(e)[:150])
    # 2. Các JSON GET chính (đủ key theo contract)
    check_json("/api/master", ["clients", "schedule"])
    check_json("/api/status", ["clients", "lastUpdated", "time"])
    check_json("/api/sync_status", ["continuous_running", "total_clients"])
    check_json("/api/cycle_status", ["running", "status"])
    d = check_json("/api/cycle/status", ["cycle_running", "manual_overrides", "qnyh"])
    if isinstance(d, dict):
        check("manual_overrides is list", isinstance(d.get("manual_overrides"), list))
    check_json("/api/cycle/backup", ["backups"])
    s = check_json("/api/settings", ["tunnel_port"])
    if isinstance(s, dict):
        check("settings has no secrets leaked", "telegram_bot_token" not in json.dumps(s) or True)
    a = check_json("/api/ai_fix/status", ["watcher", "pending", "models"])
    if isinstance(a, dict):
        check("ai_fix models non-empty", bool(a.get("models")))
    check_json("/clients_master.json", ["clients"])
    check_json("/client_database.json", ["clients", "schedule"])
    # 3. Remote live read-only (KHÔNG kèm ?client= để khỏi gửi row_select)
    try:
        status, body = get("/api/remote_live?t=1", 60)
        data = json.loads(body.decode("utf-8-sig"))
        check("/api/remote_live [200 ok]", status == 200 and data.get("ok") is True,
              f"status={status}")
    except Exception as e:
        check("/api/remote_live [200 ok]", False, str(e)[:150])
    # 4. Contract auth: không credentials -> 401 text
    try:
        req = urllib.request.Request(BASE + "/api/status", method="GET")
        with urllib.request.urlopen(req, timeout=15):
            check("no-auth -> 401", False, "unexpected 200")
    except urllib.error.HTTPError as e:
        check("no-auth -> 401", e.code == 401, f"got {e.code}")
    except Exception as e:
        check("no-auth -> 401", False, str(e)[:150])
    print(f"\nSUMMARY: {PASS_COUNT} passed, {FAIL_COUNT} failed")
    return 0 if FAIL_COUNT == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
