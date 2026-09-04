# Phase 2 Slice 2 — write endpoint dau tien (`POST /api/log`)

Baseline (tu `app_public.py` ai tool + review issue #1):
- `POST /api/log` yeu cau Basic auth + `X-DB-Editor: 1`, body JSON
  `{action?, clients?, schedule?}` (thieu -> default `unknown/?/?`).
- 200 `{"status":"logged"}` + append 1 dong vao 3 file
  `cache/action.log`, `activity_history.jsonl`, `change_log.jsonl`.
  JSON sai -> 500 `{"error":...}` (passthrough, khong tu tao idempotency).
- Khong cham cycle state/worker/remote; retry = 2 dong (giu semantics).

Implementation (dung 3 lop, khong SQLite/Session/SSE/Worker/Tunnel):
- `config.WRITE_ALLOWLIST = {"api/log"}` (duy nhat).
- `repositories/aitool.py::post()` — noi duy nhat HTTP write (Basic + X-DB-Editor).
  Co guard `subpath not in WRITE_ALLOWLIST -> 403`.
- `services/log.py::append_log(body, content_type)` — forward raw body.
- `app.py::WRITE_HANDLERS` — chi mo `POST /up/api/log`; GET `/up/api/log` -> 403;
  PUT/PATCH/DELETE `/up/api/log` -> 403; POST path READ khac -> 403.
- Route/service khong ghi file truc tiep (khong `open()` trong `app.py`/`log.py`).

Verify (stub local, KHONG ghi live ai tool):
- `tests/security/test_write_log.py`: **12/12 xanh** (boot, POST valid 200 +
  Basic/X-DB-Editor, malformed -> 500 passthrough, POST path khac 403 +
  zero upstream write, PUT/PATCH/DELETE 403, GET /up/api/log 403,
  upstream down -> 502 + alive, no-direct-write).
- Hoi quy: `tests/security/test_proxy.py` giu nguyen (7 READ path van 28 calls 403).
- AST: `app.py`, `aitool.py`, `log.py`, `config.py` pass `py_compile`.
- Live ai tool: CHUA test ghi (theo dieu kien review — tranh overwrite concurrent
  append cua AutoCycle). Khi live test: dung marker `phase2-slice2-test`, chup
  truoc/sau, chi xoa delta test, khong restore-overwrite 3 file.
- Zero-downtime: khong restart/stop worker, khong doi scheduler ownership.
