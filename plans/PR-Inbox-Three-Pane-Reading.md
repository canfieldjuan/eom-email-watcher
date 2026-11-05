# Inbox: three panes and contained reading

Status: **deferred three-pane design reference — not an Inbox-link prerequisite or implementation authorization**.
Baseline: `main` at `3ed72aec73672a4334f5fcd949b7c60198d40ef8`.
Juan requested a searchable watched-sender list, an exact-sender message list with an
all-senders option, and a selected message or thread in a reading pane.

On 2026-11-05 Juan said “Let’s get started with our next slice.” This session interpreted
that as permission for P1/P2, implemented locally at
`e39c32d39439481fa19f4b6847d8ec4f4b94c906`. It was not an explicit choice of a
message-details-first reader or exact-revision acceptance of the entire plan.
Juan subsequently chose “We should connect the full thread first.” Oversight note
`2472ea66` now relays the more precise sequencing decision: **“Link first.”** It also
relays **“Open its thread too”** for non-vendor mail. The
[Inbox-link contract](PR-Inbox-Thread-Entry.md) and its explicit thread-definition
amendment cover every valid retained Inbox conversation in the current layout first.

**This three-pane plan is deferred.** Its P1/P2 commit stays frozen, unsubmitted and
outside PR #343's application scope. The Inbox link must work from main without it.
After that link is reviewed and accepted, this design needs its own later review and
acceptance before further redesign code. Accepting PR #343 does not authorize the
three-pane implementation. Merge still needs Juan's approval for each PR; no build,
package, install, deployment or live app/settings change is authorized.

## Root cause and current capability

The Inbox renders every watched address in an unbounded navigation column and every
loaded message as a card whose details expand in that same list. Neither column has
a viewport-height scrolling boundary. Search/filter controls sit inside a disclosure,
so applied scope is easy to miss while browsing. The issue is navigation, density and
separation of reading from finding, rather than missing exact-sender filtering.

| Current source owner at the baseline | What exists; what does not |
| --- | --- |
| `desktop/src/main.ts`: Inbox markup, `renderInboxSenderNavigation`, `selectInboxSender`; `inboxSenderNav.ts` | Full watched addresses and exact selection; no sender-list search. “All messages” includes all retained matching admissions, not just watched senders. |
| `main.ts`: `InboxQuery`, `queryFromInboxControls`, `commitInboxQueryFromControls`; [Engine API](../docs/ENGINE_API.md) | Exact normalized `sender` is distinct from substring `sender_query`. Subject/summary keyword, priority, topic, status, account and page size already exist. All predicates combine with AND. |
| `main.ts`: `InboxItem`, `renderInbox` | Saved analysis, provenance, attachments, action/confirmation/calendar state and existing controls. No email body, complete `ThreadRef` or selected vendor is returned in an Inbox row. |
| `desktop/src/styles.css`: `.shell`, `.inbox-columns`, `.inbox-list`, `.inbox-card` | Wide 80% Inbox shell, two natural-height columns, individually expandable cards; no three-pane or single-reader selection model. |
| `threadReads.ts`, `threadView.ts`, `threadViewDom.ts` | M3 reads retained vendor-related threads with complete scoped identity, oldest-first messages, explicit local body expansion and copy inventory. Entry today is Vendors → View threads. It is not a general reader for arbitrary Inbox rows. |
| `main.ts`: `loadInboxPage`, `runInboxRefresh`; `inboxReload.ts` | Request generation, query epoch, account-effect checks, coalesced first-page/span refresh and explicit Load more. These remain the data owners. |

## Proposed experience — later, separate redesign

```text
Inbox                 [Search subject or summary…] [Search] [Filters]
Account: Active — …    [Priority: High ×] [Topic: Invoice ×]
┌ Watched senders ─────┬ Matching messages ────────┬ Retained thread ─────────┐
│ [Find sender…]       │ sender · time            │ thread subject          │
│ All senders          │ subject                  │ timeline / stored bodies│
│ name                 │ state · attachments      │ Selected message details│
│ full address         │                          │ Existing actions        │
│ … own scroll …       │ … own scroll …           │ … own scroll …          │
│                     │ [Load more]              │                         │
└─────────────────────┴──────────────────────────┴─────────────────────────┘
```

The drawing describes layout, not new data. Reuse the current neutral/green palette,
type and state vocabulary. Use pane separators and one clear selected-row treatment;
avoid another layer of large cards or decorative status badges. Subject and sender
lead each row; time and existing state indicators remain readable. Keep the existing
priority, category, status, attachment count, partial-summary and single action-state
signals. Do not invent unread markers, sender totals, thread counts or full-body search.

