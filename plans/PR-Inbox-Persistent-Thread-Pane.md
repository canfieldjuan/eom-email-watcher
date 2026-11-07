# Inbox: keep the retained thread beside the message list

Status: **docs-only proposal for review; oversight review and Juan's exact-revision
acceptance are pending**. This document does not authorize application code,
another PR's merge, local builds, installation or deployment.

Juan authorized submitting ready work to oversight without a separate permission
request. This review submission does not replace his acceptance of the final
reviewed plan or his approval to merge this PR.

Baseline: `main` at `ec0909560703304821ac8fd10ab31d5260d555f3`, the verified
PR #350 merge. PRs #343–347 delivered the Inbox thread connection; #348's
accepted plan and #349–350 delivered sender navigation and compact filters.

References: [navigation and filters](PR-Inbox-Navigation-Filters.md),
[Inbox thread entry](PR-Inbox-Thread-Entry.md),
[deferred three-pane direction](PR-Inbox-Three-Pane-Reading.md),
[Two-Column Inbox](PR-Two-Column-Inbox.md), [Wide Inbox](PR-Wide-Inbox.md),
and [Thread View Contract](../docs/THREAD_VIEW_CONTRACT.md).

## Root cause

Finding messages is now improved, but opening a thread still replaces the Inbox.
`InboxThreadEntry.open` and its reader-state callback set `columns.hidden` while
an origin is selected. In `main.ts`, `#inbox-thread-view` is a sibling after
`#inbox-columns`. The message column also grows with loaded rows and expanded
details. Users must go Back before comparing another message or changing scope.

The existing scoped reader already solves identity, vendor choice, retained body
access and stale-response fencing. This proposal changes its placement and the
Inbox's scrolling/navigation, not its read contract.

The next vertical slice is intentionally limited to **read a full retained thread
while keeping the message list available where space permits**. Saved analysis
and existing actions remain in their current message rows. Migrating those
controls into the reader and replacing row expansion require a later proposal.

## Required change surface

### Observable experience

```text
Inbox   [Subject or summary........] [Search] [Filters]
Account: Active — ...   [Priority: High ×]   25 per page
┌ Watched senders ─────┬ Matching messages ─────┬ Retained thread ─────────┐
│ Find watched sender │ sender · received     │ source message context   │
│ All senders         │ subject / state       │ newest thread subject    │
│ name + full address │ [Open thread]         │ retained timeline        │
│ ... bounded list ...│ existing details      │ explicit body expansion  │
│                     │ ... bounded list ...  │ ... bounded reading ...  │
│                     │ Load more             │ Back to Inbox            │
└─────────────────────┴───────────────────────┴──────────────────────────┘
Clear local history — retains its existing history-wide scope and confirmation
```

The diagram describes the wide layout, not new data or a pixel specification.
Reuse the existing palette, type and wording, with pane separators and one clear
selected-origin treatment. Do not add unread markers, thread counts or new status
vocabulary. Keep message subject, full sender address, received time and the
existing category/state/attachment/partial-summary/action indicators available.

### R1 — one existing reader, explicit selection

- The wide reader area is present from entry, with “Select a message to view its
  retained thread” until the user activates a current row's **Open thread** button.
  Do not auto-select the first row, open on hover, or change the row-details toggle.
- Reuse the existing Inbox-origin `InboxThreadEntry`, `ThreadReadClient`,
  `ThreadViewState` and `ThreadViewDom`. Put the reader inside a stable pane wrapper
  that owns the empty state and scroll; do not keep a second reader or duplicate
  body/copy state. The existing Vendors reader remains independent.
- A selected origin is the complete provider/account/mailbox-identity/message
  reference bound to the committed query epoch and mailbox-operation scope.
  Never key it by row index, subject, sender, native thread ID alone or active
  account fallback. Selecting another eligible row replaces the origin and
  invalidates all previous reader requests before rendering the new selection.
- Mark the chosen row visibly and accessibly; use native button selected-state
  semantics, not a listbox containing the row's other interactive controls.
  Selecting an already open current origin keeps its existing view and focuses
  the reader; it does not repeat reads or expand bodies. Explicit Refresh/Retry
  retain their existing separate meanings.
