# Data Ownership, Portability and Multi-Remote Design Pack

Status: DESIGN ONLY. This document authorizes no production migration, remote-account change, scheduler handoff, Cloudflare restart, `db.html` change, or live remote control.

Evidence date: 2026-09-09. Source audit: local `ai tool` artifacts, `AUTORELOG/p4_baseline.md`, `AUTORELOG/p4_design_freeze_addendum.md`, and the website Phase 2 contracts.

## 1. Target Topology

The webapp is a central control plane. A Windows host remains the owner of its local runtime and one active remote profile:

```text
Webapp control plane
  -> Host
    -> one active Remote Profile
      -> one Remote Account and session
        -> one Room / Session
          -> Clients
            -> Policy / Schedule
              -> Durable Control Intent
                -> Local Runtime / Worker
                  -> Observations and Audit
```

The supported fleet shape is:

```text
PC-A -> Remote Profile A -> Account A -> Cycle A
PC-B -> Remote Profile B -> Account B -> Cycle B
PC-C -> Remote Profile C -> Account C -> Cycle C
```

A host cannot activate two remote profiles or two cycle/sync runtimes at once. A profile cannot have two hosts with mutate authority. A standby host is read-only until a reviewed handoff or failover checkpoint passes.

Every future command must bind all of the following before dispatch:

- `host_id`
- `profile_id`
- verified remote identity
- current binding generation / lease
- command idempotency key

A mismatch fails closed. A viewed profile is not automatically the active profile, and a viewed profile that differs from the active remote owner has no control authority.

## 2. Current Source Audit Matrix

The current files are the golden source and are not migrated by this pack. `Portable` means the domain meaning can be exported after redaction and validation, not that the current file can be copied into another machine and run.

| Current artifact | Current owner / writer | Scope | Classification | Portability decision | Future destination |
|---|---|---|---|---|---|
| `tools/clients_master.json` | db.html, sync, AutoCycle materialization | Remote profile | Desired clients, names, groups, selected flags and schedule | Portable as validated profile domain data; no live credentials or host paths | `profile_clients`, `profile_schedules`, `profile_policies` |
| `tools/client_database.json` | remote sync and `sync_master.ps1` | Remote profile observation | Materialized client state, schedule projection, `lastUpdated` | Export as observation fixture only; never the portable source of truth | `profile_observations` and read projection |
| `tools/config.json` | db.html / sync | Profile policy plus current runtime mapping | Emulator aliases, schedule and path/config values | Split by field; aliases and policy may be portable, paths and secrets are not | Profile policy tables plus host runtime config |
| `tools/remote_rooms.json` | remote activation/sync | Host and remote profile | Product, machine identifier, room identifier and connectivity | Machine-bound binding evidence; never copy as an activation credential | `host_profile_bindings` observations |
| `tools/remote_session.json` | LoginRemote / runtime | Remote account/profile and current host | Bearer session token | Secret, non-portable, never included in export/backup; re-login required | External secret reference only |
| `tools/cache/cycle_state.json` | AutoCycle | Remote profile | Daily slot completion ledger | Not portable as live state; may be imported only as a reviewed checkpoint with profile identity and schedule hash | `profile_control_checkpoints` |
| `tools/cache/cycle_stopped.flag` | Flask/UI | Currently global path, intended cycle stop | Durable stop intent | Must become profile-scoped intent; never infer global account state | `profile_control_intents` with `kind=cycle_stopped` |
| `tools/cache/cycle_paused.flag` | Flask/UI | Current profile runtime | Scheduler reconciliation pause | Profile-scoped intent; no direct production migration in this pack | `profile_control_intents` |
| `tools/cache/alwaysrun_paused.flag` | Flask/UI | Current profile runtime | Fixed-client repair pause | Profile-scoped intent with fixed-client safety precedence | `profile_control_intents` |
| `tools/cache/alwaysrun_override.json` | Flask, AutoCycle, sync | Current profile | Temporary fixed-client disable set | Portable only as explicit policy/intent after identity validation; never as host state | `profile_policies` plus intents |
| `tools/cache/manual_override.json` | remote sync | Current profile | Short-lived manual observation banner | Keep as evidence input during migration; durable model belongs to P4 operational store | `profile_observations`, `manual_overrides` |
| `tools/cache/workers_restart.flag` | Flask / maintenance | Host runtime | Restart request | Host-bound ephemeral signal; never export | `host_runtime_state` / supervisor command |
| `tools/cache/autocycle.pid` | AutoCycle | Host process | PID hint protected by cycle mutex | Non-portable and disposable; never restore | `host_runtime_state` observation |
| `tools/cache/remote_sync.pid` | remote sync | Host process | PID hint protected by sync mutex | Non-portable and disposable; never restore | `host_runtime_state` observation |
| Named mutexes | AutoCycle, sync, Flask and remote actions | Host kernel | Singleton and write/remote serialization | Non-portable; replace with host leases while preserving singleton semantics | `leases` plus supervisor ownership |
| `tools/cache/activity_history.jsonl` | Flask, AutoCycle, sync, watchdog | Host/profile events | Operational history | Redacted append-only export allowed with host/profile attribution | `profile_audit_events`, `host_runtime_events` |
| `tools/cache/change_log.jsonl` | Multiple paths | Host/profile audit | Compatibility/change audit | Redacted export allowed; preserve source and event identity | `profile_audit_events` |
| `tools/cache/ai_fix_requests/*` | AI-fix watcher / agent | Project and host | Durable request/result files | Portable only as sanitized work item text; no runtime credentials or paths | Separate AI job domain, not remote control state |
| `tools/cache/settings.json` | Flask/db.html | Host settings plus secrets | Telegram credentials, browser, tunnel settings | Never portable as-is; public projection only | Secret references plus host settings |
| `tools/cloudflared.exe` | start_onboot / public web | Host installation | Binary and local executable path | Host-bound installation; provision separately | Host agent installation manifest |
| `tools/cache/public_url.txt` | Cloudflare watchdog | Current tunnel process | Ephemeral Quick Tunnel URL | Non-portable observation; never identity or profile authority | `host_runtime_state` observation |
| Task Scheduler `AutoGhostStory_OnBoot` | Windows Task Scheduler | Host | Logon startup for Flask, tunnel, sync, cycle and watchdog | Host-bound; recreate from an installer, never import as domain state | Host agent installer/runtime |
| P4 `runtime/operational/p4_operational.sqlite3` | Future P4 operational owner | Host control plane, with profile targets | Mutable jobs, leases, checkpoints, overrides and events | Do not merge with P3; backup/restore requires host/profile validation and no live lease copy | P4 operational store |
| P3 `runtime/sqlite/<generation>.sqlite3` | Existing runtime reader | Immutable generation | Read-only imported generation | Immutable and read-only; never an operational target or portable live queue | Existing P3 generation store |

