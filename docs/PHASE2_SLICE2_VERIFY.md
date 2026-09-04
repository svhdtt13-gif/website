# Phase 2 Slice 2 — write endpoint dau tien (`POST /api/log`)

Baseline (tu `app_public.py` ai tool + review issue #1):
- `POST /api/log` yeu cau Basic auth + `X-DB-Editor: 1`, body JSON
  `{action?, clients?, schedule?}` (thieu -> default `unknown/?/?`).
- 200 `{"status":"logged"}` + append 1 dong vao 3 file
  `cache/action.log`, `activity_history.jsonl`, `change_log.jsonl`.
  JSON sai/rong/sai kieu -> loi JSON cua ai tool (500 theo implementation hien tai)
  va duoc passthrough; khong tu tao idempotency.
- Khong cham cycle state/worker/remote; retry = 2 dong (giu semantics).

Implementation (dung 3 lop, khong SQLite/Session/SSE/Worker/Tunnel):
- `config.WRITE_ALLOWLIST = {"api/log"}` (duy nhat).
- `repositories/aitool.py::post()` — noi duy nhat HTTP write (Basic + X-DB-Editor).
  Co guard `subpath not in WRITE_ALLOWLIST -> 403` va luon tu dat
  `X-DB-Editor: 1`, khong tin header cung ten tu client.
- `services/log.py::append_log(body, content_type)` — forward raw body.
- `app.py::WRITE_HANDLERS` — chi mo `POST /up/api/log`; GET `/up/api/log` -> 403;
  PUT/PATCH/DELETE `/up/api/log` -> 403; POST path READ khac -> 403.
- Route khong JSON-validate va khong ghi file; body bytes + Content-Type duoc
  chuyen nguyen ven den repository/upstream de giu semantics golden reference.

Verify (stub local, KHONG ghi live ai tool):
- `tests/security/test_write_log.py`: **15/15 xanh** (boot, valid POST 200,
  exact raw bytes + Content-Type, Basic/X-DB-Editor, client marker cannot
  override repository marker, empty/malformed/wrong-type JSON passthrough,
  POST path khac 403 + zero upstream write, PUT/PATCH/DELETE 403,
  GET /up/api/log 403, upstream down -> 502 + alive).
- Hoi quy: `tests/security/test_proxy.py` giu nguyen (7 READ path van 28 calls 403).
- AST: `app.py`, `aitool.py`, `log.py`, `config.py` pass `py_compile`.
- Live ai tool: da chay voi marker duy nhat `phase2-slice2-test-*`; ket qua va
  delta-only cleanup duoc ghi tai day sau khi test. Cleanup chi loc bo dung
  dong co marker, khong restore-overwrite va khong xoa append dong thoi.
- Zero-downtime evidence: truoc/sau live test ghi nhan AutoCycle PID,
  `cycle_state.json` byte hash va cycle log; khong restart/stop worker,
  khong doi scheduler ownership, cycle state hash khong doi.
