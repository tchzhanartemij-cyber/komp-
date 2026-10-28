# Overnight dashboard integration

Preflight: retained the existing design, life data, personal-link/cookie authentication,
projects, chat history, mobile navigation and composer. Existing CLI chat remains the
non-agent mode. No production data was opened or written for development.

The installed OpenClaw `gateway/openresponses-http-api.md` documents a supported
normal agent runtime, persistent user routing and SSE. This integration uses that
runtime rather than implementing another tool executor. `tools-invoke-http-api.md`
is not a substitute for an agent run; no tools proxy was added. Current Gateway
loopback port is 18789 with token authentication; responses HTTP is disabled per
owner-provided preflight. This is a live integration blocker. Only configured Anthropic model identifiers are read from OpenClaw configuration;
no configuration is changed and no endpoint is enabled.

## Parent setup (deployment not performed)

Keep these unset for the default fail-closed unavailable state. An operator must
separately decide whether to enable Gateway responses using its supported setting
`gateway.http.endpoints.responses.enabled`. The dashboard cannot enable it.

Active execution also requires a valid dashboard access token (the existing dashboard
link/cookie authentication). Handler rejects execution with HTTP 503 when the dashboard
has no token protection, even if all Agent environment variables are set. Only when an
operator separately supplies `SVODKA_AGENT_TRUSTED_PROXY=1` may a tokenless dashboard
execute work behind an independently authenticated, access-restricted reverse proxy.
This flag trusts the operator's proxy setup; it does not inspect or trust request headers.
Agent and office snapshots expose this blocked state too. Existing read-only dashboard
behavior is retained. No environment settings were changed during this refinement.

Server environment required for an active agent:

```
SVODKA_AGENT_ENABLED=1
SVODKA_AGENT_URL=http://127.0.0.1:18789/v1/responses
SVODKA_AGENT_TOKEN_FILE=/absolute/path/to/private-token-file
SVODKA_AGENT_ID=main
SVODKA_AGENT_USER=svodka-owner-dashboard
```

The token file must be readable only by its owner (0600), contain only the operator
token, and never be served as static content. Do not copy tokens into browser code,
URLs, screenshots or documentation. Use a dedicated stable user per deployment;
changing it changes Gateway conversation continuity. Agent ID and user are fixed by
the process, never supplied by the browser. Only literal loopback HTTP URLs with the
exact responses path are accepted; redirects and environment proxies are disabled.

This is full operator access, including owner-sender semantics. Opting in explicitly
permits server-side agent work using the Gateway's existing permissions. This is not
a reduced-scope credential. Retain existing Gateway access controls, approval and tool
policies; do not expand them to make this dashboard work. Restrict dashboard access
to the owner using the existing token/proxy protections. Do not opt in on an open or
shared dashboard. Browser disconnect does not cancel work: the dashboard server owns
the upstream SSE connection and persists deltas and terminal state in
`LIFE_DIR/dashboard-agent/agent.sqlite3`. Browser polling is observation only. Server
restart marks unfinished runs interrupted; it never automatically retries work with
possible side effects. One active run is allowed; no arbitrary session, URL, header,
model override or tool parameters are accepted from clients. History includes the
latest 20 dashboard runs, with each output limited to 16,384 Unicode characters
(at most 64 KiB of UTF-8) and an explicit `truncated` flag; this does not read Gateway transcripts. The office excludes
message content and shows real run statuses and timestamps. Existing coder jobs are
optional read-only metadata via `SVODKA_CODER_QUEUE_FILE=/absolute/path/tasks.json`;
only id/status/created/updated are returned, never prompts/results/repo paths/sessions.
No queue file is modified. Empty and unavailable sources are explicit.

## Models and UX

Codex metadata comes from `~/.codex/models_cache.json` (override for isolated tests:
`SVODKA_CODEX_MODELS_FILE`). Only visible model slug/display name are extracted;
identity and other cache metadata never leave the server. Observed listed models
include gpt-6-astra and gpt-6.1-sol. Listed/configured means an explicit attempt is
possible when CLI exists, not verified execution, subscription eligibility or remaining
quota. Models are labelled accordingly. CLI presence never proves model availability.
Claude retains its CLI default. Concrete Claude options come only from keys under
`agents.defaults.models` in `~/.openclaw/openclaw.json` with the `anthropic/` prefix.
Only validated Claude identifiers are extracted; other configuration fields and secrets
are never returned. `anthropic/claude-opus-5-5` becomes exact CLI slug
`claude-opus-5-5`. `SVODKA_CLAUDE_MODELS`, when set, overrides discovery with a
comma-separated list of exact Claude slugs (empty disables concrete choices).
Choices are labelled configured and unverified, retain quota/pause checks, and do not
claim runnable status. Explicit ChatGPT model choices also respect the provider pause.
Explicit choice attempts
only that model and surfaces failure; Auto tries eligible provider routes in order.
No Claude or Codex generation was invoked during development.

