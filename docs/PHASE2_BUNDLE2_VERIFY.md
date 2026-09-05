# Phase 2 Bundle 2 — AI create and deferred boundaries

## Revised acceptance

The revised baseline from Issue #1 comment `5549979951` enables only the safe
AI queue-creation endpoint in this bundle:

- `POST /up/api/ai_fix` is enabled through the route -> service -> repository
  boundary.
- `DELETE /up/api/ai_fix/answers` remains deferred because deletion ownership and
  atomic exclusion with the watcher are not proven.
- `POST /up/api/ai_fix/watcher` remains deferred because watcher ownership,
  heartbeat verification, PID identity, and atomic queue exclusion are not
  proven.
- `POST /up/api/sync_remote` and `POST /up/api/sync_all` remain deferred because
  cross-process exclusion with `Local\\AutoGhostStory_RemoteWs` and the
  continuous-sync worker is not proven. A website lock or status preflight is
  not sufficient.

Deferred routes are not in the write dispatch and return the existing JSON 403
without contacting ai tool.

## AI create guarantees

- Only `cycle`, `web`, and `userimport` are accepted.
- Userimport text is trimmed, must be non-empty, must contain no control
  characters, and is limited to 2,000 characters.
- The route sends only a typed JSON payload to the fixed upstream action. It
  never reads or writes the ai tool filesystem directly.
- The repository holds the named Windows mutex `Local\\AutoGhostStory_AiFixCreate`
  while creating a request and keeps successive upstream create calls at least
  1.1 seconds apart. This prevents the golden second-based queue filename from
  being silently reused by concurrent website requests.
- The upstream success envelope is validated strictly, including a safe queue
  basename. Runtime, network, timeout, HTTP, and malformed responses become
  `502 {"error":"ai fix unavailable"}` without leaking upstream details.

## Verification

Executed from the disposable branch `phase2-bundle2-ai-create`:

```bat
python tests\\security\\test_ai_fix.py
```

Result: **29 passed, 0 failed**.

The pre-Bundle 2 security evidence on the same approved scope was documented as
**384 passed, 0 failed**. Therefore the Phase 2 close-out records the
**documented aggregate 384 + 29 = 413 passed, 0 failed**. This is not described
as one combined test command or one single test-process run.

The test uses a local HTTP stub and verifies bearer ordering, strict input
rejection, typed upstream auth, response validation, generic failures,
concurrent filename spacing, and the four deferred write boundaries. No live
AI-fix execution is part of this test.

The historical golden contract smoke remains **16 passed, 0 failed**; it was
not rerun during the Phase 2 close-out audit. No GitHub check runs were
configured for the Bundle 2 PR.
