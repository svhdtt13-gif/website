# U1 Read-only Dashboard Contract

U1 is a browser-only read surface. It must not replace or mutate the golden `tools/db.html` operator UI.

## Read Sources

- `/up/api/cycle/status` and `/up/api/cycle_status`: cycle/process state.
- `/up/api/sync_status` and `/up/api/status`: Auto Sync and general client telemetry.
- `/up/api/ai_fix/status`: watcher, queue, model and recent result metadata.
- `/up/api/cycle/backup`: backup metadata list only.
- `/up/api/settings`: backend-approved public settings projection only.

## Safety Contract

- All frontend requests are same-origin `GET` requests with `cache: no-store`.
- U1 does not call `/up/api/remote_live` and does not expose client selectors.
- U1 does not expose POST, PUT, PATCH or DELETE controls.
- Backup create/delete/restore remains on the guarded golden operator surface.
- Settings writes, Telegram tests and browser actions remain absent from U1.
- Partial source failure must leave the affected panel unavailable without enabling any write action.
- The frontend refreshes status every 10 seconds and displays the last refresh time.

## Acceptance Evidence

Before merge, compare U1 and the golden `/db` page in a real browser, run the contract/security tests, and record network evidence showing only the listed GET sources. Check the public legacy Cloudflare `/db` health before and after verification.