- Show a small plain-text source-message context: its subject, full sender,
  received time and source account. It is drawn only from the currently eligible
  Inbox row and cleared when that binding becomes unavailable. Keep it distinct
  from the thread heading, which continues to use the newest subject.
- Preserve zero/one/many vendor resolution, vendor chooser paging and explicit
  fresh-origin Retry. Non-vendor mail still opens its full eligible retained
  thread. Do not infer a vendor from the selected sender or duplicate resolution.
- Keep the notice that thread history includes other senders and messages outside
  the Inbox filters, the partial-history/Sent notices, oldest-first timeline,
  origin marker, explicit body/copy expansion and every existing no-text state.
  Reading does not fetch a provider body, run analysis or execute an action.

### R2 — bounded panes and shared controls

- On a wide window, sender navigation, matching messages and retained thread are
  simultaneously usable. The Inbox alone may use the available width with modest
  gutters instead of its previous 80% rule. Other tabs, including Expiry Ledger,
  keep their existing width and behavior.
- Move the existing Search/Filters controls and applied scope above the pane grid.
  Preserve their native form associations, input bounds, draft/committed behavior,
  account resolution, busy state and focus rules. Do not duplicate a hidden form
  or turn the sender finder into message search.
- Give the pane grid the remaining viewport height when usable. Sender results,
  message results and the reading wrapper have independent vertical scroll owners;
  increasing sender count, loading messages or reading long content must not make
  the outer page grow indefinitely. Use shrinkable grid/flex children and wrapping
  text; do not clip overflow to conceal a sizing defect.
- Keep sender finder, All senders and selected-sender pinning reachable, using
  #349's existing owners. Full addresses remain visible, with wrapping rather
  than permanent ellipsis or tooltip-only access.
- Keep explicit **Load more** at the end of the message scroll region. Existing
  inline expanded details belong to that region; add no new per-card scroll box.
  The shared reader's existing body-text size/scroll protection remains intact.
- Keep Inbox loading/error/Previous results and durable removal notices visible
  outside individual rows. Keep **Clear local history** outside the reader with
  its existing history-wide confirmation; selected sender/filter/thread must
  never narrow that operation.
- At short heights or when expanded filters/200% text consume the available
  height, permit the enclosing controls/view to scroll rather than making panes
  negative-sized or hiding controls. List/content scroll regions remain bounded.
  No fixed minimum height may force clipping.

### R3 — narrow windows, focus and Back

- Use available Inbox content width rather than squeezing three panes. Proposed
  starting thresholds are 72rem for three panes and 52rem for messages plus reader,
  with approximately 16rem senders, 22rem messages and at least 28rem reading space
  where all three fit. These are layout starting values, subject to the measured
  acceptance sizes below; changing them must preserve the stated behavior. Use
  container queries, or a ResizeObserver at the existing entry owner when visibility
  and focus need coordination. Viewport media queries alone cannot measure this
  content-width boundary.
- Below the three-pane threshold, put the existing sender navigation in a labelled
  **Senders** disclosure above the messages, retaining its finder, full-address
  results and current-selection context. This does not create another sender
  model. At the middle width the thread stays beside the messages.
- Below the two-pane threshold, show Messages when no thread is selected and the
  retained thread when one is selected. **Back to Inbox** closes/invalidate-clears
  the reader and restores Messages. The sender disclosure is available there.
  Opening it alone never changes the query or starts a thread read.
- Keep the existing Back to Inbox action at every width. On wider layouts it
  clears the reader to its placeholder and returns focus to the selected origin's
  enabled Open thread button, with the message heading as fallback. No offscreen
  origin triggers automatic paging.
- On wide layouts, users may scroll messages while a thread is open: Back must
  keep that latest list position, not reset to the scroll captured before opening.
  On narrow layouts, preserve the positions when Messages becomes hidden and
  restore them on Back. Re-renders, Load more and breakpoint changes preserve
  current sender/message scroll, subject only to normal clamping after shorter
  content. Do not scroll to a newly appended row.