Default layout: AI → Chat / Server agent switch, embedded persistent agent chat.
Alternative: `?agentLayout=separate#agent`, or click the layout button inside AI.
Preference persists locally. Both layouts retain the separate Agents office tab.
Running desks animate only while the recorded status is running; reduced-motion
preference disables that animation. Each real row has a CSS workstation with desk, monitor, chair and avatar silhouette;
empty sources produce no workstations. No simulated workers or progress percentages.
Events persist actual delta receipt/terminal status, timestamp and output character count.
Polling skips hidden documents, permits one request batch at a time and preserves
unchanged run text nodes. Mobile navigation shrinks within viewport bounds with all
seven buttons in separate mode. Added user-facing status labels are Russian; API
status codes remain unchanged.

## Verification and blockers

Run `python3 -m unittest discover -s openclaw/dashboard/tests -v` and compile both
Python files. Extract the inline script and run `node --check`.
Tests use temporary owned data and mocked HTTP transport; no production writes.
The suite includes metadata sanitization, fixed routing, opt-in, private token file,
auth cookies/personal link, SSE completion/incomplete/failure/truncation, persistence,
restart recovery, queue allowlist and error secret redaction. Two additional real HTTP
integration tests automatically skip if local sockets are prohibited.

The earlier parent verification reported all 9 tests passing outside the sandbox and
actual desktop/mobile browser checks. Those are prior parent results, not checks run
during this correction pass. This sandbox prohibits socket creation
(`PermissionError: Operation not permitted`), so the two socket-dependent tests skip here.
The new layout regression test executes the actual layout/navigation functions in Node
with a minimal DOM mock; it does not claim a new visual browser check.
For parent verification, use a new empty LIFE_DIR, a test dashboard token file and
an unused loopback port; leave active agent disabled unless pointing at a mock.
Check desktop/mobile at #ai, #agents and ?agentLayout=separate#agent; never use real
personal links in screenshots. Live Gateway execution remains unverified.

Coder registration blocker: normal coder queue supports add/list/show/answer/redo/
merge/cancel; add would launch Claude. No safe external-completion import was found
in its CLI source. Do not enqueue this completed work or manually edit tasks.json.
Register it only when a supported external-completion mechanism exists.

Refinement local result: 13 tests discovered, 11 passed and 2 socket-dependent tests skipped;
Python compilation, `node --check`, and `git diff --check` passed. Assistant answers
have a full-answer copy action; streaming respects the reader's scroll position.

Git metadata is read-only in this sandbox. No commit, deployment, configuration change,
restart, model network probe or queue registration was performed.


## Additional security and robustness corrections

Agent POST checks authentication, application/json and a supplied Origin against the
request Host before reading JSON; token protection or explicit trusted proxy opt-in
remains required. JSON rejects duplicate keys and nonstandard numeric constants.
Snapshots fetch only bounded output substrings from SQLite and show truncation in Russian.
All SQLite connections close explicitly, including failed transactions. Deltas persist
at most once per 0.4 seconds, with final output always flushed at terminal state.
SSE uses bounded readline and event accumulation, processes a final event at EOF,
and enforces a 600-second monotonic deadline, with socket shutdown interrupting blocked
reads. BytesIO mock responses require no socket interface. Dead worker runs become
interrupted on the next snapshot or submission once storage is writable; work is never
retried automatically. Live workers continue to exclude concurrent submissions.
CLI model identifiers must begin with a letter/digit and are passed as --model=value.
Separate mode keeps its agent panel visible after Chat; embedded #agent opens the
agent panel in AI. Russian UI labels and the existing hidden rule remain intact.

Correction verification: 21 tests discovered, 19 passed, 2 socket-dependent skips;
Python compilation, inline JavaScript syntax and git diff whitespace checks passed.
No config, deployment, merge, production restart, model network probe or commit.

## Final independent verification, 2026-10-28 ~03:05 UTC+8

Outside the Codex sandbox, all 21 tests passed including both real loopback HTTP
tests (no skips). Python compilation and diff whitespace checks passed. Native
Claude Sonnet review was invoked only after the usage command confirmed reset
(02:51: 0% consumed); after corrections its final review found no critical/high
issues. Remaining low-priority observation: SQLite run/event retention is not yet
automatically pruned; API responses are bounded.

An isolated authenticated server used empty /tmp/svodka-preview-life on loopback
18797, a throwaway test access token, and read-only sanitized actual coder queue
metadata. No production data writes. Browser checked desktop 1440x1000 and iPhone13
390px: embedded agent, separate agent, model picker, office, disabled capability.
No horizontal overflow; seven-button mobile navigation now fits (321px). Browser
runtime errors: zero. Astra/Sol and configured Opus are listed with explicit
unverified-execution labels, not represented as successful live probes.

