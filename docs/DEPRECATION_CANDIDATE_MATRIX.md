# Deprecation Candidate Matrix

Status: policy and review matrix only. No removal is authorized by this document.

A current ai tool feature may only move from `DEPRECATION-CANDIDATE` to `DEPRECATED-READY` after replacement parity, operator test, ownership proof, canary proof and rollback proof all pass. Actual removal is a separate PR with dependency checks and an archive/rollback plan.

| Current feature or artifact | Deprecation candidate? | Replacement gate | Dependency | Removal gate |
|---|---|---|---|---|
| Duplicate mirror snapshots and captured `db.html` diagnostic copies | `DEPRECATION-CANDIDATE` | Webapp read contract and browser parity prove the copy is not the only evidence source | Golden `tools/db.html`, legacy API shape, audit evidence | Operator parity PASS, ownership PASS, canary PASS, rollback/archive PASS |
| Read-only probe/helper scripts that duplicate cycle, sync or settings reads | `DEPRECATION-CANDIDATE` | U1 and the documented read endpoints provide equivalent fields and error behavior | API contract tests and legacy health check | No remaining operator/runbook dependency; archive retained; rollback PASS |
| Legacy website route/proxy adapters | `DEPRECATION-CANDIDATE` | New webapp route -> service -> repository path has contract/security parity | 29-route inventory, golden source, deployment routing | Dependency scan clean, shadow comparison PASS, rollback route retained |
| `remote_live_read.ps1` and duplicate remote diagnostics | `DEPRECATION-CANDIDATE` | Remote observations are available through the profile-scoped observation path | Remote profile identity, host binding, read lease | Profile A/B isolation PASS, operator evidence PASS, archive/read fallback PASS |
| `remote_toggle_rows.ps1`, `FullAutoActuator.ps1` and other live mutation helpers | `DEPRECATION-CANDIDATE` | P4 Remote I/O worker owns the same action contract with durable intent, lease and unknown reconciliation | Profile binding, verified identity, worker canary, rollback checkpoint | P4 takeover complete, no duplicate owner, canary PASS, rollback to AutoCycle PASS |
| Global `cycle_stopped.flag` behavior | `DEPRECATION-CANDIDATE` | Profile-scoped durable `cycle_stopped` intent is shadow-read and honored on every host binding | Portable Domain Store, profile handoff and host agent | Profile A/B handoff PASS, stop/resume evidence PASS, rollback mapping retained |
| `start_onboot.ps1` watchdog and `AutoGhostStory_OnBoot` Task Scheduler path | `DEPRECATION-CANDIDATE` | Host Agent owns lifecycle, health, leases and startup ordering | Host binding, service installer, worker/tunnel health checks | Host Agent reboot PASS, failover PASS, old task archive and rollback PASS |
| Quick Tunnel startup/restart helper and random `public_url.txt` authority | `DEPRECATION-CANDIDATE` | Named tunnel or explicit host tunnel agent owns health and stable routing | Cloudflare credentials outside repo, health checks, legacy `/db` fallback | Public health before/after PASS, rollback URL path retained, no legacy downtime |
| Current `db.html` write/control surface | `DEPRECATION-CANDIDATE`, final stage only | Webapp owns approved write/control parity and operator workflow | P4 ownership, session auth, remote profile manager, rollback/archive | Separate removal PR, dependency scan, archive published, rollback rehearsal PASS |

## Frozen Removal Rules

- Never delete a candidate during a feature implementation PR.
- Never remove the golden `db.html` or legacy Cloudflare path before replacement and rollback evidence.
- Never remove a helper while a scheduler, watchdog, runbook or recovery script still depends on it.
- Never copy credentials, sessions, PID files, mutexes or live leases into a portable archive to make a removal gate pass.
- A candidate remains available as an archived rollback artifact until the owner explicitly approves the separate removal PR.
