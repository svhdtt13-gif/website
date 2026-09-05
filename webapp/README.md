# Webapp proxy + Phase 2 write actions

Dashboard doc du lieu tu ai tool qua proxy cung origin.
Write actions require the website Bearer token and remain server-side through the
repository/service boundary. The current implemented writes are:

- `POST /up/api/log`
- `POST /up/api/cycle/backup`
- `POST /up/api/settings`
- guarded name-only `POST /up/api/master`
- guarded `DELETE /up/api/cycle/backup/<name>`
- guarded `POST /up/api/settings/test_telegram`
- guarded `POST /up/api/settings/open_browser`

Other write actions remain blocked by the allowlist. Bundle 1 does not modify or
copy `ai tool` files and does not control cycle, sync, tunnel, Telegram settings,
or scheduler ownership directly.

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
- Browser URLs are limited to absolute HTTP/HTTPS URLs with a hostname, no userinfo/control/CRLF/NUL, and at most 2048 UTF-8 bytes.

## An toan

- Proxy bind `127.0.0.1` — chi may local mo duoc.
- Credentials ai tool va write token nam trong bien moi truong phia server,
  khong bao gio ra trinh duyet. Khong commit file chua secrets.
- Muon tro sang ai tool khac: doi `AI_TOOL_API_BASE`.