### P1 — watched-sender navigation

- Add a separately labelled **Find watched sender** input. It filters the already
  loaded watchlist by case-insensitive literal name/address text, preserving its order.
  This is local navigation search: it changes neither `sender_query` nor the Inbox query,
  account, cursor or reader. Clearing it restores the list; it never edits the watchlist.
- Keep **All senders** reachable above the search results. This is a proposed rename
  of **All messages**, still `sender: null`, with helper text “All retained messages
  in the selected account scope.” It must retain label-admitted mail and captured Sent
  replies; it is not a union restricted to watched addresses.
- Keep the selected sender pinned visibly when the finder would otherwise hide it.
  Names supplement full addresses; wrapping is allowed, tooltip-only addresses and
  permanent ellipsis are not. The full list remains reachable by clearing the finder
  and scrolling. No alphabetic regrouping or sender matching by display name/domain.
- Preserve `aria-pressed`, removal fallback to All, and watchlist-error behavior.
  A failed watchlist refresh preserves the selection; no finder matches is a different
  state from no retained messages or a failed watchlist load.

### P2 — compact search and truthful filters

- Move **Subject or summary** keyword search to the compact toolbar, with an explicit
  Search/Enter submit. It keeps today's literal case-insensitive search and 200-character
  UI bound. No live query-on-keystroke, body search or new network work.
- **Filters** opens a compact disclosure containing Sender contains (`sender_query`,
  existing 320-character UI bound), account, priority, topic, status and page size.
  Preserve current values, defaults, Apply and Reset behavior; clarify only the old
  “Sender” input label to “Sender contains” so it cannot be mistaken for exact selection.
  Search and Apply submit the same draft controls through the same query owner.
- Show committed keyword/sender-contains/priority/topic/status as removable labels.
  Removing one clears only that committed criterion and its matching draft input;
  it neither commits other unsubmitted drafts nor clears exact sender selection.
  Closing Filters preserves drafts. Labels represent committed queries, never drafts.
- Account scope is always visible, including the default Active account. Removing a
  non-default account label returns to Active account, not All retained accounts.
  The Active-account label opens the account control and has no remove button. Page
  size remains visible as “25 per page” (or the chosen size), not a search predicate.
- Reset still restores the form defaults, including Active account and 25 rows, while
  retaining the exact sender. All senders changes only `sender`; it does not reset
  secondary criteria. Preserve account reconciliation, missing-active-account refusal,
  retained-account choices, source attribution and account-operation revisions.
- This explicitly replaces [Wide Inbox](PR-Wide-Inbox.md)'s earlier static filter
  summary choice. It does not change what a filter matches.

### P3 — message list and one reading pane

**Deferred integration:** once the current-Inbox link exists, reuse its accepted
vendor/non-vendor entry and shared reader in this pane. This section proposes only
the later layout/selection changes; it does not move those changes into the link's
scope or redefine its read selectors and body states.
- Replace inline multi-card expansion with one selected-thread reading pane. Start
  with “Select a message to view its retained thread”; do not auto-select or invoke an
  action. Selection is an explicit native button action, keyed by the new complete
  Inbox message reference and committed query epoch, never by row position or subject.
- Show the originating message's subject, full sender, time and provenance alongside
  the current thread heading/timeline. Keep existing expanded-card content in the
  secondary Selected message details section: saved analysis, truncation notice,
  attachments, capability results, confirmation/calendar state, retry and local
  deletion. A summary is never labelled “Email body.”
- Keep one owner for existing action handlers, expiry timers, in-flight state and
  decision-refresh fences. Row chips must still update for unselected messages.
  Changing selection hides controls but does not cancel, repeat or acknowledge an
  in-flight decision. Its late completion may update its own state, never the newly
  selected message's reader. Merely selecting a row adds no mailbox/provider/model call;
  existing Inbox capability refresh and explicit action behavior remain unchanged.
- Load more preserves selection and scroll, appends the existing cursor page and does
  not open the appended row. Preserve descending `(received_at, message_id)` ordering,
  10/25/50/100 page sizes, deduplication and the existing first-page/span refresh owner.
  Keep Load more at the end of the message pane; do not substitute infinite scrolling.
- Query commits, sender/account changes and accepted deletion/clear invalidate the
  reader before any new selection can render. Preserve existing list behavior: scope
  changes clear rows; secondary-filter replacement/failure may retain previous rows.
  Retained previous-query rows get a “Previous results” annotation and cannot select a
  reader under the new query until a current page commits. Keep existing status/error
  wording and retry policy; do not clear the underlying retained list as a workaround.
