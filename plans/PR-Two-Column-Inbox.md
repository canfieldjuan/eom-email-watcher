# Two-Column Inbox

## Why this slice exists

The operator approved a two-column Inbox layout (mockup reviewed 2026-10-03): watched senders and "All messages" on the left; expandable messages, attachments, and current action states on the right. Today the Inbox is one filtered list, and the watched senders live on a separate Watchlist tab.

A read-only investigation of `origin/main` at `ac4829f`, plus a reproducible probe, settled the one remaining data question:

- `inbox.query` keyset paging is complete. With 130 messages from one sender (20 sharing one `received_at`), paging at limits 7, 25, and 100 returned all 130, with no duplicates and strictly descending `(received_at, message_id)`.
- `sender_query` is not a sender filter. It is a case-insensitive substring match on the address or the display name (`src/eom_email_watcher/db.py:11475-11480`; `docs/ENGINE_API.md:250`). The probe for `bob@acme.com` also returned 15 rows from `jimbob@acme.com` and 5 rows whose display name contained the address. A watched-sender view built on it would show other senders' mail under that sender's name.

### Problem-derived contract

- Root cause: the read contract has no exact sender filter, and the desktop has no sender navigation.
- Correct fix must touch:
  - add an exact, normalized `sender` filter to `inbox.query` and to every hop that carries it: store, engine API, Rust `InboxQuery`, TypeScript query type, and `docs/ENGINE_API.md`;
  - render the Inbox as two columns, driven by the existing `watchlist_list` command;
  - render each message as a collapsed row that expands to the card content rendered today, with a single action-state chip computed from states the engine already projects.
- Must not change:
  - `sender_query` semantics;
  - cursor format or ordering;
  - schema or indexes;
  - admission, analysis, Connect, automation, or calendar behavior;
  - decision admission or the refresh-required fence;
  - the content and controls of an expanded card;
  - the Watchlist tab;
  - existing filters and page size;
  - "Load more" and "Clear local history".

## Scope (this PR)

Ownership lane: two-column-inbox
Slice phase: operator-visible layout change on existing data

1. Exact sender filter: `inbox.query` gains optional `sender`.
2. Two-column Inbox view with sender navigation.
3. Collapsible message rows with one action-state chip.

### Observable behavior

Engine:

1. `inbox.query` accepts optional `sender`. The engine normalizes it with `normalize_validated_address` (`src/eom_email_watcher/config.py:245`), the same normalization every mail adapter applies to stored `messages.sender`. It returns only rows whose `sender` equals the normalized value. `BOB@Acme.com` matches stored `bob@acme.com`; `jimbob@acme.com` and display-name matches do not.
2. `sender` combines with every other filter by `AND`, including `sender_query`. Paging, ordering, the 100-row limit, and the cursor are unchanged.
3. A `sender` that is not a non-empty string, or that `exact_sender_selector_id` (`src/eom_email_watcher/config.py:263`) rejects, returns `invalid_request`. That is the same check `watchlist.add` applies: an invalid address, or an admission selector over 512 UTF-8 bytes. Every sender the watchlist accepts is therefore queryable, including the 505-character address `tests/test_engine_api.py:1191` accepts. A rejected `sender` never falls back to a substring match or to an unfiltered page. `null` or an absent field means no sender filter.

Desktop:

4. The Inbox view has two columns. The left column lists "All messages" followed by every watched sender from `watchlist_list`, showing each sender's name when present and always its address. The right column holds the existing filter form (all current controls), status line, message list, "Load more", and "Clear local history". Below about 760px the left column stacks above the right; nothing scrolls horizontally.
5. "All messages" is selected on launch and sends no `sender`. Selecting a watched sender sends that sender's address as `sender` and reloads from the first page. The selected item exposes `aria-pressed="true"`, and the list heading shows the selection.
6. A watchlist change (add or remove on the Watchlist tab) refreshes the left column. If the selected sender is removed, the selection returns to "All messages" and the list reloads.
7. Each message renders collapsed with:
   - sender name and address;
   - received time;
   - subject;
   - the existing priority stripe;
   - the category badge;
   - the existing `stateLabel`;
   - the attachment count when there are attachments;
   - at most one action-state chip.

   A toggle `button` with `aria-expanded` and `aria-controls` shows or hides the full card. The expanded region holds exactly what the card renders today, with unchanged controls and behavior.
