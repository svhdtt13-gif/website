# Fresh DB.HTML Golden Refresh — 2026-09-09

## Authority

This audit was collected directly from the current local `ai tool` project, not
from the historical website API inventory. The live local project is the golden
reference for data, operator behavior, side effects, ownership and public
access.

The audit was read-only. It did not edit `tools/db.html`, Flask code, scripts,
JSON state, flags, logs, backups, remote state or credentials. No write API,
remote mutation, browser/tunnel action or worker control action was called.

Sources inspected:

- `tools/db.html`
- `WebAppControl/flask/app_public.py`
- `start_public_web.ps1`
- `tools/start_onboot.ps1`
- `tools/cache/public_url.txt`
- `tools/AutoCycle.ps1`
- `continuous_sync_remote.ps1`
- `tools/sync_master.ps1`
- `remote_live_read.ps1`
- `remote_toggle_rows.ps1`
- current master/database/settings/cycle/override/watcher state
- website `main` frontend and Phase 2 route close-out overlay

## Fresh data snapshot

- Master: 26 clients, 4 schedule groups; group counts `fixed:5`, `HAMI:5`,
  `MIE:5`, `MNHI:5`, `NA:5`, `none:1`.
- Database: 26 clients, 4 schedule groups, `lastUpdated=2026-09-09`.
- Cycle state: today `2026-09-09`; completed slots are `04:00|HAMI`,
  `08:00|MNHI`, and `12:30|MIE`.
- `cycle_stopped.flag`, `cycle_paused.flag`, `alwaysrun_paused.flag` and
  `workers_restart.flag` are absent at capture time.
- `alwaysrun_override.json` has no disabled clients.
- `manual_override.json` contains active cooldown observations for 8 clients;
  this is an audit/coordination signal and does not gate cycle execution.
- `ai_fix_watcher.json` reports running with `pending=0`; model/runner status
  was recorded without exposing credentials.

## Cloudflare public-access golden invariant

Public access is part of db.html product parity, not an optional deployment
convenience.

Read-only topology evidence at capture time:

- `tools/cache/public_url.txt` contains
  `https://ontario-detection-enhancements-ministers.trycloudflare.com/db`.
- `cloudflared.exe` PID `18792` runs
  `tunnel --url http://127.0.0.1:8080 --no-autoupdate`.
- legacy `app_public.py` PID `8652` listens on `127.0.0.1:8080`.
- website `proxy.py` PID `10920` listens separately on `127.0.0.1:8090`.
- an unauthenticated `HEAD` request to the current public `/db` URL returns
  `401 Unauthorized`, proving the public tunnel reaches the auth-protected
  legacy route rather than an unprotected static page.

Ownership and behavior:

- `start_public_web.ps1` starts the legacy Flask/Waitress service and the
  Cloudflare Quick Tunnel; its origin is fixed to `127.0.0.1:8080` and it
  writes the discovered `/db` URL to `cache/public_url.txt`.
- `tools/start_onboot.ps1` watches/restarts cloudflared and currently chooses a
  live local origin, persisting the latest Quick Tunnel URL to the same file.
- `app_public.py` defaults `DB_WEB_PORT` to `8080`; `/api/cycle/url` and cycle
  fix can stop existing cloudflared, start a new Quick Tunnel, and rewrite the
  URL file. Those legacy controls are not to be invoked by a website canary.
- `db.html` displays the URL from `cache/public_url.txt`; the public URL is
  also included in cycle-fix activity/status output.
- `app_public.py` serves `/db`, `/db.html`, API routes and allowlisted static
  files behind Basic Auth; mutating requests additionally require
  `X-DB-Editor: 1`.

Required invariant: every website public canary must leave the current legacy
Cloudflare process, origin `127.0.0.1:8080`, URL file and authenticated `/db`
route untouched. P7 Named Tunnel is later hardening and is not part of P4.

## Source fingerprints

Hashes identify the audited local inputs. No secret values are included.