- When resizing with a selected reader, keep that reader visible in narrow mode;
  do not close/reopen or refetch it just because the breakpoint changed. When no
  selection exists, narrowing shows Messages. Move focus only if its current pane
  becomes hidden, to a visible heading/control; ordinary resize/background render
  must not steal focus. Hidden panes are absent from keyboard and accessibility
  navigation. An actually hidden/closed reader must contain no retained body/copy
  bytes and must reject late completions.
- Explicit row activation focuses the reader heading. Preserve focus inside body,
  copy and vendor-choice controls across their existing renders. Retain labelled
  regions, semantic headings, visible focus and polite loading/error announcements.
  Scroll regions are keyboard reachable and do not trap focus.

### R4 — preserve guards and restore reading after successful revalidation

- Preserve exact-sender filtering, All senders semantics including non-watched
  admissions/Sent replies, account modes and the missing-Active refusal, every
  secondary predicate, defaults, page sizes, ordering, deduplication and cursors.
  Finder edits and disclosure toggles change neither query nor reader.
- A real query/sender/account commit clears the old reader through the existing
  owner before a new selection can render. Previous-query rows may remain visible
  with Previous results but cannot open a reader. Loading/failure must not cause
  automatic selection or clear retained rows as a workaround.
- Same-query refresh still invalidates body, copy, inventory and source-context
  payloads immediately. Before that clear, keep only bounded presentation intent:
  the complete origin, query/selection generation, resolved thread/context identity,
  explicitly expanded body message IDs, previously loaded thread-span size,
  reader/body scroll offsets and reader focus key. Retain no body/copy/summary bytes,
  old DOM nodes or old cursors. This intent is not authorization for a read.
- After the complete current Inbox span revalidates the **same complete origin**,
  resolve through the existing guarded owner and require the **same retained thread
  and read context** before restoring anything. Focus return and connect-queue
  refresh must use the existing loaded-span owner while a reader is selected, so a
  first page alone cannot revive or discard an origin on a later loaded page.
  Visibility return must pass the same revalidation gate before restoration.
- Revalidate at most the thread span the user already loaded, using fresh guarded
  pages; do not hunt beyond that span for missing messages or fetch additional Inbox
  pages. For each previously expanded body still present and eligible in that fresh
  span, re-read through the existing guarded body owner. Restore its expansion only
  from the fresh response, including a current no-text state where applicable.
  Previously unopened bodies stay closed. Copy/inventory payloads also remain
  cleared; their existing explicit opening controls are preserved.
- Restore the reader and restored body's scroll offsets after fresh content renders,
  clamped to the current extent. Keep focus on the corresponding reader control,
  or the reader heading if it no longer exists. Do not steal focus if the user moved
  elsewhere while refresh was pending. Successive invalidations in one overlapping
  refresh sequence must not overwrite the original intent with the cleared view;
  only the latest current refresh may restore, without duplicating body reads.
- On failure, origin absence, changed origin/thread/context, query change, explicit
  A-to-B selection, Back, tab close or mutation hold, discard restoration intent and
  keep old content cleared. No late refresh/body completion may restore A over B.
  Absence from the committed loaded span remains unavailable, not a deletion claim
  or automatic replacement selection. Preserve explicit fresh-origin Retry; do not
  resurrect bytes or silently substitute a changed thread. These continuity rules
  apply to window focus, connect-queue and document-visibility return, not resize.
- User Load more appends through the existing owner and does not reopen the
  selection or refresh the thread. Account identity/catalog/vendor/watchlist and
  configuration changes, tab hiding, focus/resume and nested mutation holds retain
  their existing lifecycle dispatch. No competing lifecycle/query owner is added.
- Removal barriers remain authoritative. Begin-mutation invalidation clears both
  readers before writes; pending/completed/deferred/refused outcomes and durable
  notices remain truthful. No physically retained body bytes become visible through
  the pane, saved details or a stale callback.
- Row-details expansion, saved analysis, attachment previews, capability actions,
  automation/calendar confirmation, Retry analysis, Delete locally, expiry timers
  and decision-refresh fences remain with their existing handlers and targets.
  Merely selecting or closing a thread neither invokes nor acknowledges them.
  Unselected row indicators still update. An in-flight action for A never acts on
  selected B or makes B inherit A's result. No action controls move in this slice.

### Source owners and staged implementation after acceptance