- During same-query revalidation retain the selected identifier, but clear stale
  actionable reader content until a current projection returns it. A failed refresh
  leaves a retry state, not old actionable details. If the originating identifier is
  absent from the loaded Inbox page/span, show “Selected message is not in the loaded results”; do not
  claim deletion, auto-page to find it or switch to another message. It may reappear
  when that same query loads it again. Query changes clear that remembered identifier.
- Preserve completed/deferred/refused removal outcomes and persistent pending-removal
  notices. Hide accepted logical targets immediately even while bytes remain. Keep
  Clear local history and its existing confirmation/scope, visibly outside the reader
  as a history-wide action; selected sender/filter scope must not silently narrow it.

### P4 — contained scrolling and accessible narrow layouts

- On a wide window use three panes inside the remaining viewport below navigation
  and toolbar. Each pane has its own vertical overflow region; adding senders, loading
  pages or opening long details does not grow the outer document indefinitely.
  Headers/search stay reachable, with no nested scroll box for each message card.
- Proposed starting proportions: about 16rem for senders, 22rem for messages and a
  reader of at least 28rem, with the reader taking spare room. Use available content
  width and font-relative minima to choose layout, rather than squeezing three panes.
  Inbox alone may use the available window with modest gutters instead of its old 80%
  rule. Expiry Ledger and all other tabs retain their existing layout.
- When three panes do not fit, offer Senders as a labelled panel with its own bounded
  list and keep messages beside the reader if they fit. When two panes do not fit,
  show one of Senders, Messages or Details with explicit Back controls and the current
  sender/account context. Preserve selection, filters and list scroll on transitions.
  Hidden panes are absent from tab order; breakpoint changes do not change the query.
- Support the current 640×520 minimum window, the existing 320-CSS-pixel responsive
  surface and 200% text/zoom. At very short heights allow controls to scroll in their
  containing view instead of clipping them; list growth still belongs to bounded panes.
  No document-wide horizontal scroll; long addresses, subjects and body text wrap.
- Native buttons/inputs, visible focus, named navigation/list/detail regions, semantic
  headings and explicit selected state. No listbox containing interactive action buttons.
  Keyboard activation moves focus to the reader heading; Back restores the trigger or
  list heading if it disappeared. Search/chip removal preserves sensible focus and
  announces results/loading/errors politely, without announcing every row on refresh.
  Scroll regions must be keyboard reachable and never trap focus. State is not color-only.

## Thread integration decision and safety