| Source | Size | SHA-256 |
|---|---:|---|
| `tools/db.html` | 103983 | `bacfc33d00fb7babe38bd1fa4361f52c52a8fb85ed008f8f08847438f3fb8de4` |
| `WebAppControl/flask/app_public.py` | 75284 | `7a8b9e0e28942ea74ead8a19a292b83aed0810e983448b14e01cf493ef8232b1` |
| `start_public_web.ps1` | 3005 | `b4ad1f6a550b4e8f0a73edc564d861bd6b64d7cd7933f606952c5d5e59ccf8d` |
| `tools/start_onboot.ps1` | 17732 | `c6fe5d58f9b3471bd7e7a5f9e5beefb8bff4af069c50b5865f1ce3a27913e21e` |
| `tools/AutoCycle.ps1` | 49584 | `ed4065694849cffe00bd52f5645fc95a4fa02170b994b62000227db316498b13` |
| `continuous_sync_remote.ps1` | 19485 | `532329c3a82e6ba9c465a93db02e0660fd970b49276feb8c8d51393d4ad91bea` |
| `tools/clients_master.json` | 9714 | `6b0ad5a5a9064b1e471ec459c070d6141754c04bacf9ba239bd9ac45b381abbe` |
| `tools/client_database.json` | 11925 | `b8d6ca9fe6639c9402ad0067e36b3d6a31131f6b60f5f98efc9e52542136292` |
| `tools/cache/cycle_state.json` | 198 | `2187e99f7a53e2744989f9b0a3146618d8cbe3b1509cd9cd852a75ea5305e6f9` |
| `tools/cache/manual_override.json` | 2173 | `0e414f54f4aa9cd6aa5d798daf605d1981e1d28ce6e105cd57c9a770f6c265d7` |
| `tools/cache/public_url.txt` | 72 | `not recorded as a source fingerprint because the URL is ephemeral` |

Settings keys observed: `telegram_bot_token`, `telegram_chat_id`,
`default_browser`, `cloudflared_path`, `tunnel_port`,
`auto_restart_tunnel`, `auto_telegram`, `auto_open_browser`. Secret values
were not read into this evidence.

## Authority and side effects

- `db.html` is the operator UI and calls Flask routes directly with browser
  credentials. Its fetch wrapper adds `X-DB-Editor: 1` to non-GET `/api/*`
  requests.
- `app_public.py` directly reads/writes ai-tool files and invokes PowerShell
  helpers. Basic Auth and the same-origin marker protect the legacy surface.
- `AutoCycle.ps1` owns schedule execution, uses
  `Local\\AutoGhostStory_AutoCycle`, `Local\\AutoGhostStory_DbWrite` and
  `Local\\AutoGhostStory_RemoteWs`, protects Fixed clients, performs remote
  WebSocket actions and writes state/log files.
- `continuous_sync_remote.ps1` owns live remote observation and persisted
  manual overrides.
- `remote_live_read.ps1` is read-mostly but CHOOSE uses remote `row_select`,
  so CHOOSE is a remote side effect rather than a pure read.
- `remote_toggle_rows.ps1` requires `AUTORELOG_REMOTE_SOURCE=db.html`. The
  website must not impersonate that legacy identity.

## Status and decision vocabulary

- `GOLDEN-ONLY`: current db.html capability; no website replacement exists.
- `GOLDEN-INVARIANT`: behavior/access path must remain available during all
  website work, including rollback.
- `UI-PARTIAL`: website UI shows a subset or summary, not golden parity.
- `ACCEPTANCE-REQUIRED`: candidate route/UI exists or is proposed, but browser,
  operator, side-effect and rollback evidence is missing.
- `BACKEND-ONLY`: website backend boundary exists but UI does not expose it.
- `DEFERRED-NO-GO`: blocked by the approved safety boundary.
- `ELIGIBLE-NOW`: can use an approved Phase 2 boundary without claiming P4
  authority migration.
- `P4-AUTHORITY-LATER`: future scheduler/worker/remote authority needs a
  separate P4 design; it does not block a safe read-only proxy slice.

`READ-PARITY-UI` is intentionally absent. It may only be assigned after
browser/operator parity is demonstrated against db.html for the specific slice,
including data shape, loading/error states, public access and rollback.

## DBHTML Parity Matrix

