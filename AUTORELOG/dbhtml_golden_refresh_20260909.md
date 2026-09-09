# Fresh DB.HTML Golden Refresh — 2026-09-09

## Authority

This audit was collected directly from the current local `ai tool` project, not
from the historical website API inventory. The live local project is the golden
reference for behavior, data shape, side effects and ownership.

The audit was read-only. It did not edit `tools/db.html`, Flask code, scripts,
JSON state, flags, logs, backups, remote state or any credential. No write API,
remote mutation or browser control action was called.

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
- `ai_fix_watcher.json` reports running with `pending=0`; the model/runner
  status was recorded without exposing credentials.
- `autocycle.pid` and `remote_sync.pid` files exist; this is not by itself a
  liveness proof because the authoritative script checks are mutex/process
  based.

## Source fingerprints

Hashes are included to identify the audited local inputs. No secret values are
included.

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

- `db.html` is the operator UI and calls the Flask routes directly with browser
  credentials. Its fetch wrapper adds `X-DB-Editor: 1` to non-GET `/api/*`
  requests.
- `app_public.py` is authenticated with Basic Auth and requires the same-origin
  marker for POST/PUT/PATCH/DELETE. It directly reads/writes the ai-tool files
  and invokes PowerShell helpers for several controls.
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
  remote `row_select`; therefore CHOOSE mode is a remote selection side effect,
  not a pure read.
- `remote_toggle_rows.ps1` is the guarded remote mutation helper. It requires
  `AUTORELOG_REMOTE_SOURCE=db.html`, validates Master identity and Fixed policy,
  holds `RemoteWs`, performs On/Off actions and verifies the result.

## DBHTML Parity Matrix

Status meanings:

- `GOLDEN-ONLY`: current db.html capability; website UI/backend replacement is
  not complete.
- `READ-PARITY-UI`: website UI currently consumes the read contract.
- `BACKEND-ONLY`: website backend route exists, but website UI does not use it
  as a db.html replacement.
- `DEFERRED-NO-GO`: intentionally blocked by the approved safety boundary.
- `P4-DEPENDENT`: requires runtime ownership/lease/queue evidence before safe
  website replacement.