The current machine has a real remote session token and Telegram settings. This matrix intentionally records only paths and classifications, never secret values.

## 3. Proposed Domain Store Schema

This is a design target, not a migration script. The Portable Domain Store is separate from P4 Operational SQLite. It contains profile domain data and safe references. P4 remains the mutable job/lease/checkpoint store described by the existing design freeze.

### `hosts`

- `host_id` primary key, stable generated identity, display name
- `host_fingerprint`, `platform`, `agent_version`, `timezone`
- `status` in `ACTIVE`, `STANDBY`, `OFFLINE`, `QUARANTINED`
- `last_seen_at`, `created_at`, `updated_at`
- unique host fingerprint; no credential material

### `remote_profiles`

- `profile_id` primary key, stable logical identity
- `display_name`, `provider`, `product_id`
- `remote_account_ref` non-secret external reference
- `verified_identity_ref`, `identity_verified_at`, `identity_verification_method`
- `state` in `ACTIVE`, `VERIFIED`, `OFFLINE`, `NEEDS_LOGIN`
- `cycle_stopped_intent_id`, `created_at`, `updated_at`
- unique provider/account reference where known

### `host_profile_bindings`

- `binding_id`, `host_id`, `profile_id`, `binding_generation`
- `mode` in `ACTIVE`, `STANDBY`, `READ_ONLY`, `HANDOFF_PENDING`, `REVOKED`
- `mutate_authority`, `verified_identity_ref`, `lease_owner_id`
- `bound_at`, `released_at`, `handoff_checkpoint_id`
- unique active binding per host; unique mutate-authority binding per profile

### `profile_clients`

- `client_id`, `profile_id`, stable remote client identity
- `remote_name`, display name, group, selected, policy classification
- fixed/orphan/unknown safety classification
- source revision and observed identity reference
- unique `(profile_id, client_id)` and no client row without profile ownership

### `profile_schedules`

- `schedule_id`, `profile_id`, group/window/open/close data
- timezone, enabled, source revision, effective range
- schedule hash used for checkpoint comparison

### `profile_policies`

- `policy_id`, `profile_id`, policy kind and JSON value
- precedence, effective time, actor/source, revision
- fixed-client and orphan protections are non-bypassable invariants

### `profile_control_intents`

- `intent_id`, `profile_id`, target kind and target ID
- `kind`, desired state, source, actor/request ID, precedence
- `cycle_stopped` is a first-class profile intent, not a global flag
- `created_at`, `expires_at`, `resolved_at`, `resolution`, evidence reference
- durable idempotency key and revision

### `profile_observations`

- `observation_id`, `profile_id`, `host_id`, `binding_generation`
- observed remote identity, client state, room connectivity and source timestamp
- source hash, received_at, freshness, raw payload reference
- observations are never treated as permission to mutate

### `profile_audit_events`

