# LCR2 Runtime Reality Baseline

Status: `RUNTIME_REALITY_PASS_WITH_EXPECTED_LCR2_GAPS`

Decision date: 2026-09-20

This is a read-only Phase 0 report. No runtime process, endpoint, WebSocket
mutation, database mutation, or remote action was performed while collecting
this evidence.

## Baseline

- Repository: `C:\Users\ADMIN\Documents\ai tool\website-checkout`
- Branch: `p4/lcr1-legacy-canary-contract`
- HEAD: `3018b237a870ea35b686d2fa53055ac9e942bcc6`
- Worktree was clean before this report was added.
- LCR1 contract and acceptance matrix remain unchanged.
- `p4/lcr2a-foundation` was not yet created when this baseline was captured;
  it is created only after this gate is recorded as pass.

## Authoritative Runtime Paths

The checked-out repository is not the direct LEGACY runtime source. The
authoritative runtime files are under the parent `ai tool` directory:

- Reader/materializer: `C:\Users\ADMIN\Documents\ai tool\continuous_sync_remote.ps1`
- Scheduler/mutator: `C:\Users\ADMIN\Documents\ai tool\tools\AutoCycle.ps1`
- Master policy: `C:\Users\ADMIN\Documents\ai tool\tools\clients_master.json`
- Materialized live database: `C:\Users\ADMIN\Documents\ai tool\tools\client_database.json`
- Browser UI: `C:\Users\ADMIN\Documents\ai tool\tools\db.html`
- Flask server: `C:\Users\ADMIN\Documents\ai tool\WebAppControl\flask\app_public.py`
- Master mirror: `C:\Users\ADMIN\Documents\ai tool\AUTORELOG\clients_master.json`
- Database mirror: `C:\Users\ADMIN\Documents\ai tool\AUTORELOG\client_database.json`
- Scheduler ledger: `C:\Users\ADMIN\Documents\ai tool\tools\cache\cycle_state.json`
- Runtime log: `C:\Users\ADMIN\Documents\ai tool\tools\cache\cycle.log`

`tools/cache/db.html` exists but is not the active Flask-served page. The
active page is `tools/db.html`.

## Deterministic Data Flow Observed

1. `continuous_sync_remote.ps1:118-133` reads the remote session/room and
   sends `scr_list` through the sole LEGACY WebSocket reader.
2. `continuous_sync_remote.ps1:134-160` accepts `scr_list_res` and reduces
   every instance to only `id` (with leading `0:` removed), `rawName`, and
   `state`. The response `idx` is not retained.
3. `continuous_sync_remote.ps1:173-188` matches existing
   `client_database.json` rows by `client` ID and updates only `status`.
4. `continuous_sync_remote.ps1:204-268` rebuilds policy identity using unique
   normalized `remote_name`, configured aliases, clean remote names, and old
   IDs. It preserves `name`, `group`, and `selected`, while live `status`
   comes from the remote response.
5. `continuous_sync_remote.ps1:271-300` writes the generated master to both
   `tools/clients_master.json` and `AUTORELOG/clients_master.json`.
6. Status changes write both `tools/client_database.json` and
   `AUTORELOG/client_database.json` at `continuous_sync_remote.ps1:185-188`.
7. `db.html:875-934` loads the master and database files. The status table
   correlates rows by exact `client` ID. `db.html:448-471` treats
   `group == "fixed"` and `selected != false` as Fixed policy, while live
   running/offline state remains remote status.

## Deterministic Orphan Semantics

The UI does not render an explicit `orphan` label. The main status table is
master-driven: it renders only selected rows whose group is `fixed` or appears
in the schedule (`db.html:494-524`). Status is looked up by exact `client` ID;
missing database entries become `unknown` (`db.html:917-927`), while the
remote-live view is a separate read-only display (`db.html:366-434`).

For target safety, the following derived classification is deterministic and
fail-closed. `offline` is reserved for an observed remote state; absence,
staleness, ambiguity, and conflicts are never converted to `offline`.