8. Expansion is held in a module-level set keyed by `message_id`. Rows default to collapsed. Refreshes ("Load more", scheduled check, `watcher://connect-queue`, decision refresh) never collapse an expanded row. The set is not pruned on refresh: the scheduled check reloads only the first page (`desktop/src/main.ts:4584`), so a row reached through "Load more" must come back expanded when it is loaded again. The set is cleared when the query changes (sender selection, filter submit or reset, account change). It loses one id on `inbox.delete` of that message, and is cleared on `inbox.clear`.
9. The action-state chip is a pure function of projected states only:
   - fire `state` (closed set `AUTOMATION_FIRE_STATES`);
   - Connect result `status` (`requested | accepted | processing | completed | failed`);
   - calendar proposal `state`, `status`, and `expires_at`, evaluated against a `now` argument. The renderer passes its `renderStartedAt`, and tests inject a fixed clock.

   A proposal counts as expired by the renderer's own rule (`desktop/src/main.ts:1781-1784`): `status` is `accepted` and `expires_at` is at or before `now`. A proposal whose `status` is not `accepted` has no suggestion, and the renderer labels it "Needs review", so it contributes "Needs review". An expired `awaiting_confirmation` proposal contributes nothing, because it can no longer be confirmed. The renderer's existing expiry timer (`nextProposalExpiry`) re-renders at expiry, so the chip changes then.

   Precedence, highest first:

   | Chip | Triggered by |
   |---|---|
   | "Needs your confirmation" | a fire in `awaiting_confirmation`, or an unexpired proposal with an accepted suggestion in `awaiting_confirmation` |
   | "Action failed" | a fire `failed`, a result `failed`, or a proposal `failed` |
   | "Needs review" | a fire `manual_review` or `source_unavailable`; a proposal `manual_review` or `unresolved`; or a proposal without an accepted suggestion |
   | "Action paused" | a fire `entitlement_paused` |
   | "Action running" | a fire `pending_dispatch` or `submitted`; a result `requested`, `accepted`, or `processing`; or a proposal `write_authorized`, `writing`, or `reconciling` |
   | "Completed" | a fire, result, or proposal `completed` |

   Unknown or malformed values contribute nothing, and `declined` contributes nothing. The chip never reads `reason`, `job_id`, `last_error`, or any provider text.
10. An empty sender view names what actually filtered it. The default query always carries an account scope (`desktop/src/main.ts:985-988`).
    - Any of keyword, free-text sender, priority, topic, or status is set: "No retained messages from this sender match these filters."
    - Otherwise, the account selection is not "All retained accounts": "No retained messages from this sender in this account."
    - Otherwise: "No retained messages from this sender."

    `inboxSenderNav.ts` chooses the text as a pure function. A failed `watchlist_list` shows its error in the left column; "All messages" and the list keep working.

### Invariants

- A sender view never shows a message whose `sender` differs from the selected address.
- A response for a superseded query never renders. The existing `inboxRequestGeneration` / `mailboxEffectRequestIsCurrent` guard applies to sender selection exactly as it does to filter changes.
- Changing the sender selection discards the cursor. A cursor from one query is never sent with another.
- Raw `reason`, `job_id`, and provider error text never reach the new chip or row markup. This holds the #181 position: the reason vocabulary is OPEN.

### Concurrency

- Selection during an in-flight load: the selection bumps the request generation, so the late page is discarded.
- A refresh event while a sender is selected: `refreshLoadedInboxSpan` and the scheduled-check reload use `activeInboxQuery`, which now carries `sender`, so refreshes stay in the selected view.
- The selected sender is removed while its page is loading: the fallback to "All messages" bumps the generation, so the stale page is discarded.
- A row is collapsed while its attachment invocation or automation decision is in flight: the in-flight sets stay keyed by fire id or attachment key, so collapsing hides the controls and does not cancel or duplicate the request.
- Expansion state changes only on user toggle, query change, delete, and clear. No refresh or other async path writes it.

### Failure cases

- `invalid_request` for `sender` (for example a stale or hand-edited watchlist entry): the status line shows the error with `data-kind="error"`, the selection stays, and "All messages" recovers.
- The Rust host does not deserialize `sender`. `InboxQuery` has no `deny_unknown_fields`, so a missing field would be dropped silently and the engine would return every message under the sender's heading. The Rust struct field and the forwarding in `query_inbox` are therefore required, and a test proves `sender` reaches the engine payload.

### Closure Declaration

