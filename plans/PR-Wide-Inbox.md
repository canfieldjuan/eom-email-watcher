# Wide Inbox

## Why this slice exists

After installing the #185 release candidate, the operator reported that the two-column Inbox does not use enough of the screen. In the screenshot, the content spans about 950 screen pixels of a 1905-pixel window. That is 760 CSS pixels at the 1.25× display scale.

### Problem-derived contract

- Root cause, and the factor that actually controls the width: `.shell { width: min(760px, calc(100% - 40px)); }` (`desktop/src/styles.css:39-41`). Every view sits inside that one container (`desktop/src/main.ts:516`). The Inbox's own grid is second: its sender column is capped at `minmax(140px, 190px)` (`desktop/src/styles.css:200-203`), which is why addresses such as `ap@mccarthyimprovement.com` break mid-word.
- The Expiry Ledger has the same cap. Its table needs `min-width: 1160px` and scrolls sideways inside 760px (`desktop/src/styles.css`, about 1289-1298).
- Correct fix must touch:
  - let the shell widen only for the views whose content is wide;
  - widen the Inbox sender column.
- Must not change:
  - Watchlist, Health, or Settings layout: they stay 760px; their forms read better narrow;
  - any card, filter, or behavior;
  - the existing breakpoints at 760px and 680px.

## Scope (this PR)

Ownership lane: wide-inbox
Slice phase: layout correction on an operator-reported defect

### Observable behavior

1. While the Inbox or Expiry Ledger tab is shown, the shell is `min(1440px, calc(100% - 48px))` wide. On a 1536-CSS-pixel window (1920 screen pixels at 1.25×) that is 1440px; on narrower windows it fills the window minus 24px gutters.
2. While Watchlist, Health, or Settings is shown, the shell stays at today's `min(760px, calc(100% - 40px))`.
3. Switching tabs switches the width immediately. The Inbox width also applies on launch, before the config check picks a view. The markup starts with the Inbox marker, because the Inbox is the view visible in the markup.
4. The Inbox sender column becomes `minmax(220px, 300px)`; the message column takes the rest.
5. Below 760px, the two columns still stack. At any width of 320px or more, the page never scrolls horizontally. The Expiry Ledger table keeps its own scroll box, which is only needed below about 1208px of shell width.

### Invariants

- Only `showView` sets the view marker. It is set from the same `view` argument that toggles `hidden` (`desktop/src/main.ts:1190-1200`), so the marker and the visible view cannot disagree.
- No other view's computed layout changes.

### Failure cases

- An unknown view value cannot occur: `showView` takes a closed union. If the marker were ever missing, the shell falls back to today's 760px. The fallback is the old layout, never a broken one.

### Files touched

- `desktop/src/main.ts`: the shell marker in the markup, and one assignment in `showView`.
- `desktop/src/styles.css`: the wide-shell rule for the two views, and the sender column size.
- `desktop/test/inboxFilters.test.ts`: source-wiring tests.

## Mechanism

- Add `data-view="inbox"` to the `.shell` element (`desktop/src/main.ts:516`).
- In `showView`, set the shell element's `dataset.view` to the `view` argument, next to the existing `hidden` toggles.
- CSS: inside `@media (min-width: 681px)`, add `.shell[data-view="inbox"], .shell[data-view="expiry-ledger"] { width: min(1440px, calc(100% - 48px)); }`. The media query is required, not optional. The attribute selector outranks the plain `.shell` in the existing `@media (max-width: 680px)` rule (`desktop/src/styles.css:1354-1357`), so outside a `min-width` query it would override phone widths whatever its position in the file. Also change `.inbox-columns` to `minmax(220px, 300px) minmax(0, 1fr)`.

## Intentional

- 1440px is a cap, not full bleed, so lines stay readable on wide monitors.
- Watchlist, Health, and Settings stay narrow.

## Deferred

- Collapsing the Inbox filter panel by default to save vertical space. It is not reported, and it is a product choice.

## Verification

- Fail-first source-wiring tests:
  - the `.shell` markup carries `data-view="inbox"`;
  - `showView` assigns the shell's `dataset.view` from its argument;
  - `styles.css` has the wide rule for exactly `inbox` and `expiry-ledger`;
  - the wide rule sits inside `@media (min-width: 681px)`;
  - the sender column is `minmax(220px, 300px)`.
- Commands, each a narrowing of a prefix declared in the Codex scope file:
  - `node --test --experimental-strip-types --test-isolation=none desktop/test/*.test.ts`
  - `pnpm --dir desktop build`
- Effect check: the controlling factor is the `.shell` width, which no ancestor overrides (`body` and `#app` add no max-width). The operator confirms the effect visually in `pnpm --dir desktop tauri dev` or a new release candidate.

## Estimated diff size

| File | LOC |
|---|---:|
| `desktop/src/main.ts` | 3 |
| `desktop/src/styles.css` | 6 |
| `desktop/test/inboxFilters.test.ts` | 20 |
| **Total** | **29** |

## Codex scope file

```json
{
  "roots": ["<worktree>"],
  "allow": ["desktop/src/main.ts", "desktop/src/styles.css", "desktop/test/inboxFilters.test.ts"],
  "goal": "Wide Inbox and Expiry Ledger shell (plans/PR-Wide-Inbox.md)",
  "plan": "plans/PR-Wide-Inbox.md",
  "verify": {
    "commands": ["node --test --experimental-strip-types --test-isolation=none", "pnpm --dir desktop build"],
    "max_runs": 4
  },
  "churn": { "max_lines": 60, "max_new_tests": 4 }
}
```