| db.html capability | API/helper and data effect | Website backend | Current website UI | UI migration eligibility now | Future P4 authority dependency | Acceptance and rollback gate | Status |
|---|---|---|---|---|---|---|---|
| Cycle/process dashboard | `GET /api/cycle/status`, `/api/cycle_status`, `/api/status`; reads cycle/process/mutex state | `/up/api/cycle/status`, `/up/api/cycle_status`, `/up/api/status` | Uses only `/up/api/cycle/status`; summary is smaller than golden | Yes, read-only UI slice; no public cutover | P4 needed only to replace scheduler authority | Browser compare cards/fields/errors and preserve legacy public URL; disable only canary | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Auto Sync dashboard | `GET /api/sync_status`; reads PID/running state and metadata | `/up/api/sync_status` | Uses one summary card; not full golden view | Yes, read-only UI slice | P4 lifecycle owner only for start/stop migration | Browser/operator parity; no lifecycle action; legacy tunnel untouched | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Client table | `clients_master.json`, `client_database.json`, db.html remote-live overlay | `/up/clients_master.json`, `/up/client_database.json` | Renders master table only; no remote-live overlay | Yes after explicit overlay contract | P4 materializer/remote authority only if live source migrates | Compare clients/status/Fixed/orphans/overlay; rollback to golden GET | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| AI-fix status card | `GET /api/ai_fix/status`; reads watcher/queue summaries | `/up/api/ai_fix/status` | Uses pending/auto summary only | Yes, read-only status slice | Watcher authority migration separate | Compare queue/error/heartbeat fields and public auth behavior | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Activity History | `activity_history.jsonl`, cycle/change/action logs; browser aggregation | No equivalent current website read contract | Not rendered | No until typed read route exists | P4 event/audit model later | Exact order/dedupe/event comparison; retain db.html public fallback | `GOLDEN-ONLY / ACCEPTANCE-REQUIRED` |
| Remote Live READ | `GET /api/remote_live` -> `remote_live_read.ps1`; remote snapshot under `RemoteWs` | `/up/api/remote_live` exists | Not used | Yes only after read-only remote browser evidence | Remote I/O lease/authority stays golden until migration | Prove no mutation and single owner; canary rollback only | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Remote Live CHOOSE | `GET /api/remote_live?client=...`; remote `row_select` side effect | Guarded selector path exists | Not used | No | P4 remote action owner/lease required | Treat as mutation; verify selection/reconnect | `GOLDEN-ONLY / DEFERRED-NO-GO` |
| Backup list | `GET /api/cycle/backup`; reads metadata/manifests | `/up/api/cycle/backup` exists | Not used | Yes, low-risk read-only slice | P4 backup boundary later | Browser shape/order/error parity; disable canary | `BACKEND-ONLY / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Public settings projection | `GET /api/settings`; redacts token/chat ID and returns public fields | `/up/api/settings` exists | Not used | Yes, low-risk read-only slice | P4 only if settings become operational jobs | Verify redaction/contract; disable canary | `BACKEND-ONLY / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Display-name CAS | `POST /api/master`; narrow display-name-only safe write | Guarded `/up/api/master` exists | No UI | Candidate after separate safe-write gate | Full master/schedule authority remains P4-later | CAS/preimage/browser proof; preserve public legacy path | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Settings safe partial write | `POST /api/settings`; validated public settings write | Guarded `/up/api/settings` exists | No UI | Candidate after explicit rollback proof | Tunnel/Telegram/browser authority remains P4-later | Redaction/validation/preimage/no retry | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Backup create/delete | `POST /api/cycle/backup`, `DELETE .../<name>`; artifact write/delete | Guarded boundaries exist | No UI | Candidate; restore remains blocked | Operational backup/restore authority needs P4 boundary | Manifest/hash/target guard; no legacy tunnel action | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Save Master/Schedule, Sync Remote/All, worker lifecycle, AlwaysRun/remote On-Off, destructive restore/fix | Golden write/control routes; file, process and remote effects | Intentionally deferred or narrowly bounded | No UI replacement | No | P4 job/worker/remote authority, identity and rollback | No dual owner; exact preimage/checkpoint; preserve public db.html | `DEFERRED-NO-GO` |
| Public access / Cloudflare Quick Tunnel | `cloudflared tunnel --url http://127.0.0.1:8080`; `public_url.txt`; `/api/cycle/url`; `/db` auth route | Website proxy is a separate local origin at `127.0.0.1:8090`; no public repoint | Current db.html public banner and legacy URL are live | **No public cutover now**; docs/isolated canary only | P7 Named Tunnel is later hardening, not P4 scope; legacy authority remains | Canary must use separate origin/process/tunnel/hostname. Health-check legacy public `/db` before/during/after. Rollback stops only canary; legacy URL/process/origin remain live | `GOLDEN-INVARIANT / DEFERRED-NO-GO` |
| Test Telegram / Open Browser, AI queue controls, public URL refresh | External/local side effects and watcher/tunnel actions | Guarded or deferred boundaries exist | No UI replacement | No public canary action may invoke legacy tunnel refresh | External side-effect and watcher authority later | Explicit operator action, sanitized response, no retry; public legacy path must survive | `DEFERRED-NO-GO` |

