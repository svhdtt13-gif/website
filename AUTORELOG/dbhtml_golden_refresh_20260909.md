# Fresh DB.HTML Golden Refresh — 2026-09-09

## Authority

This audit was collected directly from the current local `ai tool` project, not
from the historical website API inventory. The live local project is the golden
reference for behavior, data shape, side effects and ownership.

The audit was read-only. It did not edit `tools/db.html`, Flask code, scripts,
JSON state, flags, logs, backups, remote state or any credential. No write API,
remote mutation, browser/tunnel action or worker control action was called.

Sources inspected:

- `tools/db.html`
- `WebAppControl/flask/app_public.py`
- `tools/AutoCycle.ps1`
- `continuous_sync_remote.ps1`
- `tools/sync_master.ps1`
- `remote_live_read.ps1`
- `remote_toggle_rows.ps1`
- current master/database/settings/cycle/override/watcher state
- website `main` frontend and Phase 2 route close-out overlay

## Fresh snapshot

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
- `autocycle.pid` and `remote_sync.pid` files exist; this is not by itself a
  liveness proof because the authoritative script checks are mutex/process
  based.

## Source fingerprints

Hashes identify the audited local inputs. No secret values are included.

| Source | Size | SHA-256 |
|---|---:|---|
| `tools/db.html` | 103983 | `bacfc33d00fb7babe38bd1fa4361f52c52a8fb85ed008f8f08847438f3fb8de4` |
| `WebAppControl/flask/app_public.py` | 75284 | `7a8b9e0e28942ea74ead8a19a292b83aed0810e983448b14e01cf493ef8232b1` |
| `tools/AutoCycle.ps1` | 49584 | `ed4065694849cffe00bd52f5645fc95a4fa02170b994b62000227db316498b13` |
| `continuous_sync_remote.ps1` | 19485 | `532329c3a82e6ba9c465a93db02e0660fd970b49276feb8c8d51393d4ad91bea` |
| `tools/sync_master.ps1` | 6011 | `a46b5fa0a23fe183b3bb1c3983fa5f3557c678f6566f7d095dc7741536730797` |
| `remote_live_read.ps1` | 9416 | `9b69bc841694550e6c5012562ee6e2f5b42b672c5056412f43af0042e09e15fc` |
| `remote_toggle_rows.ps1` | 11500 | `95a06164a42d1e31bfda23ebd9b0a03d9ff770fdb9529eab074b1b6bd44d6774` |
| `tools/clients_master.json` | 9714 | `6b0ad5a5a9064b1e471ec459c070d6141754c04bacf9ba239bd9ac45b381abbe` |
| `tools/client_database.json` | 11925 | `b8d6ca9fe6639c9402ad0067e36b3d6a31131f6b60f5f98efc9e52542136292` |
| `tools/cache/cycle_state.json` | 198 | `2187e99f7a53e2744989f9b0a3146618d8cbe3b1509cd9cd852a75ea5305e6f9` |
| `tools/cache/manual_override.json` | 2173 | `0e414f54f4aa9cd6aa5d798daf605d1981e1d28ce6e105cd57c9a770f6c265d7` |
| `tools/cache/alwaysrun_override.json` | 69 | `a5a00b5bce762f2edecf4f3a27aeda824ad75d9b58705ae5523efc75bd158cc3` |
| `tools/cache/settings.json` | 290 | `1b79eb04f18bb3ea5034574f85da6cc493ec5df3d40c78bf31629b0e70fde134` |

Settings keys observed: `telegram_bot_token`, `telegram_chat_id`,
`default_browser`, `cloudflared_path`, `tunnel_port`,
`auto_restart_tunnel`, `auto_telegram`, `auto_open_browser`. Secret values
were not read into this evidence.

## Current authority and side effects

- `db.html` is the operator UI and calls Flask routes directly with browser
  credentials. Its fetch wrapper adds `X-DB-Editor: 1` to non-GET `/api/*`
  requests.
