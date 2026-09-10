# Host Agent Discovery Foundation

This slice is read-only discovery only. It does not start or stop processes,
write Task Scheduler state, log in or reconnect remotely, switch profiles, or
hand control to P4/scheduler/Host Agent runtime code.

## Three separate layers

`GET /up/api/host_discovery` keeps these layers independent:

1. **Configured Host Identity** comes only from `HOST_AGENT_HOST_ID`. The
   service never falls back to the machine hostname and never creates a host.
2. **Portable Binding** is read from existing `hosts`, `remote_profiles`, and
   `host_profile_bindings` rows in one SQLite read snapshot. Missing host IDs or
   bindings remain unbound; nothing is created.
3. **Local Runtime Observation** probes only the fixed prerequisite allowlist
   under `HOST_DISCOVERY_ROOT`. It does not inspect processes or infer an owner.

The response always reports `runtime_authority.mode = LEGACY` and
`active_runtime_owner = null`. A `state=ACTIVE` binding is not a runtime-owner
claim.

## Prerequisite allowlist

Only these relative paths are probed, read-only:

- `tools/AutoCycle.ps1`
- `continuous_sync_remote.ps1`
- `tools/cache/cycle_state.json`
- `tools/client_database.json`
- `tools/cache/cycle_stopped.flag`

The response reports only `PRESENT`, `ABSENT`, or `UNREADABLE`; it does not
return file contents. All start, stop, login, switch, and bind controls remain
disabled.