## UI slices eligible for immediate design

These are eligibility statements, not merge or cutover approvals:

1. Correct existing dashboard cards/table to show the same read fields and
   remote-live overlay as db.html, using the approved Phase 2 proxy.
2. Add backup-list and public-settings read views without enabling restore,
   worker control or remote mutation.
3. Evaluate existing display-name CAS as a separately gated safe-write UI
   slice; do not expand it into schedule/master editing.
4. Test all candidates locally or through an isolated canary origin. Do not
   point the current Cloudflare tunnel at the website proxy.

Each candidate still needs contract, security, browser/operator parity,
public-access health, side-effect and rollback evidence before its status can
become `READ-PARITY-UI`.

## Cloudflare migration and rollback gates

The legacy public URL is a golden invariant. No current P4/UI slice may:

- stop, restart, repoint or replace the legacy cloudflared process;
- change `DB_WEB_PORT`, the legacy `127.0.0.1:8080` origin or
  `tools/cache/public_url.txt`;
- call legacy `/api/cycle/url` or cycle-fix solely to expose the website;
- set website remote mutations to `AUTORELOG_REMOTE_SOURCE=db.html`;
- claim public parity from a local `127.0.0.1:8090` check alone.

A future website public canary must have a separate process/origin/tunnel and
separate public endpoint. Its acceptance gate must verify both surfaces:

- website canary authentication and intended UI behavior;
- legacy public `/db` remains reachable and authenticated at the pre-canary URL.

Rollback is asymmetric by design: stop or disable only the website canary and
restore website traffic to its prior safe state. Never stop the legacy
cloudflared process as part of canary rollback. P7 Named Tunnel remains a later
hardening phase and is not pulled into P4.

## Remote identity migration blocker

`remote_toggle_rows.ps1` currently accepts only
`AUTORELOG_REMOTE_SOURCE=db.html`. The website must not set that value to
impersonate the legacy UI. Before website/P4 remote mutation is enabled:

- define and approve a distinct worker identity;
- update the helper contract to accept that identity without removing legacy
  `db.html` for rollback;
- record caller identity, job ID and lease in the audit trail;
- enforce `Local\\AutoGhostStory_RemoteWs` single-owner fencing and verify
  `unknown` outcomes without automatic replay.

Until those gates pass, remote On/Off/AlwaysRun controls remain
`DEFERRED-NO-GO`. This is independent of whether a read-only UI slice is
eligible now.

## Current conclusion

The current website UI is a summary dashboard with four data sources:

- `/up/api/cycle/status`
- `/up/api/sync_status`
- `/up/clients_master.json`
- `/up/api/ai_fix/status`

It does not yet have browser/operator parity with db.html, especially the
remote-live client overlay and the public Cloudflare access path. The website
backend already has approved Phase 2 boundaries, so UI work need not wait for
future P4 authority migration. A safe Phase 2 proxy must not be described as
replacing P4 authority or the legacy public origin.

Keep original `db.html` and its Cloudflare public URL running as golden/fallback
until the specific slice has contract, security, browser/operator, public
access, side-effect and rollback evidence. This document does not authorize
merge of PR #23, remote mutation, scheduler ownership, tunnel changes or
website-only cutover.