- `app_public.py` is authenticated with Basic Auth and requires the same-origin
  marker for POST/PUT/PATCH/DELETE. It directly reads/writes ai-tool files and
  invokes PowerShell helpers for several controls.
- `AutoCycle.ps1` owns schedule execution, uses
  `Local\\AutoGhostStory_AutoCycle`, `Local\\AutoGhostStory_DbWrite` and
  `Local\\AutoGhostStory_RemoteWs`, protects Fixed clients, performs remote
  WebSocket actions and writes database/state/log files.
- `continuous_sync_remote.ps1` owns live remote observation, uses
  `Local\\AutoGhostStory_RemoteSync` plus `RemoteWs`, writes live status/master
  projections and persisted manual overrides, and may restart AutoCycle only
  after a structural change and downstream sync.
- `sync_master.ps1` materializes master into `client_database.json`,
  `config.json`, `clients_master.csv` and cleans stale AlwaysRun overrides.
- `remote_live_read.ps1` is read-mostly but a selected-client request uses
  remote `row_select`; CHOOSE mode is therefore a remote selection side effect,
  not a pure read.
- `remote_toggle_rows.ps1` is the guarded remote mutation helper. It requires
  `AUTORELOG_REMOTE_SOURCE=db.html`, validates Master identity and Fixed policy,
  holds `RemoteWs`, performs On/Off actions and verifies the result.

## Status and decision vocabulary

- `GOLDEN-ONLY`: current db.html capability; no website replacement exists.
- `UI-PARTIAL`: website UI shows a subset or summary, but not golden operator
  behavior or data parity. This is not a parity approval.
- `ACCEPTANCE-REQUIRED`: candidate route/UI may exist, but browser/operator
  contract, side-effect and rollback evidence is still missing.
- `BACKEND-ONLY`: a website boundary exists but the UI does not expose it.
- `DEFERRED-NO-GO`: intentionally blocked by the approved safety boundary.
- `ELIGIBLE-NOW`: can be implemented using an already-approved Phase 2
  read/safe-write boundary, without claiming P4 authority migration.
- `P4-AUTHORITY-LATER`: future scheduler/worker/remote authority still needs
  a separate P4 design and gate. It does not by itself block a read-only UI
  slice that remains a proxy to the golden source.

`READ-PARITY-UI` is deliberately absent from the current matrix. It may only be
assigned after browser/operator parity is demonstrated against db.html for the
specific slice, including data shape, empty/error/loading states and relevant
side effects.

## DBHTML Parity Matrix

