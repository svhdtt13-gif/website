# U1 Profile Context Boundary

U1 remains read-only, but it is profile-aware by construction. The dashboard must not model the current ai tool as one global account.

## Context Shape

A future profile-aware read response may expose `profile_context` with these fields:

- `host_id`
- `profile_id`
- `remote_account` or `account_ref`
- `verified_identity` or `identity_ref`
- `control_authority`

Until the backend exposes that field, U1 displays host/profile/account as unresolved and displays `READ ONLY / FAIL CLOSED` for authority. It does not invent a default host, account, room or session.

## Safety Rules

- A viewed profile is not a mutation target.
- Missing host, profile, binding or identity data cannot enable control.
- A profile context is not a credential or session store.
- U1 does not call `remote_live`, select clients, switch profiles or send commands.
- Future controls must require matching `host_id + profile_id + verified_identity + active binding lease`.

This boundary lets U1 continue parity work while the Portable Domain Store, Remote Profile Manager and Host Agent remain future design/implementation stages.