- `event_id`, `profile_id`, optional `host_id` and `binding_id`
- event type, actor/source, request ID, created time
- redacted evidence JSON, previous/current revision
- append-only; secrets and session payloads excluded

### `host_runtime_state`

- `host_id`, runtime role, worker health, PID hints, mutex/lease observations
- local paths, process version, startup task health, tunnel health
- `cycle_stopped` is not stored here; this table can only observe host runtime
- all PID, mutex, task and executable fields are disposable host facts

### P4 relationship

P4 tables `jobs`, `leases`, `checkpoints`, `manual_overrides` and `worker_events` remain in the separate operational DB. Every target JSON in those rows must include `host_id`, `profile_id`, binding generation and verified identity reference. No P4 job may target a profile whose binding is not active and verified.

## 4. Profile and Binding State Rules

Profile state is observational and fail-closed:

```text
NEEDS_LOGIN -> VERIFIED -> ACTIVE
VERIFIED -> OFFLINE -> VERIFIED
ACTIVE -> OFFLINE
ACTIVE -> NEEDS_LOGIN
```

- `VERIFIED` means identity proof passed, but the profile may not currently own a host runtime.
- `ACTIVE` means exactly one host binding has mutation authority and valid lease.
- `OFFLINE` means reads are stale or host/runtime is unavailable; no mutation is allowed.
- `NEEDS_LOGIN` means the session is absent, expired or identity proof failed; no mutation is allowed.
- A viewed profile is always safe to inspect, but control is enabled only when viewed profile, active binding, host lease and verified identity all match.
- A standby PC never copies PID, mutex, lease, session, room ownership or stop flags from another PC.
- Handoff requires quiescing new dispatch, recording active group/live snapshot/checkpoint/pending jobs/leases, revoking old mutate authority, verifying the new host identity, then acquiring a new binding lease.

## 5. Portable Package Contract

A future export is a versioned package containing:

- schema version and package ID
- profile metadata without secrets
- clients, schedules, policies and durable profile intents
- redacted observations and audit events
- source hashes and validation reports
- explicit origin `host_id` and `profile_id` references

A package must exclude:

- remote session tokens, cookies, passwords, Telegram tokens and Cloudflare credentials
- PID files, named mutex state, Task Scheduler registrations and process handles
- live lease ownership, worker checkpoints that have not been reconciled, and ambiguous remote jobs
- executable absolute paths unless included as non-authoritative installation hints
- random Quick Tunnel URLs as profile identity

Import into a new PC creates an unbound `VERIFIED`-pending profile representation. It does not activate a remote account, restore a session, start a cycle, or take a lease.

## 6. Cycle Stop Semantics

The current `cycle_stopped.flag` is a global file path and therefore cannot be the future ownership model. The target semantics are:

- stop is a durable `profile_control_intent` with `kind=cycle_stopped`
- the intent follows the profile across host handoff
- a newly bound host must observe the intent before starting Cycle or any runtime with mutate/control authority
- Remote Sync read-only observation and reconciliation remain allowed while the profile is stopped, including after reboot or handoff
- if Sync must be stopped in the future, it requires a separate profile intent such as `sync_stopped`; `cycle_stopped` must not be reused for that purpose
- clearing stop requires authenticated intent for the same profile and binding authority
- host restart, PID loss, mutex loss or profile switch never clears the intent
- copying a flag file between PCs is forbidden

## 7. Required Implementation Order

1. Review this design pack and resolve ownership/schema questions.
2. Build a Portable Domain Store foundation separate from P4 Operational SQLite.
3. Add shadow/read-only import from the current ai tool source.
4. Run synthetic Profile A/Profile B tests proving no cross-profile leakage.
5. Build Remote Profile Manager with the four profile states and fail-closed switch state machine.
6. Add host binding and Local Agent observation/lease boundaries.
7. Only then continue P4 Slice 1B and later with `host_id` and `profile_id` on jobs, leases, checkpoints and overrides.

U1 may continue in parallel, but it must display host/profile context and must not treat the current single-host source as a global account. No step above authorizes live remote mutation or production migration.

## 8. Review Checklist

- [ ] Every current JSON, flag, log, session, PID, mutex, Task Scheduler and Cloudflare artifact has an owner and scope.
- [ ] Portable domain data is separated from host runtime facts and secrets.
- [ ] Profile A/B synthetic tests prove reads, intents, schedules, observations and audit rows do not cross profiles.
- [ ] Host/profile binding uniqueness and fail-closed command checks are specified before code.
- [ ] `cycle_stopped` remains profile-scoped across handoff and does not disable read-only Remote Sync.
- [ ] A future `sync_stopped` intent is separate from `cycle_stopped`.
- [ ] P4 operational DB remains separate from P3 immutable generations.
- [ ] No migration, account switch, scheduler handoff, `db.html` change or Cloudflare legacy restart is included in this pack.
