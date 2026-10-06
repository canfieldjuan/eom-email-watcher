# Truthful Inbox Empty State

## Why this slice exists

Follow-up from #185 (listed in its PR), extended to every scope change. The Inbox's empty-list text always asserts that no messages exist, even while the page is still loading or after the load failed:

- A sender or account change goes through `clearInboxPageForAccountChange` (`desktop/src/main.ts:2553`). It renders an empty list before its request starts, so the list shows "No retained messages from this sender." (sender view) or "No watched messages match these filters." (account change) while the page is still loading.
- When that first-page load fails, `loadInbox` (`desktop/src/main.ts:2684`) sets the error in the status line but does not re-render. The same "no messages" text stays beside the error, although nothing is known about what exists.

### Problem-derived contract

- Root cause: the empty-state text (`desktop/src/main.ts:1657-1680`) is derived only from the query. Nothing records whether the page it describes actually loaded.
- Correct fix must touch:
  - record the state of the current first page (loading, loaded, failed) at the places that change it;
  - make the empty-state text check that state before claiming absence.
- Must not change:
  - the existing empty-state texts when a load succeeded;
  - the status line and its error text;
  - paging, refresh, and request-generation behavior;
  - the stale-row behavior of a failed non-scope filter Apply (see Deferred).

## Scope (this PR)

Ownership lane: inbox-empty-state
Slice phase: correctness fix on an existing operator-visible state

### Observable behavior

