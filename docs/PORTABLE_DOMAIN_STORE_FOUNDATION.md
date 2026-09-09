# Portable Domain Store Foundation

Baseline: `main` at `c79c0ca3e79d7a153387f1dd857dbbf6eb023cc2`.

This slice adds a separate, profile-scoped SQLite store for portable domain
data. It does not replace the P4 Operational SQLite, AutoCycle, `db.html`, or
any ai-tool runtime source of truth.

## Scope

The foundation contains:

- deterministic schema creation and schema metadata at version 1;
- `hosts`, `remote_profiles`, and explicit `host_profile_bindings`;
- profile-owned clients, schedules, policies, control intents, observations and audit events;
- a read-only shadow importer with an explicit `host_id + profile_id + account_ref` binding;
- profile-scoped export and a basic SQLite backup manifest.

Every remote-domain repository read requires `profile_id`. There is no global
`list_clients()` or equivalent default scope. Missing scope fails closed.

## Data boundary

The portable store contains no passwords, session material, cookies, bearer
values, Telegram secrets, process identifiers, mutexes, leases, scheduler
registration, process handles, or tunnel URLs as authority. `host_id` is only a
logical origin/binding reference. Live process state remains host-runtime data.

`cycle_stopped.flag` is imported only when an explicit legacy binding is passed.
It becomes a profile-scoped durable `cycle_stopped` intent and is never written
back to the legacy flag. The intent does not disable read-only Remote Sync.

## Import boundary

`FileSystemGoldenSource` reads only the explicitly named legacy JSON/text files.
It never writes the source root and never derives identity from a session,
process, room, or tunnel value. The importer writes normalized rows under the
provided profile and records only safe observation/audit summaries.

## Acceptance gate

`tests/contract/test_portable_domain_store.py` proves:

- empty database migration, schema metadata and reopen durability;
- Profile A/B isolation, including repeated client/schedule/policy IDs;
- profile-scoped stop intent, observations, audit and export;
- binding account mismatch and active-host conflicts fail closed;
- importer source files remain byte-identical;
- no runtime/secret authority fields are present in the schema;
- backup manifest integrity.

This slice does not add a Profile Manager UI, account switching, P4 jobs or
leases, Host Agent, scheduler control, remote mutation, `db.html` changes,
Cloudflare changes, or Task Scheduler changes.
