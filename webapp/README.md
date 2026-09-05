# Webapp proxy + Phase 2 write actions

Dashboard doc du lieu tu ai tool qua proxy cung origin.
Write actions require the website Bearer token and remain server-side through the
repository/service boundary. The current implemented writes are:

- `POST /up/api/log`
- `POST /up/api/cycle/backup`
- `POST /up/api/settings`
- guarded display-name-only CAS `POST /up/api/master`
- guarded `DELETE /up/api/cycle/backup/<name>`
- guarded `POST /up/api/settings/test_telegram`
- guarded `POST /up/api/settings/open_browser`
- guarded `POST /up/api/ai_fix`

The following remain intentionally blocked and are not in write dispatch:

- Bundle 2: `DELETE /up/api/ai_fix/answers`, `POST /up/api/ai_fix/watcher`,
  `POST /up/api/sync_remote`, `POST /up/api/sync_all`.
- Bundle 3: `POST /up/api/clear_history`, backup restore, `POST
  /up/api/cycle/fix`, and `GET/POST /up/api/cycle/url`.
- Bundle 4: `POST /up/api/sync_continuous/<start|stop>`, `POST
  /up/api/cycle/<start|stop>`, and `POST /up/api/alwaysrun/<open|stop>`.

The complete current inventory, safety rationale, and evidence are in
`docs/PHASE2_CLOSEOUT.md`. Bundle 3/4 actions are audit/freeze complete but
NO-GO for implementation; do not add a generic route to expose them.

## Chay

```bat
cd webapp\backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set AI_TOOL_API_BASE=http://127.0.0.1:8080
set AI_TOOL_USER=admin
set AI_TOOL_PASS=<mat-khau-db-web>
set WEBAPP_WRITE_TOKEN=<random-secret-outside-repo>
python proxy.py
```

Mo `http://127.0.0.1:8090`.

## Write boundary

- Every implemented write requires `Authorization: Bearer <WEBAPP_WRITE_TOKEN>`.
- Missing or wrong token returns 401 and makes no request to ai tool.
- `WEBAPP_WRITE_TOKEN` is read only from the environment; it is never hard-coded or committed.
- If the server has no configured token, every write is rejected safely.
- The Bearer gate is the minimum Phase 2 protection, not a session/role system.
- The repository creates Basic Auth + `X-DB-Editor: 1` for ai tool requests; clients cannot override that marker.
- Bundle 1 action routes reject query strings and block non-POST methods before upstream access.
- AI create accepts only the frozen `cycle`, `web`, and `userimport` schema, validates the returned queue basename, and spaces concurrent creates by 1.1 seconds under a named Windows mutex because the golden filename has only second precision.
- Browser URLs are limited to absolute HTTP/HTTPS URLs with a hostname, no userinfo/control/CRLF/NUL, and at most 2048 UTF-8 bytes.
- `POST /up/api/master` is a display-name-only CAS operation; it does not expose full master/schedule writes.
- Backup listing/creation and settings GET/POST semantics are separated in `docs/API_CONTRACT.md`.

## An toan

- Proxy bind `127.0.0.1` — chi may local mo duoc.
- Credentials ai tool va write token nam trong bien moi truong phia server,
  khong bao gio ra trinh duyet. Khong commit file chua secrets.
- Muon tro sang ai tool khac: doi `AI_TOOL_API_BASE`.
