# Wide, Compact Inbox

## Why this slice exists

After installing the #185 release candidate, the operator reported that the Inbox does not use enough of the screen. A preview of the real frontend (`main.ts` and `styles.css` at `680f5d2`, a stubbed backend, and sample data) at the operator's window size of 1524×785 CSS pixels measured the layout:

- The shell is 760px wide; the sender column 190px; the message column 550px.
- On first open, **no message is visible**. The intro block and the open filter panel fill the first screen.
- Widening alone does not fix the second point. At 80% width the shell is 1207px and the message column 887px, but every row is the same height.
- With the intro hidden and the filters collapsed, three full messages and part of a fourth show on the first screen.

The operator chose 80% width plus three compaction changes: "put all of it in 186".

### Problem-derived contract

- Factors that control the layout:
  1. `.shell { width: min(760px, calc(100% - 40px)); }` (`desktop/src/styles.css:39-41`) wraps every view (`desktop/src/main.ts:516`). No ancestor sets a width; only `body` sets `min-width: 320px`.
  2. The Inbox sender column is capped at `minmax(140px, 190px)` (`desktop/src/styles.css:200-203`).
  3. The intro block (`desktop/src/main.ts:517-521`: eyebrow, `h1`, lede) sits above every view.
  4. The filter panel is rendered `<details ... open>` (`desktop/src/main.ts`, the inbox markup).
  5. Since #185, an expanded row repeats what its toggle row shows. The card keeps its original header (`desktop/src/main.ts:1726-1767`: sender name, address, received time, subject `h3`) and footer badges (`desktop/src/main.ts:2320-2336`: category, state).
- Must not change:
  - Watchlist, Health, or Settings layout;
  - filter behavior (Apply, Reset, values);
  - card content other than removing the repeated items;
  - any control or action;
  - the 760px and 680px breakpoints.

## Scope (this PR)

Ownership lane: wide-inbox
Slice phase: layout correction on an operator-reported defect

### Observable behavior

1. **Width.** While the Inbox or Expiry Ledger tab is shown, at viewport widths of 681px or more, the shell is `max(80%, min(760px, calc(100% - 40px)))` wide: 80% of the window, never narrower than today. That is 1207px at the operator's size. On a 2560-pixel monitor at the same 1.25× scale (2048 CSS pixels) it is about 1640px; on a 2560-CSS-pixel window it is about 2048px. At 681px and up, Watchlist, Health, and Settings keep today's `min(760px, calc(100% - 40px))`. At 680px and below, every view, the Inbox and Expiry Ledger included, uses the existing phone rule `min(100% - 28px, 760px)` (`desktop/src/styles.css:1356`), unchanged.
2. **Sender column.** It becomes `minmax(220px, 300px)`; the message column takes the rest. Below 760px the columns still stack.
3. **Intro.** While the Inbox is shown, the intro block is visually hidden. It stays in the DOM and in the accessibility tree, using the standard visually-hidden clip pattern, not `display: none`, so the page keeps its `h1`. On every other tab it shows as today.
4. **Filters.** The filter panel starts collapsed: `<details>` without `open`. One click on its summary opens it. Its controls, Apply, Reset, and values behave as today.
5. **No repeated header.** An expanded row's content no longer repeats:
   - the sender name and address;
   - the received time;
   - the subject `h3`;
   - the footer's category and state badges.

   The toggle row above it already shows them. These stay, with their current text:
   - the provenance lines ("Mailbox: …" in the all-accounts view, and "Admitted by …");
   - the priority badge;
   - the footer actions (Retry analysis, Delete locally);
   - all of summary, details, calendar proposal, and attachments.
6. The view marker: `.shell` carries `data-view` from launch (`"inbox"`, the view visible in the markup), and `showView` sets it from its `view` argument. Width and intro follow the active tab immediately.

### Invariants

- Only `showView` changes the marker. It uses the same argument that toggles `hidden` (`desktop/src/main.ts:1190-1200`), so the marker and the visible view cannot disagree.
- Nothing is removed from the DOM or the accessibility tree except the repeated row items in item 5.
- Every rule for the wide shell and the hidden intro is keyed to `data-view`. Without a marker, the layout falls back to today's.

