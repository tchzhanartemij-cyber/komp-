# Independent review resolution, 2026-10-28

Source: [unaltered independent review](QA-20261028-independent-review.txt). The reviewer inspected the diff and did not run tests. This follow-up stays in the same worktree, based on 2feb760. No production, deployment, browser, configuration or model calls were made.

| Review finding / requested correction | Resolution and local regression evidence |
| --- | --- |
| Empty memory panel becomes inaccessible | Always available “Новая страница” button outside the panel opens the overview with zero entities. Closed state survives data refresh, graph revisit and reload via localStorage (best effort if storage unavailable). Overview close uses icon-btn. Harness checks explicit reopen and stored state. |
| Escape steals native behavior; focus/scroll | Only graph/panel Escape outside native input/select/textarea closes memory; an open dialog excludes it. Focus returns to a connected focusable trigger or search. Revisit uses renderPanel, without selection or scroll. Harness covers dialog/select/search, graph close, mobile selection and closed revisit. Browser focus/layout validation remains pending. |
| Newly created page no longer selected | Confirmed memory creation queues the entity key and required data revision. Selection opens only once matching fresh data contains the entity. Later new-page edits remain in the per-page draft map. Existing selections are rendered without calling selectNode on revisit/background refresh. |
| POST waits for possibly two timed GETs | Confirmed HTTP success returns immediately, without reading a successful response body or awaiting GET. Invalidation is synchronous; background refresh errors are handled. Harness awaits form completion while GET remains unresolved and checks both unchanged reset and later edits. |
| dataFlight completion microtask gap | Loop and ownership cleanup execute within the same async job; no separate promise finalizer owns dataFlight. Fresh calls invalidate immediately and either request another iteration or start a new flight after cleanup. Tests cover stale response exclusion, coalescing, completion-boundary invalidation, timeout and backoff. Visibility recovery forces refresh. |
| Persistent error masks offline; clearing by path | Separate transport status and per-action mutation failure map, keyed by path plus serialized submitted body. Exact success clears only its action. Each error has “Скрыть ошибку” / aria-label “Скрыть ошибку сохранения”; confirmed HTTP rejection offers “Повторить”. Harness checks same-path different bodies, dismiss, offline status and HTTP 400/401/409/500. |
| Ambiguous network retry duplicates/toggles | Fetch rejection states “Результат сохранения неизвестен”, instructs refresh/check and manual retry only after checking, and offers no retry button. Form handler preserves that restriction. No backend idempotency changes. |
| Missing namedItem throws after success | Reset restores hidden values only when namedItem exists. Harness removes the control on reset and verifies success completes. |
| Mock promises may silently exit | Harness fails on beforeExit unless all async assertions completed; it drains and asserts no outstanding requests/dataFlight at the end. |

## Verification provenance

The parent independently ran the existing **25/25 tests outside the worker sandbox, with no skips**. The actual `python3 -I` HTTP checks for authentication, data, agent and office succeeded. This is independent test evidence supplied by the parent, not a claim that this worker performed those HTTP checks.

After these changes, the worker ran `python3 -m unittest discover -s openclaw/dashboard/tests -v`: 25 tests, 22 passed and 3 socket-dependent skips because this sandbox prohibits sockets. The expanded Node harness and JavaScript syntax check passed. This supersedes any implication in the earlier QA document that the worker sandbox result was the only available startup evidence.

No browser QA was performed for this follow-up. The parent should still verify real dialog Escape, focus restoration, empty graph reopen, stored closed state, mobile scroll, fresh new-page selection, hanging GET plus successful POST, and status/dismiss/retry labels in an isolated synthetic browser session. Drafts remain tab-memory only; panel visibility is stored separately.