Screenshot evidence (outside repo, test credentials only):
- desktop office: /home/jarvis/.openclaw/media/browser/deab9696-6e14-4f00-aa61-8341f52f7259.png
- mobile office: /home/jarvis/.openclaw/media/browser/ed7509f1-bdda-42be-a940-95705d04dc44.jpg
- desktop embedded: /home/jarvis/.openclaw/media/browser/9dec5e26-aaae-403c-8fab-5d9e61c0e4e8.png
- desktop separate: /home/jarvis/.openclaw/media/browser/d7f1c652-d295-4ff2-ac9c-578618f7a612.png
- mobile embedded: /home/jarvis/.openclaw/media/browser/e2598a0f-c34a-44b9-ab7c-ad9e2754be31.png
- mobile models: /home/jarvis/.openclaw/media/browser/7f729a52-adfc-4ab3-b5f7-44ef1af2a75f.jpg

The live Gateway endpoint remains disabled and was NOT changed. Actual server-agent
execution is therefore prepared and mock-tested, not live-proven. Approval of the
UI must not be confused with approval to enable full-operator agent access.

## Authorized live verification, 2026-10-28 09:50 UTC+8

This section supersedes the earlier disabled-endpoint blocker. The owner separately
approved protected Gateway integration; the parent enabled responses through the
supported config tool before this verification. No Gateway config, tool policy,
public listener, systemd unit or production dashboard was modified here.

Effective settings: responses enabled; token auth; bind loopback. Actual listeners
were 127.0.0.1:18789 and [::1]:18789 only. POST /v1/responses without a token and
with an invalid token each returned 401. The actual authenticated SSE request below
completed successfully, proving the hot-reloaded endpoint is active.

A backend-only copy of the existing local Gateway token was saved without printing
its contents to /home/jarvis/.config/jarvis/svodka-agent/gateway-token (0600), under
a 0700 directory outside the repository/static root. The copied credential is the
existing full-operator token, not a newly restricted credential. No auth or policy
was weakened. Keep this file current if the Gateway token is rotated.

One isolated dashboard instance ran on 127.0.0.1:18798 with a fresh empty LIFE_DIR,
its own random dashboard access token, no coder queue and unique fixed user
svodka-liveproof-4a5600ebc5762d49. One harmless message asked for SVODKA_LIVE_OK
and prohibited tools, files, memory and external actions. POST /api/agent/send
returned 202; run 1 persisted running/delta/completed events and the exact answer
SVODKA_LIVE_OK in approximately 7.6 seconds. Office metadata also showed completed.
Unauthenticated dashboard API access returned 401, and a cross-origin submission
returned 403 before creating a run. Public agent/office/model JSON and HTML were
checked against both credentials: no credential contents were present.

Important model-selection distinction: production agent.py deliberately routes to
openclaw/main and has no explicit backend-model environment variable. To avoid
using Claude during this isolated proof, the test harness added the documented
x-openclaw-model: openai/gpt-6.1-sol header to its one upstream request only.
The repository implementation was not changed. Gateway transcript evidence confirms
provider=openai, model=responseModel=gpt-6.1-sol, final SVODKA_LIVE_OK and zero tool
calls. Session-list default model metadata still says Claude; it is not execution
provenance and must not be used instead of the actual assistant transcript.

Private evidence and isolated environment:
/home/jarvis/.config/jarvis/svodka-agent/live-test-20261028-095042/proof.json
/home/jarvis/.config/jarvis/svodka-agent/live-test-20261028-095042/isolated.env
Test harness (contains no credentials): /tmp/svodka-live-proof-20261028.py
The test server was shut down and port 18798 verified closed after completion.
Gateway conversation bookkeeping and isolated test SQLite were the only live-run
writes; no production life data was read/written by this verification.

### Exact production connection, prepared but not activated

A 0600 file /home/jarvis/.config/jarvis/svodka-agent/production.env.prepared
contains the following non-secret wiring (the secret remains in its token file):

```
SVODKA_AGENT_ENABLED=1
SVODKA_AGENT_URL=http://127.0.0.1:18789/v1/responses
SVODKA_AGENT_TOKEN_FILE=/home/jarvis/.config/jarvis/svodka-agent/gateway-token
SVODKA_AGENT_ID=main
SVODKA_AGENT_USER=svodka-owner-dashboard
```

After explicit deployment approval, inspect the current dashboard service and merge
these variables into its existing environment without replacing other settings.
Keep the existing production LIFE_DIR, loopback bind and dashboard access token;
verify the owner-only authenticated proxy route. Do not set TRUSTED_PROXY merely
to bypass missing dashboard protection. Review the approved worktree/commit and
restart only the dashboard after its separate deployment/service-change approval.
No production merge, deployment, service change or restart has happened.

The prepared production env uses main's configured model (currently Claude Opus,
with GPT Sol fallback), NOT the test-only GPT header. If production must be pinned
to GPT Sol, that requires a separately reviewed backend-only model selector or an
approved existing Gateway agent/session-model setting; it is not silently solved
by the UI model picker or by this env file. No such configuration change was made.