- Fire states: CLOSED by `AUTOMATION_FIRE_STATES` (`db.py:61`). Unknown values produce no chip.
- Connect result statuses: CLOSED by the `AttachmentCapabilityResult.status` union (`desktop/src/main.ts:81`). Unknown values produce no chip.
- Calendar proposal states: CLOSED by the `proposalStateLabels` keys (`desktop/src/main.ts:1792`). Unknown values produce no chip. Expiry follows the renderer's rule (`status` `accepted` and `expires_at` at or before `now`). An unparseable `expires_at` is treated as not expiring, as the renderer does, and its unknown `status` values mean no suggestion.
- Reason vocabulary: OPEN, as in #181. Not read or rendered.

### Boundary-change enumeration

- New input: `sender` on `inbox.query`. Callers are the desktop through Rust `InboxQuery` and any CLI or host that calls the engine directly.
- Input shapes:
  - absent, `null`, or valid mixed-case → accepted, normalized;
  - the longest address `watchlist.add` accepts (`tests/test_engine_api.py:1191`) → accepted;
  - empty, whitespace, not a string, not an address, or a selector over 512 UTF-8 bytes → `invalid_request`.
- Guard-relevant field: only the normalized `sender` reaches SQL, as a bound parameter.

### Files touched

- `docs/ENGINE_API.md`
- `src/eom_email_watcher/db.py`
- `src/eom_email_watcher/engine_api.py`
- `desktop/src-tauri/src/engine.rs`
- `desktop/src/inboxActionState.ts` (new)
- `desktop/src/inboxSenderNav.ts` (new)
- `desktop/src/main.ts`
- `desktop/src/styles.css`
- `desktop/test/inboxActionState.test.ts` (new)
- `desktop/test/inboxSenderNav.test.ts` (new)
- `desktop/test/inboxFilters.test.ts`
- `tests/test_db.py`
- `tests/test_engine_api.py`

## Mechanism

- Store: `query_inbox` gains `sender: str | None`. When set, it adds the clause `sender = ?`. There is no index change; retained volume is bounded by retention, and ordering still uses `idx_messages_inbox_order`.
- Engine: `_query_inbox` admits `sender` in its payload set. It validates the value with `exact_sender_selector_id` and filters on `normalize_validated_address(sender)`, raising `invalid_request` when validation fails.
- Rust: `InboxQuery.sender: Option<String>`, forwarded as `"sender"`. The typed contract test (`inbox_query_and_page_contract_are_typed`) includes it.
- Desktop:
  - `inboxSenderNav.ts` is a pure selection model: it derives the selection from the watchlist plus the current selection, and handles removal fallback.
  - `inboxActionState.ts` is the pure chip mapper.
  - `main.ts` adds the left-column markup, wires selection into `activeInboxQuery.sender`, and wraps each card's existing content in a collapsible region with the expansion set.
  - `styles.css` adds the two-column layout, row, and chip styles using existing tokens.

## Intentional

- Watched senders only in the left column. Gmail-label-admitted mail from non-watched senders appears under "All messages".
- No per-sender counts: no count API exists, and adding one is a separate backend change.
- No "new mail" banner: the scheduled-check handler already reloads the Inbox (`desktop/src/main.ts:4580-4585`).
- The existing free-text Sender filter stays as a secondary filter and combines by `AND`.

## Deferred

Parking predicate: anything that does not make a watched-sender view exact or the Inbox two-column is parked by default.

