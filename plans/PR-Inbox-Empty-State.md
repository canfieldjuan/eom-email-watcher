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

1. While the current first page is loading and no rows are shown, the empty list reads "Loading messages…".
2. When the current first-page load fails and no rows are shown, the empty list reads "Messages could not be loaded." The status line keeps showing the error, as today.
3. After a successful first-page load, or a successful "Clear local history", an empty list shows exactly today's texts: the sender texts from `inboxSenderEmptyText`, "No watched messages match these filters.", or "No watched messages yet. …".
4. A failed "Load more" or background refresh while rows are shown changes nothing in the list.

### State and invariants

- One module-level page state, `"loading" | "loaded" | "failed"`, starts as `"loading"`. Only these change it:
  - `clearInboxPageForAccountChange`: `"loading"`, before it renders;
  - `loadInbox` with `append === false` and a current generation: `"loaded"` on success, before it renders; on failure, `"failed"` plus a re-render, but only when `inboxItems` is empty;
  - a successful `inbox_clear`: `"loaded"`.
- A response for a superseded generation never changes the state. The existing generation check returns before any state write.
- The empty-state branch is the only reader.

### Failure cases

- A first-page failure while rows are shown (background refresh): the state is unchanged and rows stay, as today.
- `loadInbox` returning early (mutation in flight, stale effect scope): the state is unchanged. A later load or deferred reload sets it.

### Files touched

- `desktop/src/main.ts`
- `desktop/test/inboxFilters.test.ts`

## Mechanism

Add the state variable next to `inboxQueryEpoch`. Set it at the four points above. In `renderInbox`'s empty branch, return "Loading messages…" or "Messages could not be loaded." first, and otherwise fall through to today's chain unchanged.

## Intentional

- The failure text stays generic, because the status line already shows the specific error.

## Deferred

- A failed non-scope filter Apply leaves the previous rows visible under the new filters with an error status. That predates this slice and needs its own decision.

## Verification

- Fail-first source-wiring tests in `desktop/test/inboxFilters.test.ts`:
  - `clearInboxPageForAccountChange` sets the state to `"loading"` before `renderInbox`;
  - a non-append failure with no rows sets `"failed"` and re-renders;
  - success sets `"loaded"` before rendering;
  - `inbox_clear` success sets `"loaded"`;
  - the empty branch checks the state before the query-derived texts;
  - the two new strings are present.
- Commands, each a narrowing of a prefix declared in the Codex scope file:
  - `node --test --experimental-strip-types --test-isolation=none desktop/test/*.test.ts`
  - `pnpm --dir desktop build`
- Effect check, reviewer-run: the stubbed-backend preview, with `inbox_query` made slow, then failing, for one sender. The empty list must read "Loading messages…" during the load and "Messages could not be loaded." after the failure, and today's sender text when the sender truly has no messages.

## Estimated diff size

| File | LOC |
|---|---:|
| `desktop/src/main.ts` | 15 |
| `desktop/test/inboxFilters.test.ts` | 30 |
| **Total** | **45** |

## Codex scope file

```json
{
  "roots": ["<worktree>"],
  "allow": ["desktop/src/main.ts", "desktop/test/inboxFilters.test.ts"],
  "goal": "Truthful Inbox empty state (plans/PR-Inbox-Empty-State.md)",
  "plan": "plans/PR-Inbox-Empty-State.md",
  "verify": {
    "commands": ["node --test --experimental-strip-types --test-isolation=none", "pnpm --dir desktop build"],
    "max_runs": 4
  },
  "churn": { "max_lines": 70, "max_new_tests": 6 }
}
```