| Stage | Change and boundary |
| --- | --- |
| Plan/review | This one new plan only. Review at its exact revision and obtain Juan's acceptance before application code. |
| First vertical UI slice | `main.ts` pane wrapper/markup/source context and selected-origin rendering, `inboxThreadEntry.ts` presentation/return-scroll behavior, Inbox-scoped `styles.css`, and `threadHistory.ts`/`threadView.ts`/`threadViewDom.ts` for bounded refresh intent and fresh guarded restoration. Preserve the single refresh/read owners; deliver explicit thread opening beside messages plus usable narrow fallback together. |
| Verification and review | Extend actual-owner `inboxThreadEntry`, `threadIntegration`, sender-navigation and filter DOM tests; synthetic source-only browser geometry and keyboard checks. Each published UI PR goes to oversight and connector review; GitHub CI runs. Each merge needs Juan's approval for that PR. |
| Later, separate | Moving selected-message details/actions into the reader and retiring multi-row expansion require their own owner audit and accepted plan. The nonblocking #349–350 test/cosmetic follow-ups remain tracked separately; this plan does not silently implement them. |

Keep presentation decisions in the existing entry integration or a small pure
layout helper if necessary. No standalone replacement Inbox controller is proposed.
If the first UI diff exceeds about 1,000 lines including tests, split reviewable
stages: bounded scroll owners with the current entry behavior first, then pane
placement, continuity and narrow fallback. Every shipped stage must keep a working
narrow layout; do not split by shipping a wide-only reader first.
If inspection shows read, action or lifecycle semantics must change to make the pane
safe, stop and amend the plan for review/acceptance rather than widening this slice.

## Explicit non-scope

This proposal supersedes only the Inbox whole-view replacement on Open thread and
Back, Inbox width/breakpoints, control placement, message/reader scroll geometry
and restoration of explicitly expanded bodies after fresh same-origin/thread reads
of the linked older plans. It does **not** accept the deferred three-pane plan's
selected-action migration or removal of multi-row expansion.

No Python/Rust/API/schema/storage/capture/retention/body-state/dependency/CSP/workflow
change. No new provider/model requests, body search, reply/compose, bulk actions,
unbounded or unsolicited Inbox paging, HTML/Markdown email renderer, remote image, attachment download,
watchlist-management redesign, theme rewrite or live settings/mailbox access.
The bounded revalidation of a previously user-loaded thread span is defined in R4;
it does not introduce infinite scrolling or a search for absent origins/messages.
No modification or cherry-pick of the frozen local, unpublished `e39c32d` navigation
implementation; that reference is not a commit available on GitHub.

## Assumptions/blockers

Juan requested continuation after verifying #350. That merge is confirmed from
GitHub, oversight mail `dfaa5e5b` and fetched main: parents `2383ee8` + `af491eb`,
tree `e66c9d539b87b931d73b39e5817b3edcd6cf5f3c`, equal to the reviewed head tree.
All four PR CI jobs passed. The completed merge automation has been disabled.

The source check inspected 34 registered/previously recorded worktrees. All 26
previously recorded heads/statuses were unchanged; eight additional owned/review
worktrees were clean. The original benchmark/model checkout remains dirty and
untouched. Open PRs #314 (model), #311 (native admission diagnostic) and #237
(attachment-contract status) have no path overlap with this plan or proposed
TypeScript/CSS changes. No open Inbox pane PR was found.

This proposal uses an isolated worktree based on the verified merge. A live owner
session reader is not available in this tool set; the checks establish repository
state, not that other sessions are paused. Repeat the check before implementation
or publication and stop on an overlapping change. Nothing here pauses or changes
another session.

Oversight/connector review and Juan's exact-revision acceptance remain pending.
Submission for review is authorized; plan acceptance is separate from merge
approval. GitHub CI builds/package checks remain authorized; local application
build/package/install/deploy remain prohibited.

## Verification plan

For this documentation proposal: verify source ownership at the baseline, local
and remote overlap, relative links, document-only diff and whitespace. Do not claim
application tests for a plan. Future implementation must prove:

