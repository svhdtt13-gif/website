# Remote Profile Manager Foundation

This slice is the read-only/state-management foundation after the Portable
Domain Store Foundation. It does not perform production import, live profile
switching, P4 control, scheduler handoff, remote mutation, or runtime owner
selection.

## Read contract

`GET /up/api/profile_manager` reads the configured Portable Domain Store using
a SQLite read-only connection and returns:

- profile IDs, display names, safe account references, and one current binding;
- `VERIFIED`, `ACTIVE`, `OFFLINE`, or `NEEDS_LOGIN` profile status;
- all observed active bindings;
- explicit disabled control capabilities;
- `runtime_authority.mode = LEGACY` and `active_runtime_owner = null`.

The runtime owner is deliberately not inferred from a portable profile status or
binding. Legacy runtime state remains authoritative until a later, separately
approved switching slice.

The endpoint accepts no query parameters and rejects every non-GET method. A
missing, invalid, or newer store returns a generic 503 response without raw
SQLite details.

## Viewed profile

The frontend profile selector is browser-local view state only. It is not sent
as a control request, is not persisted to the Portable Domain Store, and cannot
change the active runtime owner. The detail panel displays the viewed profile
and current binding separately from the observed runtime-authority banner.

All activation, switching, and binding controls are present only as disabled
affordances so the boundary is visible without exposing a write path.