Juan chose the Inbox link before this redesign. The current-layout link and explicit
vendor-independent extension are defined in
[PR-Inbox-Thread-Entry.md](PR-Inbox-Thread-Entry.md) and the marked amendments in the
thread contract. This later pane consumes those accepted owners; it must not implement
a competing resolver, fake vendor or extra body-state policy. The Vendors reader
remains available throughout.
That connection must reuse M3's state/read/render owners rather than build a second
body reader. Preserve [THREAD_VIEW_CONTRACT](../docs/THREAD_VIEW_CONTRACT.md) D-scope,
D-identity, D-body, D-ops and D-removal, plus [M3's plan](PR-Thread-M3-Read-View-CSP.md):
plaintext-only rendering, restrictive production CSP, local read class, removal before
body access, exact no-text states, fresh newest-subject heading, oldest-first paging,
partial-history/Sent notices, stale-cursor restart, generation fences, and invalidation
on account/vendor changes, removal, hiding and return/focus. Never keep a hidden body
or let a late response reopen it. Inbox filters constrain the selected message list;
the proposed thread view explicitly shows the full eligible retained thread, including
other senders, and must label that distinction rather than silently filter its timeline.

## Required change surface and explicit non-scope

Current review edit: two plans and the explicit thread-contract amendment. Later redesign UI owners include:
`desktop/src/main.ts`, `styles.css`, `inboxSenderNav.ts`, and a small Inbox selection/
detail-render module if extraction is needed. Move existing detail rendering without
duplicating action decisions. Preserve `inboxReload.ts`, account/removal/decision owners
and their semantics. Update the relevant Inbox DOM/state tests and old layout assertions
only for the expressly accepted layout changes.

P1/P2 remains UI-only and deferred. P3 later consumes the already reviewed Python,
Rust and M3 read changes from the current-Inbox link. Its explicit definition amendment
owns the new no-text read state; this redesign proposes no additional body-policy change.
Neither work changes schema, dependencies, capture, retention, entitlement, automation,
notification, calendar or watchlist management. No provider fetch on
reading, HTML email rendering, reply/compose, bulk action, live mailbox probe, new theme or generalized component rewrite.

Explicit amendments proposed to older UI plans: bounded/searchable sender navigation;
All senders wording; one selected reader instead of retained multi-expansion; visible
committed filter labels; Inbox-only width/breakpoint/pane navigation; reader revalidation
and focus behavior. All other filter, account, paging and sender semantics are preserved.

## Assumptions and local conflict check

- All 19 existing worktrees registered by this session's clone and the original checkout
  were inspected. Eighteen were clean. The original checkout retains seven modified
  benchmark/model-related tracked files plus benchmark/browser artifacts; none was touched.
- At the refreshed checkpoint the **Email Watcher** session is idle after Local Connect
  Status work in another repository. Its clean M3 packaging worktree is also untouched.
  This is an observed checkpoint, not an instruction to pause or alter that session.
- Open EW PRs #314 (model schema), #311 (Windows admission diagnostic) and #237
  (inspection contract status) do not overlap these plan documents or the local P1/P2
  files. The newly proposed thread bridge overlaps #311's `desktop/src-tauri/src/lib.rs`;
  recheck that work before implementing the host slice.
- The original draft is preserved at `27548b5` in `email-watcher-inbox-plan`.
  The review branch `codex/inbox-thread-entry-review` is in a separate owned worktree,
  `email-watcher-inbox-thread-review`, based directly on main and containing these
  two plan documents and the explicit thread-contract amendment. The original
  plan/navigation branches are unchanged. Recheck
  live work, main and overlapping hunks before implementation.
- The relayed three-pane direction is the visual reference available in this session;
  no claim is made that a pixel comparison against the earlier mockup was performed.
- Juan chose a usable Inbox link first, including non-vendor conversations. Review
  the amended link contract now. This layout remains a separate later concern; the
  earlier local P1/P2 implementation does not make it a link prerequisite.

## Verification plan

For this draft: inspect current source/contracts, worktree/PR overlap, Markdown links,
document-only diff and whitespace. No application tests or builds are needed to validate
an unimplemented design; the following are future acceptance evidence, not claimed results.

P1/P2 is already local but deferred. First complete the separately reviewed link in
main's existing Inbox, with its own tests. Later review/accept this redesign and carry
forward P1/P2 deliberately, then integrate the existing thread reader into contained
panes. This table describes future redesign qualification, not acceptance evidence for
the current-Inbox link.

| Boundary | Required proof before implementation is complete |
| --- | --- |
| Sender/query | Same-name/different-address and `bob`/`jimbob` twins; finder does not query mail; selected row survives finder exclusion/error; removal falls back to All; label-admitted and Sent rows remain in All. |
| Filters/accounts | Every existing predicate/default/AND combination, keyword and sender-contains separation, draft vs committed chips, one-chip removal, Reset retaining sender, Active vs explicit vs All accounts, absent active account and page sizes. |
| Paging/refresh | Equal-date rows, multiple pages without omission/duplicates, Load more preservation, blocked first/span refresh, both stale-response orders, filter failures retaining labelled previous results, selection beyond page one, account switch in flight. |
| Reader/actions | No default selection; select A then B while A's result returns; unselected chip/expiry updates; confirmation and output-preview state; duplicate-click protection, decision-refresh acknowledgement and deferred/refused/completed delete/clear unchanged. |
| Layout/a11y | Synthetic 200+ watched senders and long addresses, >100 retained messages, long analysis/attachments; wide 1524×785 and 1920×1080, 1024×768, 640×520, 320 CSS px and 200% text/zoom. Assert bounded pane/document geometry, accessible Load more/filter controls, keyboard scroll, focus return and no selection/query change on resize. |
| Existing M3 | Existing thread state/DOM/integration and removal/CSP evidence remain valid; no CSP change. The proposed thread embedding needs new native proof at its combined reviewed revision. |

Use behavioral DOM/state assertions that fail before the new behavior, not only source
string checks. Run affected Inbox and adjacent desktop tests, typecheck/build and an
isolated synthetic native UI check after code exists. Never load live settings or mail
to produce screenshots. Record platform/viewport gaps honestly; do not weaken guards.

## Implementation summary and cold diff audit

This document remains a later design reference. The current PR instead amends the
thread definitions and plans the link in today's Inbox. The earlier navigation/filter
code at `e39c32d` is excluded and unchanged. This update removes its dependency on the
link's delivery order without altering the proposed filter/layout safeguards.

## Gap audit

DONE for recording the deferral; NOT DONE for redesign acceptance/implementation.
Current-Inbox link acceptance does not authorize further work on these panes. CI is
not triggered because it builds installers; no merge/build/install is authorized.