| db.html capability | API/helper and data effect | Website backend | Current website UI | UI migration eligibility now | Future P4 authority dependency | Acceptance and rollback gate | Status |
|---|---|---|---|---|---|---|---|
| Cycle/process dashboard | `GET /api/cycle/status`, `/api/cycle_status`, `/api/status`; reads cycle, process and mutex state | `/up/api/cycle/status`, `/up/api/cycle_status`, `/up/api/status` | Uses only `/up/api/cycle/status`; summary is smaller than golden | Yes, read-only contract/UI slice | P4 needed only to replace scheduler authority, not to proxy status | Browser compare all cards, loading/error states and process fields; disable adapter to rollback | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Auto Sync dashboard | `GET /api/sync_status`; reads PID/running state and metadata | `/up/api/sync_status` | Uses endpoint for one summary card; not full golden view | Yes, read-only contract/UI slice | P4 lifecycle owner required before start/stop migration | Browser/operator parity and no lifecycle action; revert to golden read | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Client table | `clients_master.json`, `client_database.json`, plus db.html remote-live overlay | `/up/clients_master.json`, `/up/client_database.json` | Renders master table only; does not render remote-live overlay | Yes for a read-only table slice after adding explicit overlay contract | P4 materializer/remote authority only if live source is migrated | Compare 26 clients, statuses, Fixed/orphan states and overlay; fallback to golden GET | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| AI-fix status card | `GET /api/ai_fix/status`; reads watcher/queue summaries | `/up/api/ai_fix/status` | Uses endpoint for a pending/auto summary only | Yes, read-only status slice | Watcher authority migration is separate | Compare queue/error/heartbeat fields and no secret leakage; disable adapter | `UI-PARTIAL / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Activity History | `activity_history.jsonl`, `cycle.log`, `change_log.jsonl`, `action.log`; read-only aggregation | No equivalent current website read contract | Not rendered | No until a typed read route and contract exist | P4 event/audit model may replace source later | Exact order/dedupe/event label comparison; keep db.html as rollback | `GOLDEN-ONLY / ACCEPTANCE-REQUIRED` |
| Remote Live READ | `GET /api/remote_live` -> `remote_live_read.ps1`; remote snapshot under `RemoteWs` | `/up/api/remote_live` exists | Not used | Yes only as a read-only UI slice after remote-read browser evidence | Remote I/O lease/authority must remain with golden worker until migration | Prove no mutation event and single-owner lock; remove new UI on failure | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Remote Live CHOOSE | `GET /api/remote_live?client=...`; remote `row_select` side effect | Guarded selector path exists | Not used | No, until side effect is explicitly accepted | P4 remote action owner and lease required | Treat as mutation; verify selection/reconnect and rollback | `GOLDEN-ONLY / DEFERRED-NO-GO` |
| Backup list | `GET /api/cycle/backup`; reads backup metadata/manifests | `/up/api/cycle/backup` exists | Not used | Yes, low-risk read-only slice | P4 backup boundary later; no authority migration for listing | Browser shape/order/error parity; disable adapter | `BACKEND-ONLY / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Public settings projection | `GET /api/settings`; redacts token/chat ID and returns public fields | `/up/api/settings` exists | Not used | Yes, low-risk read-only slice | P4 only if settings become durable operational jobs | Verify redaction and field contract; disable adapter | `BACKEND-ONLY / ELIGIBLE-NOW / P4-AUTHORITY-LATER` |
| Display-name CAS | `POST /api/master`; narrow display-name-only write with CAS | Guarded `/up/api/master` exists | No UI | Yes, only as a separately scoped safe-write UI slice | Full master/schedule authority remains P4-later | Preimage/CAS/contract/browser proof; restore exact name/preimage | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Settings safe partial write | `POST /api/settings`; writes validated public settings | Guarded `/up/api/settings` exists | No UI | Yes, only after operator confirmation and rollback proof | External tunnel/Telegram/browser authority remains P4-later | Redaction, validation, preimage and no automatic retry | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Backup create/delete | `POST /api/cycle/backup`, `DELETE .../<name>`; artifact write/delete | Guarded boundaries exist | No UI | Candidate only; create first, delete remains higher risk | Operational backup/restore authority needs P4 boundary | Manifest/hash, target guard, artifact rollback; no restore claim | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| AI-fix request | `POST /api/ai_fix`; creates typed queue file and activity record | Guarded `/up/api/ai_fix` exists | No UI | Candidate safe-write slice after queue contract review | Watcher/job authority remains separate | Queue schema, session marker, no secret, cancellation policy | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| Save Master/Schedule and upload selected | `POST /api/master`, backup, `sync_master.ps1`; writes master/DB/config/logs | Only narrow display-name CAS; no full route | No UI | No | P4 desired-state/materializer authority required | Full preimage, CAS, derived-file and AutoCycle safety proof | `GOLDEN-ONLY / DEFERRED-NO-GO` |
| Sync Remote / Sync All | `POST /api/sync_remote`, `POST /api/sync_all`; remote read plus master/DB/config writes | Intentionally deferred | No UI | No | Remote owner, multi-step job, unknown/retry and rollback | No dual owner, step checkpoint and preimage | `DEFERRED-NO-GO / P4-AUTHORITY-LATER` |
| Auto Sync start/stop and Cycle start/stop | Worker/process lifecycle and remote actions | Intentionally deferred | No UI | No | Single supervisor, PID/mutex, durable stop intent and remote lease | Disposable lifecycle harness, no dual mutator, checkpoint rollback | `DEFERRED-NO-GO` |
| Pause/AlwaysRun/remote On-Off | Flags/overrides plus `remote_toggle_rows.ps1`; remote mutation and activity | Intentionally deferred | No UI | No | Approved remote worker identity and `RemoteWs` lease | Fixed safety, identity, remote/local verification and explicit operator action | `DEFERRED-NO-GO` |
| Clear history / restore / cycle fix | Destructive file/process/remote/tunnel actions | Intentionally deferred | No UI | No | Destructive job, checkpoint and supervisor ownership | Exact write-set, concurrent safety and disposable rollback | `DEFERRED-NO-GO` |
| Test Telegram / Open Browser | External notification or local browser side effects | Guarded boundaries exist | No UI | Candidate only with explicit operator action | External side-effect supervisor remains P4-later | Sanitized response, no credential leak, no retry | `BACKEND-ONLY / ACCEPTANCE-REQUIRED / P4-AUTHORITY-LATER` |
| AI answer deletion / watcher toggle | Deletes artifacts or controls watcher | Intentionally deferred | No UI | No | Watcher identity, archive/preimage and lifecycle ownership | Archive, PID identity and pending-queue protection | `DEFERRED-NO-GO` |
| Public URL / CSV export / reload log | URL read, client-side Blob export, `POST /api/log` append | `/up/api/log` exists; URL/export UI not ported | No UI | URL/export read slice can be considered; reload log needs contract | No P4 authority for local export; audit ownership later | Export content and append semantics; revert UI-only adapter | `GOLDEN-ONLY / ACCEPTANCE-REQUIRED` |

