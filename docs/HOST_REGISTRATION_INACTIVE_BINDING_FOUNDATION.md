# Host Registration + Inactive Binding Foundation

This slice permits configuration-only writes to the Portable Domain Store. It
does not create runtime ownership or control any process, scheduler, remote
session, or legacy file.

## Write boundaries

- `POST /up/api/host_registration` registers or confirms the local
  `HOST_AGENT_HOST_ID`. The client may optionally provide only `display_name`.
- `POST /up/api/host_binding` creates or confirms a binding for the configured
  host and a client-provided `profile_id`.

Both routes require `Authorization: Bearer <WEBAPP_WRITE_TOKEN>`, reject query
strings and non-POST methods, and write only through `PortableDomainStore`.

## Server-owned fields

The client cannot provide `host_id`, `origin_ref`, `account_ref`, `state`,
`binding_generation`, or `binding_id`.

- `host_id` comes only from `HOST_AGENT_HOST_ID`.
- New host rows receive the logical origin `host_agent:configured`.
- Bindings are always created as `OFFLINE`.
- Generation is derived from existing rows and increments after retired rows.
- Binding IDs are deterministic for host/profile/generation.

Registration and binding writes use an immediate SQLite transaction. Repeating
the same request confirms the existing configuration without creating a second
row. An existing `ACTIVE` binding is a conflict and is never changed,
retired, or moved.

Responses keep `runtime_authority.mode = LEGACY` and
`active_runtime_owner = null`. Runtime capabilities remain false, and the U1
frontend intentionally exposes no live registration or binding controls.
