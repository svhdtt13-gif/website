# Phase 2 Bundle 1 Verification

Bundle 1 covers only:

- `POST /up/api/settings/test_telegram`
- `POST /up/api/settings/open_browser`

## Frozen baseline

The golden reference is `ai tool/WebAppControl/flask/app_public.py`.
The webapp-specific safety addendum is recorded in Issue #1 comment
`5548249779` and `docs/API_CONTRACT.md`.

The implementation does not modify or copy anything in `ai tool`. In particular,
the Telegram live-success path is not claimed because the golden checkout currently
resolves `send_telegram.js` from `TOOLS_DIR.parent`, while the checked-out script is
under `tools/send_telegram.js`. This is an approved live-success limitation.

## Automated verification

Command:

```bat
python tests\security\test_settings_actions.py
```

The test uses a local upstream stub and the real proxy subprocess. It verifies:

- Bearer authentication and zero upstream access on auth failure.
- Query rejection and zero upstream access.
- Non-POST method blocking and zero upstream access.
- Repository-owned Basic Auth and `X-DB-Editor: 1`.
- Telegram empty-body forwarding, exact success/known-failure responses,
  generic 502 sanitization, no retry and no secret echo.
- Browser default URL, explicit URL, launch-rejected response, strict URL policy,
  malformed/non-object JSON rejection, generic 502 sanitization and no retry.
- Proxy liveness after action failures.

Verification result: pending local execution after implementation commit.

## Live verification

Telegram live success is intentionally not run because of the approved golden script
path mismatch and because no production Telegram message may be sent as a test.

Browser live success is intentionally not run against a production URL. A disposable
local harness is required to verify launch acceptance without changing AutoCycle,
sync worker, tunnel, settings, state or activity/change logs.

## Required regression gate

Before PR approval, rerun the Slice 1–11 security/contract suite. The approved
pre-Bundle-1 baseline was `346/346`; Bundle 1 must not reduce that result.