| db.html UI | API/helper | Data read/write | Website backend | Website UI | P4 dependency | Acceptance | Rollback | Status |
|---|---|---|---|---|---|---|---|---|
| Cycle card and process summary | `GET /api/cycle/status`, `GET /api/cycle_status`, `GET /api/status`; process/mutex probes | Read cycle state, process/mutex state and counts | `/up/api/cycle/status`, `/up/api/cycle_status`, `/up/api/status` exist | Current website UI uses `/up/api/cycle/status` | None for read-only display; P4 required for ownership replacement | Status/body/content type parity; no process action | Disable read adapter and use upstream GET | `READ-PARITY-UI` |
| Auto Sync card | `GET /api/sync_status`; `remote_sync_process_pid()` and mutex probe | Read sync PID/running state and master metadata | `/up/api/sync_status` exists | Current website UI uses it | P4 ownership required before replacing sync lifecycle | Live status parity and no spawn/kill | Revert adapter to HTTP read | `READ-PARITY-UI` |
| Fixed/Scheduled client status table | `clients_master.json`, `client_database.json`, remote live overlay | Read master policy plus live DB status | `/up/clients_master.json`, `/up/client_database.json` exist | Current website UI renders master clients; current db.html also overlays live status | P4 materializer/remote ownership for authoritative live replacement | 26-client/current-shape parity; Fixed/orphan invariants | Fall back to ai-tool GETs | `READ-PARITY-UI` |
| Activity History | `cache/activity_history.jsonl`, `cache/cycle.log`, `cache/change_log.jsonl`, `cache/action.log` | Read-only log aggregation, dedupe and sorting in browser | Public static log reads are available in golden app; website parity route is not in current 11-read contract | Not rendered by current website UI | P4 audit/event model later | Event labels/details/order match current db.html | Stop new view and retain db.html | `GOLDEN-ONLY` |
| Remote Live READ mode | `GET /api/remote_live` -> `remote_live_read.ps1` | Remote snapshot read under `RemoteWs`; no row selection in READ mode | `/up/api/remote_live` exists | Not used by current website UI | P4 remote I/O single-owner and lease proof | Snapshot shape, no mutation event, no spawn/kill | Disable route adapter | `BACKEND-ONLY / P4-DEPENDENT` |
| Remote Live CHOOSE mode | `GET /api/remote_live?client=...` -> `row_select` in helper | Reads snapshot but changes selected remote row | Guarded selector path exists in website backend | Not used by current website UI | P4 remote action owner and explicit side-effect contract | Prove selection-only action and rollback/reconnect | Disable CHOOSE; retain READ mode | `GOLDEN-ONLY / P4-DEPENDENT` |
| Save Master + Schedule | `POST /api/master`; backup, mutex, file writes, logs, `sync_master.ps1` | Writes `clients_master.json`, AUTORELOG mirror, logs, then materializes DB/config | Website has display-name-only CAS `/up/api/master`, not full master/schedule parity | No equivalent website UI | P4 enqueue/write owner and durable job evidence | Full contract, preimage/backup, CAS, AutoCycle safety | Restore exact preimage; disable write route | `GOLDEN-ONLY / P4-DEPENDENT` |
| Save Schedule | Same `POST /api/master` payload | Same master/database/config side effects | Only narrow CAS route, no schedule editor | No equivalent website UI | Same as Master write | Schedule overlap/shape/side-effect parity | Restore master/config/database preimage | `GOLDEN-ONLY / P4-DEPENDENT` |
| Upload selected to DB Status | `POST /api/master` via `pushSelected()` | Writes selection/group policy, then syncs derived DB/cycle inputs | Backend does not expose this as a dedicated safe website action | No equivalent website UI | P4 desired-state job and materializer contract | Selected/none/orphan behavior and downstream parity | Restore Master plus derived files | `GOLDEN-ONLY / P4-DEPENDENT` |
| Sync from Remote | `POST /api/sync_remote` -> `sync_remote_once.ps1` then `sync_master.ps1` | Remote read followed by master/DB/config writes and logs | Intentionally deferred; no write dispatch | No equivalent website UI | Remote owner, write job, unknown/retry and rollback | Fresh golden comparison, no dual owner, preimage | Restore only valid delta/preimage; no blind full restore | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Sync TOÀN BỘ | `POST /api/sync_all` -> remote sync + master sync | Remote/master/DB/config/log writes | Intentionally deferred | No equivalent website UI | Same, with multi-step job checkpoint | Step-level evidence and safe partial failure | Resume/rollback from checkpoint | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Auto Sync toggle | `GET /api/sync_status`, `POST /api/sync_continuous/start|stop` | Starts/stops continuous worker and writes activity | Intentionally deferred | No equivalent website UI | Single-owner supervisor/lease and lifecycle proof | No duplicate worker; stop intent durable | Restore owner to continuous sync; clear adapter | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Start Cycle | `POST /api/cycle/start` | Clears stop/override state, starts AutoCycle, remote/worker side effects | Intentionally deferred | No equivalent website UI | Scheduler/remote owner and cutover fence | PID/mutex/remote preimage and no dual mutator | Durable stop/rollback checkpoint | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Stop Cycle + All | `POST /api/cycle/stop` -> process kill + remote Off | Stops AutoCycle, disables Fixed, remote Off, local process actions | Intentionally deferred | No equivalent website UI | High-risk worker/remote ownership and `unknown` handling | Exact process/remote verification | Restore only approved preimage; explicit operator confirmation | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Pause Scheduled / AlwaysRun | `POST /api/control/<target>/<pause|resume>` | Creates/removes pause flags and changes override behavior | Intentionally deferred | No equivalent website UI | Persisted override precedence and scheduler ownership | Flag/override parity plus expiry behavior | Remove adapter, preserve flags | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Stop/Open AlwaysRun | `POST /api/alwaysrun/<stop|open>` -> `remote_toggle_rows.ps1` | Remote On/Off, local qnyh stop, override file and activity log | Intentionally deferred | No equivalent website UI | Remote I/O lease, identity guard and unknown result | Fixed safety, identity validation, remote/local confirmation | Restore override/preimage; no automatic replay | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Clear History | `POST /api/clear_history` | Destructively truncates logs/state and deletes result files | Intentionally deferred | No equivalent website UI | Destructive job, checkpoint and operator confirmation | Exact write-set and concurrent-append safety | Delta-aware restore, never blind overwrite | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Backup list | `GET /api/cycle/backup` -> directory/ZIP manifest read | Read backup metadata and hashes | `/up/api/cycle/backup` exists | Not used by current website UI | None for read; P4 backup boundary later | Metadata order/shape parity | Disable adapter | `BACKEND-ONLY` |
| Create Backup | `POST /api/cycle/backup` | Creates ZIP and manifest from runtime/scripts | `/up/api/cycle/backup` exists and is guarded | No equivalent website UI | P4 operational backup boundary must not include P3 mutation | Artifact manifest/hash and no worker interruption | Delete only created artifact after evidence | `BACKEND-ONLY / P4-DEPENDENT` |
| Restore/Delete Backup | `POST .../restore`, `DELETE .../<name>` | Restores or deletes multiple live files; restore requests worker restart | Delete exists in website backend; restore intentionally deferred | No equivalent website UI | Destructive state/job/lease/rollback proof | Full preimage, concurrent-worker safety, exact target guard | Restore only verified preimage | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Refresh/Fix Cycle | `POST /api/cycle/fix` | Backup, sync, clears flag, restarts workers, tunnel/browser/Telegram | Intentionally deferred | No equivalent website UI | Supervisor ownership, multi-side-effect checkpoint | Disposable full harness and no-secret errors | Checkpointed rollback; no blind rerun | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Settings panel read | `GET /api/settings` | Reads settings with token/chat redacted for browser | `/up/api/settings` exists with public projection | Not used by current website UI | None for read; secret boundary required | Four public fields only, no secret/path leakage | Disable adapter | `BACKEND-ONLY` |
| Settings save | `POST /api/settings` | Writes settings JSON; changes future tunnel/Telegram/browser behavior | `/up/api/settings` safe partial write exists | No equivalent website UI | P4 durable intent only if settings become operational jobs | Field validation, redaction, contract and rollback | Restore settings preimage | `BACKEND-ONLY / P4-DEPENDENT` |
| Test Telegram / Open Browser | `POST /api/settings/test_telegram`, `POST /api/settings/open_browser` | External notification or local browser side effect | Guarded website boundaries exist | No equivalent website UI | P4 supervisor/external side-effect policy | Explicit operator action, sanitized response, no credential leak | No automatic retry; disable adapter | `BACKEND-ONLY / P4-DEPENDENT` |
| AI fix request | `POST /api/ai_fix` | Creates `tools/cache/ai_fix_requests` job file and activity log | `/up/api/ai_fix` exists | No equivalent website UI | P4 job/lease model must not absorb ai-tool watcher without separate gate | Queue schema, session marker, no secret, audit | Delete only pending request under approved policy | `BACKEND-ONLY / P4-DEPENDENT` |
| AI fix queue/status | `GET /api/ai_fix/status` | Reads watcher heartbeat, queue and completed/failed summaries | `/up/api/ai_fix/status` exists | Current website UI uses this read | None for read; watcher ownership later | Status shape and no secret/path leak | Disable adapter | `READ-PARITY-UI` |
| AI answers clear / watcher toggle | `DELETE /api/ai_fix/answers`, `POST /api/ai_fix/watcher` | Deletes completed artifacts or starts/stops watcher | Intentionally deferred | No equivalent website UI | Watcher supervisor/lease and archive proof | Archive/preimage, PID identity, no pending deletion | Restore finished artifacts or restart only owned watcher | `DEFERRED-NO-GO / P4-DEPENDENT` |
| Public URL / Export CSV / Reload | `cache/public_url.txt`, browser Blob, `POST /api/log` on reload | URL read, client-side download, log append on reload | `/up/api/log` exists; URL/static reads are limited | No equivalent website UI | None for local-only export; P4 not needed for read | Export content parity; log contract for reload | Revert UI-only adapter; log is append-only | `GOLDEN-ONLY / BACKEND-ONLY` |

## Current gap conclusion

The current website UI is still a read-only dashboard with four API calls:

- `/up/api/cycle/status`
- `/up/api/sync_status`
- `/up/clients_master.json`
- `/up/api/ai_fix/status`

Its footer explicitly says edits remain on original `tools/db.html`. The
website backend has more safe read/write boundaries than the UI consumes, but an
endpoint is not counted as replacing db.html until the website UI exposes the
same operator capability and its acceptance/rollback proof passes.

The first product-parity slices should therefore be selected from low-risk
read/display capabilities and safe controls only after a fresh contract test.
Remote mutation, process lifecycle, destructive restore, scheduler ownership
and tunnel/browser/Telegram controls remain P4-dependent and NO-GO.

## Required next gates

1. Keep PR #22 unmerged until the Slice 1A lease-binding and actual-schema
   tamper regressions are reviewed.
2. Review this audit and freeze the first DBHTML parity slice separately from
   P4 runtime ownership.
3. For every UI slice, capture current ai-tool behavior first, then implement
   route/service/repository/UI with contract, security, side-effect and
   rollback evidence.
4. Keep original `db.html` running as golden/fallback until full operator
   parity and website-only canary plus rollback pass.