## UI slices eligible for immediate design

These are eligibility statements, not merge or cutover approvals:

1. Correct the existing dashboard cards/table to show the same read fields and
   remote-live overlay as db.html, using only the existing Phase 2 read proxy.
2. Add backup-list and public-settings read views without enabling restore,
   worker control or remote mutation.
3. Evaluate the existing display-name CAS as a separately gated safe-write UI
   slice; do not expand it into schedule/master editing.
4. Keep all browser/operator actions behind explicit acceptance tests and retain
   db.html as the golden fallback.

Each candidate still needs a contract test, security test, browser test,
operator parity evidence and rollback note before its status can become
`READ-PARITY-UI`.

## Remote identity migration blocker

`remote_toggle_rows.ps1` currently accepts only
`AUTORELOG_REMOTE_SOURCE=db.html`. The website must not set that value to
impersonate the legacy UI. Before any website/P4 remote mutation is enabled:

- define and approve a distinct worker identity, for example a reviewed
  `website`/P4 worker source;
- update the helper contract to accept that identity without removing the
  legacy `db.html` identity;
- retain the legacy path unchanged for rollback;
- record caller identity, job ID and lease in the audit trail;
- enforce the same `Local\\AutoGhostStory_RemoteWs` single-owner fence and
  verify `unknown` outcomes without automatic replay.

Until those gates pass, all remote On/Off/AlwaysRun controls remain
`DEFERRED-NO-GO`. This blocker is independent of whether a read-only dashboard
slice is eligible now.

## Current conclusion

The current website UI is a summary dashboard with four data sources:

- `/up/api/cycle/status`
- `/up/api/sync_status`
- `/up/clients_master.json`
- `/up/api/ai_fix/status`

It does not yet have browser/operator parity with db.html: in particular, the
client table lacks the golden remote-live overlay. The website backend already
has several approved Phase 2 boundaries, so UI work should not be delayed just
because future P4 authority migration is unfinished. Conversely, a safe Phase 2
proxy must not be described as replacing P4 authority.

Keep original `db.html` running as golden/fallback until the specific slice has
contract, security, browser/operator, side-effect and rollback evidence. This
document does not authorize merge, remote mutation, scheduler ownership or
website-only cutover.