### Failure cases

- An unknown view cannot occur: `showView` takes a closed union. A missing marker falls back to today's layout, never a broken one.
- A collapsed panel could hide active filters mid-session. At launch no filter is active, so collapsed-by-default is safe. An active-filter indicator is deferred (below).

### Files touched

- `desktop/src/main.ts`: the shell marker in the markup and in `showView`; `open` removed from the filter panel; the repeated header items left out of the expanded content.
- `desktop/src/styles.css`: the 80% rule, the Inbox intro rule, the sender column.
- `desktop/test/inboxFilters.test.ts`: new source-wiring tests. The existing "initially open native disclosure" test is changed to "initially collapsed", because the operator reversed that decision.

## Mechanism

- **Width.** Inside `@media (min-width: 681px)`, add `.shell[data-view="inbox"], .shell[data-view="expiry-ledger"] { width: max(80%, min(760px, calc(100% - 40px))); }`. The media query is required: the attribute selector outranks the plain `.shell` in `@media (max-width: 680px)` (`desktop/src/styles.css:1354-1357`), so outside the query it would override phone widths.
- **Intro.** `.shell[data-view="inbox"] .intro` uses the visually-hidden pattern (`position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap;`).
- **De-duplication.** When the expanded content is built (#185's `content` region), append only the non-repeated nodes: the provenance spans, summary, details, proposal, attachments, and a footer without the category and state badges. The toggle row's `received`, `category`, and `state` stop being clones and use their source nodes. The fewest lines that achieve this wins.

## Intentional

- 80% with a 760px floor scales with the window rather than stopping at a fixed cap.
- The intro is hidden only on the Inbox. Other tabs keep it.

## Deferred

- An active-filter count in the collapsed panel's summary line.
- Keeping focus across re-renders (deferred by #185).

## Verification

- Fail-first source-wiring tests in `desktop/test/inboxFilters.test.ts`:
  - the `.shell` markup carries `data-view="inbox"`;
  - `showView` assigns `dataset.view` from its argument;
  - the 80% rule's selector covers exactly `inbox` and `expiry-ledger`, inside `@media (min-width: 681px)`;
  - the Inbox intro rule uses the clip pattern, not `display: none`;
  - the filter `<details>` has no `open`;
  - the expanded content does not append the sender, address, received time, subject, category, or state, and still appends the provenance, priority, and footer actions;
  - the sender column is `minmax(220px, 300px)`.
- Commands, each a narrowing of a prefix declared in the Codex scope file:
  - `node --test --experimental-strip-types --test-isolation=none desktop/test/*.test.ts`
  - `pnpm --dir desktop build`
- Effect check, reviewer-run: the same stubbed-backend preview used to measure this plan, run on the implemented code at 1524×785. It must show:
  - the Inbox shell at 80% of the viewport;
  - Watchlist at today's width;
  - the intro hidden on Inbox and visible on Settings;
  - the filters collapsed;
  - at least one message row above the fold;
  - an expanded row with no repeated sender, subject, or time.

  The operator confirms in the installed release candidate.

## Estimated diff size

| File | LOC |
|---|---:|
| `desktop/src/main.ts` | 25 |
| `desktop/src/styles.css` | 20 |
| `desktop/test/inboxFilters.test.ts` | 45 |
| **Total** | **90** |

## Codex scope file

```json
{
  "roots": ["<worktree>"],
  "allow": ["desktop/src/main.ts", "desktop/src/styles.css", "desktop/test/inboxFilters.test.ts"],
  "goal": "Wide, compact Inbox (plans/PR-Wide-Inbox.md)",
  "plan": "plans/PR-Wide-Inbox.md",
  "verify": {
    "commands": ["node --test --experimental-strip-types --test-isolation=none", "pnpm --dir desktop build"],
    "max_runs": 5
  },
  "churn": { "max_lines": 140, "max_new_tests": 8 }
}
```