| Case | UI label/state | orphan? | eligible target? | observed_state |
|---|---|---|---|---|
| Unique record exists in master, remote snapshot, and materialized DB with the same non-empty client ID | Fixed or scheduled row; live status from remote-live, then DB fallback | No | Yes only for selected, scheduled, non-fixed, unique identity; fixed is not an LCR1 target | Remote `state`; otherwise DB status only as context |
| Master record exists but remote snapshot is missing it | Master row may still render as Fixed/scheduled; DB value may remain stale; no absence label | Yes | No | `unknown` for target purposes, never inferred `offline` |
| Master record exists and remote has it, but materialized DB lacks it | Master row renders; DB status is `unknown`; remote-live may show live state | No, if identity is unique and exact | No until materialized identity/state catches up | Remote `state`, but evidence is incomplete |
| Remote/materialized record exists but master lacks it | No main master row; remote-live may show `name (id)` | Yes, runtime-only/foreign record | No | Remote `state` if present; otherwise `unknown` |
| Valid Fixed record | `Fixed` row and Fixed summary; policy is always-on | No | No for LCR1 `group_on`, which requires one non-fixed target | Remote `state`; missing proof remains `unknown` |
| Same unique normalized `remote_name`, new remote ID | New ID is emitted; old name/group/selected fields are preserved by name match (`continuous_sync_remote.ps1:247-268`) | Old ID: yes/stale; new ID: no after both stores converge | No during transition; yes only after one unique converged identity exists | New remote `state` |
| Same remote ID, changed remote name | Existing ID fallback preserves policy; displayed name/remote_name are updated | No if ID is unique and non-empty | Same selected/group rule as normal record | New remote `state` |
| Stale DB row or empty remote snapshot | Sync returns before reconciliation when remote list is empty (`continuous_sync_remote.ps1:154-162`); unmatched DB rows are not updated (`:173-188`) | Yes for target safety | No | `unknown`; prior `running`/`offline` is stale context only |
| Missing/blank identity or blank remote ID/name | Current sync can fall through to blank/ambiguous values (`continuous_sync_remote.ps1:214-217`, `:247-268`) | Yes | No | `unknown` |
| Duplicate remote ID, duplicate normalized name, or master ID conflict | Remote IDs are not deduplicated; ambiguous names are not indexed; later ID merge can overwrite earlier fields (`continuous_sync_remote.ps1:220-238`, `:282-289`) | Yes/conflict | No | `conflict`/`unknown`, never a positive state |

Existing validation policy independently confirms the fail-closed orphan
meaning: `Validate-Master.ps1:53-56` requires `client_68` and `Ghost Story
PC*` candidates to use `group=none`, otherwise they are warned as orphan
policy violations. Current data still contains `client_68` in `MIE` and
`Ghost Story PC 92-96` in `gr1`; the UI renders those as scheduled rows, but
the target gate must classify them as orphan/ineligible until corrected. The
historical sample also records `client_68` as orphan and `client_99` as
new/orphan/unknown (`AUTORELOG/discovery_report.txt:37-41`).

This closes the only Phase 0 blocker: absence, stale state, missing identity,
and conflict now have an explicit target-safety result even though the legacy
UI itself has no orphan badge.

## Expected LCR2 Gaps

The following are expected deliverables, not Runtime Reality blockers:

- LCR2-A will add the separate LEGACY-owned durable handoff, authorization
  artifact, fence, receipt, and terminal-CAS store. It must not use
  `p4_operational.sqlite3`, P3 generation SQLite, or `C:\Users\ADMIN\Documents\ai tool\D:`.
- LCR2-D will add `snapshot_id`, monotonic `observation_generation`,
  `observation_boundary_id`, `canary_run_id`, `fence_identity`, non-secret
  `source_identity_ref`, and target-level post-dispatch evidence.
- No AutoCycle, continuous-sync, db.html, remote protocol, or live mutation
  change is included in this gate transition.

## Gate Decision

`Runtime Reality = PASS_WITH_EXPECTED_LCR2_GAPS`

`LCR2-A = authorized to start`

`LCR2-D = expected gap`

`IS3C = STOP`