| Boundary | Required observable evidence |
| --- | --- |
| Selection/identity | No initial read; eligible Open thread selects one complete origin; provider/account/native-ID lookalikes remain distinct. A→B and B→A late resolver, chooser, message, body, copy and inventory completions cannot repaint or focus the other origin. Same-origin activation does not duplicate reads. |
| Full thread | Vendor/non-vendor/ambiguous-vendor cases, explicit Retry, newest-subject heading, source context vs thread subject, other-sender/filter notice, oldest-first paging, no-text/partial/Sent states, hostile text and unchanged Vendors reader. |
| Current span | Query commit vs retained previous rows; origin on second/later loaded page; both refresh response orders; partial-span failure; source absent from current span; changed identity; Load more preserves reader and latest scroll. No automatic paging or replacement selection. |
| Reading continuity | Real-browser window focus return, connect-queue refresh and document-visibility return with multiple explicitly expanded bodies, including one in a later user-loaded thread page. Successful same-origin/thread/context revalidation re-reads bodies through guarded owners and restores expansions, clamped reader/body scroll and reader focus. Prove both refresh response orders, repeated invalidations and refresh racing explicit A-to-B selection; failure/absence/changed identity or context stays cleared. Assert no cached bytes, duplicate reads, unopened-body expansion or focus theft after the user moves elsewhere. |
| Controls/actions | Exact sender/full addresses/finder pinning; draft vs committed labels and account modes; Search/Enter/Apply/Reset and busy associations after moving markup; existing action/expiry/confirmation/retry/removal tests retain meaning. A's action completion while B is read cannot transfer target/state. |
| Removal/lifecycle | Nested holds, held physical bytes, accepted/refused/deferred delete/clear, account/vendor changes, hidden tab/return and old responses. Both readers clear before mutation; no hidden body/copy DOM or late reopening. |
| Scroll/focus | Real overflow and clamping with 220+ senders and >100 messages; preserved user scroll while reading, after re-render/append and on narrow Back. Keyboard activation/choice/Back, missing-trigger fallback, no hidden focus target or resize-induced query/read. Use a real browser, not only fake-DOM scroll assignments. |
| Geometry | In Chromium and WebKitGTK (the Linux webview), 1920×1080 and 1524×785 three panes; 1024×768 messages plus reader; 640×520 and 320 CSS px single-pane flow; 200% text/zoom and very short heights. Long addresses, subjects, filter labels, expanded rows and bodies; Filters open/closed. Probe content-width changes independently of viewport width. No horizontal document overflow or clipped controls; content count cannot grow the document indefinitely. Other tabs keep their layout. |

Use failing-before/passing-after actual-handler tests for the presentation/scroll
changes, then affected and full desktop tests, TypeScript `--noEmit`, whitespace
and a cold diff audit. Synthetic Chromium and offline static-page WebKitGTK checks
use current source/mock IPC and no application build or live profile/provider/model
access; report exact browser/webview versions. Exercise continuity with real browser
scroll, not only fake-DOM assignments. GitHub CI owns authorized native build/package
checks. Static WebKitGTK geometry is not packaged IPC/production-CSP qualification;
record that remaining promotion gate separately, with no local build/install.

## Implementation summary

One documentation proposal for review grounded in merged #350. It defines the next
read-only pane slice, keeps existing actions in their rows and names the remaining
separate action-migration work. Application code and the existing contracts are
unchanged. This is proposed behavior, not a claim that a pane already exists.
The review amendment addresses F1 (review 5810521636): fresh guarded body re-reads
and reading-position restoration on successful same-query refresh, with explicit
failure/race clearing. It also names content-width measurement, WebKitGTK checks,
the large-diff split boundary and the local-only frozen commit reference.

## Cold diff audit

`plans/PR-Inbox-Persistent-Thread-Pane.md`: root cause names the current layout and
entry owners; R1–R4 define the proposed presentation and preserved guards; stage,
non-scope and verification sections limit implementation and review. The change
surface contains only this plan. Relative-reference, source-owner, scope and
whitespace checks validate the proposal; runtime acceptance evidence is future work.

## Gap audit

NOT DONE for review, acceptance or implementation. The proposal is ready for its
authorized review submission; exact-head oversight/connector review and Juan's
acceptance remain. This docs-only PR includes no application code, local build,
install or deployment. This PR and each implementation PR require their own merge
approval.