1. While a first-page request is actually in flight and no rows are shown, the empty list reads "Loading messages…". This covers every first-page request: a sender or account change, Apply or Reset of the secondary filters, a scheduled check, and the initial load.
1a. After a scope change has cleared the list and before any first-page request for it has been issued, the empty list reads "Messages not loaded yet." That includes a load skipped by a guard (#203).
2. When the current first-page request fails, the empty list reads "Messages could not be loaded." whenever no rows are shown, including after a later delete empties a list that was showing stale rows. The status line keeps showing the error, as today.
3. After a successful first-page load, or a successful "Clear local history", an empty list shows exactly today's texts: the sender texts from `inboxSenderEmptyText`, "No watched messages match these filters.", or "No watched messages yet. …".
4. A failed "Load more" or background refresh while rows are shown changes nothing in the list.

### State and invariants

- One module-level page state, `"pending" | "loading" | "loaded" | "failed"`, starts as `"loading"`, because the configured startup issues the first request. Each value reflects what happened to requests for the current query. Only these change it:
  - `clearInboxPageForAccountChange`: `"pending"`, before it renders;
  - `loadInbox` with `append === false`, once its early-return guards have passed and the request is about to be sent: `"loading"`, recording that request's generation as the owner of the state, plus a re-render when `inboxItems` is empty;
  - `loadInbox` with `append === false` whose result is discarded as stale (the two `mailboxEffectRequestIsCurrent` returns, `desktop/src/main.ts:2700,2710`): if the state is still `"loading"` and still owned by this request's generation, it becomes `"pending"`, plus a re-render when `inboxItems` is empty. A request that was superseded, for example by a delete's generation bump (`desktop/src/main.ts:2620`), never leaves "Loading…" behind with nothing in flight;
  - `loadInbox` with `append === false` and a current generation: `"loaded"` on success, before it renders; `"failed"` on failure, always, plus a re-render when `inboxItems` is empty;
  - a successful `inbox_clear`: `"loaded"`.
- This slice does not schedule or re-issue loads. Whether a skipped load is retried is #203. `loadInbox`'s guards and `inboxReloadAfterMutation` are unchanged.
- A response for a superseded generation never sets `"loaded"` or `"failed"`. Its only state write is releasing its own `"loading"` to `"pending"`, and only while its generation still owns the state (above).
- The empty-state branch is the only reader.

### Failure cases

- A first-page failure while rows are shown (background refresh): the state becomes `"failed"` and the rows stay, as today. Only the empty-list text reads the state.
- A first-page load skipped by a guard (an in-flight mutation, an in-flight mail-account operation, or a stale effect scope): the state stays `"pending"` and the list reads "Messages not loaded yet." until a later request runs. The missing retry is #203.
- An in-flight first-page request superseded before it returns (a delete, a clear, or a newer load bumping the generation): when it returns, it releases its `"loading"` to `"pending"`, unless a newer request already owns the state.

### Files touched

- `desktop/src/main.ts`
- `desktop/test/inboxFilters.test.ts`
- `desktop/test/automationDecision.test.ts`: its source-wiring test at `:92` must find the commit render explicitly; see the implementation finding below

## Mechanism

Add the state variable next to `inboxQueryEpoch`. Set it at the points above; the `"loading"` set and its re-render go after `loadInbox`'s guards, before its `await`. In `renderInbox`'s empty branch, return the `pending`, `loading`, or `failed` text first; otherwise fall through to today's chain unchanged.

## Intentional

- The failure text stays generic, because the status line already shows the specific error.

## Deferred

- A failed non-scope filter Apply leaves the previous rows visible under the new filters with an error status. That predates this slice and needs its own decision.

## Verification

- Fail-first source-wiring tests in `desktop/test/inboxFilters.test.ts`:
  - `clearInboxPageForAccountChange` sets `"pending"` before `renderInbox`;
  - `loadInbox` with `append === false` sets `"loading"` after its guards and before its request, and re-renders when there are no rows (covers an empty-list Apply or Reset);
  - a current non-append failure sets `"failed"` whether or not rows are shown, and re-renders only with no rows;
  - a stale non-append return releases `"loading"` to `"pending"` only when its own generation owns the state, so it never overwrites a newer request's state;
  - `loadInbox`'s early returns and `inboxReloadAfterMutation` are untouched;
  - success sets `"loaded"` before rendering;
  - `inbox_clear` success sets `"loaded"`;
  - the empty branch checks the state before the query-derived texts;
  - the three new strings are present.
- Commands, each a narrowing of a prefix declared in the Codex scope file:
  - `node --test --experimental-strip-types --test-isolation=none desktop/test/*.test.ts`
  - `pnpm --dir desktop build`
- Effect check, reviewer-run: the stubbed-backend preview, with `inbox_query` made slow, then failing, for one sender. The empty list must read "Loading messages…" during the load, "Messages could not be loaded." after the failure, and today's sender text when the sender truly has no messages.

## Estimated diff size

| File | LOC |
|---|---:|
| `desktop/src/main.ts` | 28 |
| `desktop/test/inboxFilters.test.ts` | 40 |
| **Total** | **68** |

## Codex scope file

```json
{
  "roots": ["<worktree>"],
  "allow": ["desktop/src/main.ts", "desktop/test/inboxFilters.test.ts", "desktop/test/automationDecision.test.ts"],
  "goal": "Truthful Inbox empty state (plans/PR-Inbox-Empty-State.md)",
  "plan": "plans/PR-Inbox-Empty-State.md",
  "verify": {
    "commands": ["node --test --experimental-strip-types --test-isolation=none", "pnpm --dir desktop build"],
    "max_runs": 4
  },
  "churn": { "max_lines": 90, "max_new_tests": 8 }
}
```

## Review amendments (Codex review of `f46014a`)

1. An Apply or Reset of only secondary filters with an empty list never entered `"loading"`, so it showed a false "no messages" during the request. `loadInbox` now sets `"loading"` for any first-page request while no rows are shown.
2. A scope clear during an in-flight delete or clear left `"loading"` with no request, because only sender selection queued a reload. `loadInbox` now owns that deferral for every first-page load.

## Review amendments, round 3 (Codex review of `1c2e0c0`): the cut

Round 2's second fix made `loadInbox` queue deferred reloads. That stretched this slice from empty-state text into reload scheduling, and round 3 found three more cases of that one class: a skip during a mail-account operation that queues nothing; a deferred span refresh collapsing to page one; and a failure not recorded while rows remain. The class is a pre-existing scheduling defect with more than one owner, so:

1. The deferral change is withdrawn; `loadInbox`'s guards are unchanged. The missing retry is #203, which also records that a correct fix must preserve span refreshes.
2. The state now follows only what happened to requests, adding `"pending"` (cleared, nothing requested yet): "Messages not loaded yet." No path can claim "Loading…" without a request in flight.
3. A current failure is always recorded as `"failed"`, even while rows remain, so a later delete that empties the list shows the failure, not absence.

## Review amendments, round 4 (Codex review of `33b4e7b`)

1. A background refresh superseded by a delete's generation bump returned stale and left `"loading"`. If the delete emptied the list, it showed "Loading…" with nothing in flight. The request that sets `"loading"` now owns it by generation, and its stale return releases it to `"pending"` unless a newer request has taken ownership. This stays inside `loadInbox`, the single owner of request-derived state.

## Implementation finding (Codex session `01a10e6a`)

`desktop/test/automationDecision.test.ts:92` ("decision refresh requires its own committed inbox projection") guards that `loadInbox` returns `false` only before it commits a page and `true` only after; decision refresh relies on `const committed = await loadInbox()`. The test finds the commit as the *first* `renderInbox(inboxItems);` and expects the stale guard as a single line. This plan adds an earlier non-commit render (the `"loading"` re-render of the existing empty list) and turns the stale guard into a block. The invariant still holds; the test's text anchors do not. The test is added to Files touched. It must locate the commit render by the committed-page assignment (`inboxItems = page.items`), accept the block-form stale guard, and keep every existing assertion's intent: no `return true` before the commit render, no `return false` after it, and decision refresh still awaiting `loadInbox()`.
