# Host Registration + Inactive Binding Foundation

This slice permits configuration-only writes to the Portable Domain Store. It
does not create runtime ownership or control any process, scheduler, remote
session, or legacy file.

## Write boundaries

- `POST /up/api/host_registration` registers or confirms the configured host.
- `POST /up/api/host_binding` creates or confirms one OFFLINE profile binding.

The request contracts are exact JSON objects:

- Host registration: `{ "host_id": "...", "display_name": "..." }`.
- Host binding: `{ "host_id": "...", "profile_id": "..." }`.

`host_id` is required in both requests and must match `HOST_AGENT_HOST_ID`
exactly. Leading/trailing whitespace aliases are rejected rather than
canonicalized. The client cannot provide origin/account references, state,
generation, or binding IDs. Malformed, wrong-type, control-field, overlength,
missing-field, and mismatched-identity requests fail before a store write.

Both routes require `Authorization: Bearer <WEBAPP_WRITE_TOKEN>`, reject query
strings and non-POST methods, and write only through `PortableDomainStore`.

## Conflict and transaction semantics

- A new host receives the logical origin `host_agent:configured`.
- An existing host with the same display name is confirmed idempotently.
- An existing host with a different display name returns `409`; it is never
  renamed and the repository invariant rejects the rename for every caller.
- New bindings are always `OFFLINE`.
- Existing OFFLINE bindings are confirmed idempotently.
- An existing ACTIVE binding returns `409` and is never changed, retired, or
  moved.
- Generation is derived from existing rows and increments after retired rows.
- Binding IDs are deterministic for host/profile/generation.

Binding creation, deterministic audit-event creation, and conflict checks run
inside one `BEGIN IMMEDIATE` transaction. The audit event is profile-scoped,
has event type `host_binding_created`, and uses the binding ID in its stable
event ID. Repeated or concurrent requests therefore produce one binding
generation and one audit event, not duplicates. Concurrent registration of an
absent host with different display names produces one host row, one success,
and one `409` conflict without a later rename.

## Response and errors

Success responses include `runtime_effect: "NONE"`,
`runtime_authority.mode: "LEGACY"`, and
`runtime_authority.active_runtime_owner: null`. These writes never establish
runtime ownership or change any worker/scheduler/remote authority.

- `400`: malformed JSON, wrong types, missing/extra/control fields, invalid
  content type, whitespace aliases, or overlength text.
- `401`: missing or invalid Bearer token; no store access occurs.
- `403`: non-POST method; no store access occurs.
- `409`: configured host identity mismatch, host display-name conflict, unknown
  host/profile target, or ACTIVE binding conflict.
- `503`: missing configured host identity or unavailable/invalid store.

## Read-after-write boundary

After registration or OFFLINE binding creation, Host Discovery and Profile
Manager continue to report `runtime_authority.mode = LEGACY` with no active
runtime owner. Their read-only projections may observe the OFFLINE binding, but
no read or write path starts a process, invokes ai-tool, touches legacy files,
uses subprocess/Task Scheduler/remote mutation, or exposes UI controls.