- Showing automation `reason` copy: needs a closed reason vocabulary and product approval (#181 Deferred).
- Per-sender counts or unread markers.
- Gmail label selections in the left column.
- Keeping focus across re-renders. `renderInbox` already rebuilds every card; that predates this slice.
- Body-truncation marker (#146) and rule management (#143).

Parked hardening: an index on `messages(sender, received_at, message_id)` if exact-sender queries measure slow at retention scale.

## Verification (settling evidence)

Fail-first: each new behavior test must fail on `ac4829f` before the change. On that commit, `inbox.query` rejects the unknown `sender` field with `invalid_request`.

- Store and engine tests:
  - exact sender across several pages at limit 7 (130 rows, 20 tied timestamps): all returned, no duplicates, strictly descending;
  - superstring address and display-name rows excluded;
  - mixed-case input matches;
  - `sender` AND `sender_query`;
  - each invalid shape rejected;
  - the 505-character address from `tests/test_engine_api.py:1191` accepted, and the 506-character one rejected;
  - `null` equals absent.
- Rust: the typed contract test serializes `sender`, and a `query_inbox` forwarding assertion shows `"sender"` in the engine payload.
- Desktop unit tests:
  - `inboxActionState`: every fire, result, and proposal state; the precedence order; a proposal one millisecond before, exactly at, and after `expires_at` under a fixed `now`; a proposal without an accepted suggestion; an unparseable `expires_at`; and unknown and malformed values;
  - `inboxSenderNav`: select, All, removal fallback, watchlist failure, and each of the three empty-state texts.
- Desktop source wiring:
  - selection sends `sender`, not `sender_query`;
  - selection resets the cursor;
  - the expansion set survives `renderInbox`, including a first-page reload that drops a row loaded by "Load more", and is cleared on a query change;
  - the toggle carries `aria-expanded` and `aria-controls`;
  - the chip mapper receives only state fields.
- Commands, each a narrowing of a prefix declared in the Codex scope file:
  - `uv run --locked pytest -q tests/test_db.py tests/test_engine_api.py -k inbox`
  - `uv run --locked ruff check src/eom_email_watcher/db.py src/eom_email_watcher/engine_api.py tests/test_db.py tests/test_engine_api.py`
  - `node --test --experimental-strip-types desktop/test/*.test.ts`
  - `pnpm --dir desktop build`
  - `cargo test --manifest-path desktop/src-tauri/Cargo.toml --lib inbox_query`
- Not claimed: rendered interaction in the packaged app. The operator's check on a dev build (`pnpm --dir desktop tauri dev`) is a separate acceptance step.

## Estimated diff size

| File | LOC |
|---|---:|
| `docs/ENGINE_API.md` | 6 |
| `src/eom_email_watcher/db.py` | 6 |
| `src/eom_email_watcher/engine_api.py` | 12 |
| `desktop/src-tauri/src/engine.rs` | 10 |
| `desktop/src/inboxActionState.ts` | 70 |
| `desktop/src/inboxSenderNav.ts` | 50 |
| `desktop/src/main.ts` | 140 |
| `desktop/src/styles.css` | 90 |
| `desktop/test/inboxActionState.test.ts` | 110 |
| `desktop/test/inboxSenderNav.test.ts` | 80 |
| `desktop/test/inboxFilters.test.ts` | 30 |
| `tests/test_db.py` | 60 |
| `tests/test_engine_api.py` | 50 |
| **Total** | **714** |

## Codex scope file

The implementing session runs from a worktree of this branch, with `.codex/` listed in that worktree's `.git/info/exclude`. `<worktree>` is that absolute path.

```json
{
  "roots": ["<worktree>"],
  "allow": [
    "docs/ENGINE_API.md",
    "src/eom_email_watcher/db.py",
    "src/eom_email_watcher/engine_api.py",
    "desktop/src-tauri/src/engine.rs",
    "desktop/src/inboxActionState.ts",
    "desktop/src/inboxSenderNav.ts",
    "desktop/src/main.ts",
    "desktop/src/styles.css",
    "desktop/test/inboxActionState.test.ts",
    "desktop/test/inboxSenderNav.test.ts",
    "desktop/test/inboxFilters.test.ts",
    "tests/test_db.py",
    "tests/test_engine_api.py"
  ],
  "goal": "Two-column Inbox: exact sender filter, sender navigation, collapsible rows (plans/PR-Two-Column-Inbox.md)",
  "plan": "plans/PR-Two-Column-Inbox.md",
  "verify": {
    "commands": [
      "uv run --locked pytest -q tests/test_db.py tests/test_engine_api.py",
      "uv run --locked ruff check",
      "node --test --experimental-strip-types",
      "pnpm --dir desktop build",
      "cargo test --manifest-path desktop/src-tauri/Cargo.toml --lib"
    ],
    "max_runs": 14
  },
  "churn": { "max_lines": 800, "max_new_tests": 34 }
}
```

`roots`, `allow`, and `goal` are enforced today by the scope guard (guard 6). `verify` and `churn` take effect only once canfieldjuan/sol-5-6-cicd-lab#28 (step 6) is accepted and implemented. Until then they are inert, and the limits they express are instructions in this plan. Declared commands are prefixes, so narrowing (`-k inbox`, one test file, `inbox_query`) is allowed.

## Review amendments (Codex review of `fbd41e5`)

Four P2 findings were verified against the code and adopted:

1. The 320-character `sender` cap was narrower than the watchlist's 512-byte selector bound. `sender` now uses `watchlist.add`'s own validator.
2. Pruning expansion on refresh would collapse rows loaded through "Load more", because the scheduled check reloads only page one. Expansion is now cleared only by query change, delete, or clear.
3. A state-only mapper could not see calendar proposal expiry. The mapper now takes `status`, `expires_at`, and an injected `now`, using the renderer's own expiry rule.
4. The sender empty-state text ignored active filters and the default account scope. It now has three filter-aware forms.
