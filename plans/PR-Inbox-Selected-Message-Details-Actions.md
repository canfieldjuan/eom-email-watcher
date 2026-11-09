# Email Watcher UX: stable header, Filters and message activation

Status: **contract-only UX amendment proposal; exact-head oversight review and
Juan's acceptance of this revision are pending**. The selected-message plan was
accepted in PR #354; that accepted revision remains the authority until this
amendment is accepted. Preparing this amendment is authorized; its merge and
application implementation are held. Implementation requires separate direction
after exact-revision acceptance. No local application build, packaging,
installation or promotion is authorized here.

Amendment source base: `main` at `e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e`,
the verified PR #358 merge. Its complete tree equals qualified extraction head
`1e342c271ab2da70d255e9fedc97ee94c7397e70`. The details renderer is now extracted;
the selected-message controls/lifetime migration below remains unimplemented.

Original plan baseline: `main` at `dc39c48d959ccbcf55a49c8905175295a112a419`, the PR #353
merge. Its tree equals reviewed head `8be37008dddf2ad077f9cd91048888a2cdbb1291`.
PR #352 is merged as `8c9913562bd6d0908e647ef43188bdd7f1b59164`.
Both reviewed heads and the final merge have four successful CI jobs; both PRs have
oversight PASS, clean connector reviews and no unresolved threads. These are source
qualification facts, not evidence of installation.

The [persistent-pane plan](PR-Inbox-Persistent-Thread-Pane.md) deferred the
selected-message control migration subsequently accepted in PR #354. This amendment
adds the bounded header/Filters/activation choices in R0 to that existing target;
it does not approve the whole older
[three-pane proposal](PR-Inbox-Three-Pane-Reading.md).

## UX amendment: scope, precedence and current owners

Juan directly confirmed preparation of one contract-only amendment covering the
common header, stable Filters geometry and primary-row retained-chain activation.
That direction is not acceptance of the unwritten implementation or this revision.
The proposed experience and R0 below replace only these presentation choices;
R1-R4's identity, confirmation, refresh, outcome and removal rules remain required.

If this exact revision is accepted, these replacements take precedence over the
named older clauses. Those older documents remain historical records; they are
not parallel instructions to keep an obsolete interaction.

| Earlier choice | Proposed replacement and limit |
| --- | --- |
| This plan's separate Open thread step / no whole-row activation, and Navigation/Filters §3's Open thread alongside expansion | One semantic primary-row activation opens the eligible retained chain through the existing entry owner. Replace the redundant Open thread control and row expansion only with the usable selected-message replacement defined here. |
| Persistent-pane R2 allows expanded Filters to consume pane height / cause outer scrolling | Filters becomes an anchored, viewport-bounded panel outside layout flow. Toggling it alone preserves the underlying document extent, pane geometry and scroll, including when the base layout already needs accessible outer scrolling. |
| Persistent-pane R3 and Navigation/Filters focus return names the old Open thread button | Back returns to the initiating eligible primary-row target, or the existing visible heading fallback; no automatic paging or selection. |
| This plan excludes unrelated tab redesign; the shared tabs inherit each view's shell/intro geometry | Only the common header/navigation presentation changes across views. Page content widths, routes, admission and view-specific behavior stay unchanged. |

Current source owners at the amendment base:

| Owner | Confirmed controlling factor |
| --- | --- |
| [main.ts:516][current-header] and [styles.css:39][current-shell] | Shared navigation sits after the intro inside the view-dependent shell. Inbox width/padding and visually hidden intro differ from other views; the header must be separated from those content containers, not patched with per-view offsets. |
| [main.ts:540][current-filters], [inboxThreadEntry.ts:178][current-fit] and [:192][current-measure] | Filters is in-flow; its height moves the grid top used to calculate available height. The fitted multi-pane layout requires at least 240px; the [existing CSS height fallback][current-pane-fallback] can then increase the outer extent. Moving only a child or hiding overflow does not fix this owner chain. |
| [main.ts:2582][current-row] and [inboxThreadEntry.ts:233][current-entry] | The primary button expands row details while a sibling Open thread calls the guarded entry owner. Route the replacement target through the same admitted origin/current-span/scope owner; create no second selection or resolver. |
| [main.ts:1807][current-details] | PR #358 supplies the existing details renderer. Reuse its body, handlers and scalar timer handoff; do not re-extract, copy or move controls into shared thread-message cards. |

### Input inventory and differences from recorded work

The following private durable aliases were copied and hashed for review. They are
source-only reference inputs, not qualification of the proposed behavior.

| Alias | SHA256 / recorded scope |
| --- | --- |
| `ux-direction-input` | `81244af43c06c159db9a4288a3be17a77040ded71ee35d6a5b5f66ffe779adb1`; stable Filters and row activation proposal. |
| `navigation-direction-input` | `6b7b2709a0efd0470f2b94608f5557c0eaada4f147537adeefe7f070af0fc71b`; compact common header proposal. |
| `prior-filter-probe` | `819dbf1323c30970317679f9d26f992de2e71eab81b980c9ae6a84d301058f13`; Playwright Python, Chromium cache build 1228, loopback synthetic fixture, closed/open/closed-again at 1920x1080, 1524x785, 1440x900, 1280x720 and 1024x768. No model/prompt/provider calls. |
| `prior-filter-measurements` | `3a06d2420195d918463c3bebf8cc6632b25d6df12cc355c2216fbfc11d105df3`; the provided capture records document height 785 to 1242 at 1524x785 on opening Filters, with fitted layout changing to fallback; all recorded native-call counters are zero. Not rerun here. |
| `ux-amendment-input-inventory` | `4cb64f616c7bb02a132960b0293f8f26b81f529f54cc63fb5ac52156b0f9ba04`; binds those inputs and screenshots, settings and limits. |

The supplied probe does not seal a semantic browser version, source revision or
server/fixture bundle. Its recorded dimensions identify the reported problem;
they are not a fresh browser run, installed-product evidence or a qualified UI to
port. Before reusing a harness for implementation, pin/hash its actual source,
fixtures, browser/runtime versions, viewport/text/zoom settings and results.
No model, prompt or capacity experiment is part of this UI work.

Differences from that recorded baseline are deliberate and exhaustively scoped:
shared navigation no longer inherits view-specific content geometry; Filters no
longer changes flow geometry; the primary row opens its retained-chain context
instead of expanding details before a separate Open thread action. The previously
accepted single selected-message details surface remains the target, with its
existing action semantics unchanged. No new layout implementation is claimed.

## R0 — common header, stable Filters and primary activation

### R0a — one compact, persistent common header

- All existing destinations keep their labels, order, selected-state accessibility,
  keyboard activation, disabled/admission behavior and existing view-switch effects.
  Use one header independent of each page's width, intro and scroll container.
- At the same viewport, text size and zoom, switching views preserves the header's
  top/left anchor, dimensions and gutters, including after content has scrolled.
  Keep navigation visible during content scrolling. Content may retain its current
  appropriate width; do not widen Settings or change the Inbox's selection owners.
- Use compact text navigation with ordinary/medium label weight, a restrained
  current-view underline/marker and clear hover/focus states. Remove the large
  generic introductory banner from the shared header; retain accessible application
  identity and meaningful page titles/help in their content areas. Do not add a
  large decorative container, hide labels or replace destinations with icons.
- Narrow windows may wrap navigation consistently across views. Keep every
  destination readable and reachable at enlarged text/zoom; no hamburger, auto-hide,
  tiny targets, clipping or overlap with the focused content below it.

### R0b — Filters does not resize or scroll the underlying workspace

- Place the existing filter form in one labelled, modeless panel anchored to
  Filters and outside the workspace's layout flow. Finalize its position and the
  Inbox height calculation against the common header, not hard-coded per-view
  offsets. Opening/closing alone changes neither outer document extent nor pane
  bounds, selected origin, sender/message/reader scroll or visible row set.
- This invariant also holds on narrow/short/enlarged-text layouts: if the closed
  base layout legitimately needs outer scrolling, toggling Filters preserves that
  range and underlying positions. Constrain the panel to the available viewport
  with its own accessible scrolling; never clip fields or mask a sizing defect
  with document overflow suppression. The panel must not obstruct navigation or
  leave focused controls under the header.
- Preserve all fields, bounds, native form associations, draft/applied distinction,
  explicit Apply/Search/Reset, account/sender scope, busy rules and applied labels.
  Closing never silently applies, resets or discards a draft. Toggling starts no
  query, body read, analysis or other native operation.
- Filters/Close and Escape can dismiss the panel; Escape/Close returns focus to
  Filters. Pointer and keyboard access to every field remain usable, with no focus
  trap. A form action or text selection cannot also activate a message row.

### R0c — primary row opens its retained chain, once

- Give each eligible message one semantic primary-row control, covering its
  normal sender/subject/metadata hit area. Pointer activation and native Enter/Space
  equivalence select that exact origin and enter its retained-chain context through
  `InboxThreadEntry` and the admitted-origin key. Replace the separate Open thread
  step once this target and the selected-message surface are usable.
- Keep cards as neutral containers and native actions/links/disclosures as separate
  controls. No button contains other buttons, no controls-bearing listbox, and no
  card-wide bubbling handler that turns an action, text selection or drag into a
  thread open. Keyboard activation dispatches once, not once per key/click handler.
- Preserve visible focus and selected state; invalid, partial, previous-query,
  blocked or stale origins cannot select/read through this route. No automatic
  first selection, hover activation or selection from background refresh.
- Re-activating the same current origin preserves ready/pending reading state and
  performs no duplicate resolver/message/body read. It does not create a new
  selection lifetime or cancel a pending confirmation/parameter step. Existing
  explicit Retry remains the route for failed fresh-origin resolution. Back closes/clears through the same
  owner and restores the initiating eligible target with existing scroll rules and
  heading fallback; resizing does not reopen or refetch the chain.
- Whole retained chain means the existing eligible retained conversation context,
  with its existing pagination and zero/one/many resolution, explicit ambiguous
  chooser, no-match/unavailable/partial-history/no-text states and source-account
  fencing. Do not fetch missing provider history, infer a vendor or eagerly load
  every page/body. A clicked message is not the sole timeline entry by definition.
- Keep the origin's eligible selected-message details/actions available under R1
  while chain resolution is loading, ambiguous or failed. Timeline success cannot
  authorize another message's actions; selecting/opening never executes them.

## Original owner audit and retained baseline

The present separation is intentional, not an unfixed defect in #353. Opening a
thread keeps the message list beside the reader, but saved analysis, attachments
and actionable controls still belong to expanded message rows. The single reader
renders the retained thread and its body/copy/inventory controls instead.

| Current owner at the baseline | Confirmed responsibility; consequence for this proposal |
| --- | --- |
| [main.ts:1807-1917][rows] | `renderInbox` owns row metadata, saved summary, truncation disclosure, suggested action and deadline. Reuse this projection rather than deriving analysis from thread text. |
| [main.ts:1919-2082][calendar] | Calendar projection, exact proposal versions/hash, invitation warning, confirmation and refresh/outcome handling. Preserve all of them. |
| [main.ts:2085-2464][attachments] | Attachment metadata/Open, provider choice, capability parameters and consent, invocation/request IDs, structured refusal recovery, output controls and automation decisions. Move this presentation through one owner, not a copied second set of handlers. |
| [main.ts:2482-2529][row-actions] | Explicit Retry analysis, Delete locally, and independent row-details expansion. Retire the Inbox multi-row expansion surface only after its replacement is usable. |
| [main.ts:927-950][integration] and [inboxThreadEntry.ts:18-35,194-240][entry] | Complete Inbox-origin identity, committed query/span and mailbox-operation scope select the reader. The current compact source label additionally waits for successful resolution. The new selected-message projection must use the eligible Inbox row, independently of thread-resolution success. |
| [main.ts:1241-1265][lifecycle], [main.ts:2876-2990][removal] and [threadHistory.ts:36-72][history] | Mailbox request fences, deletion/clear barriers, both readers' mutation holds, and refresh dispatch. Keep these as the lifecycle owners. |
| [main.ts:1548-1575,1630-1685][outputs] | Message/part/job/artifact keyed UI state, result rendering and output previews. A detached or hidden details surface must not retain preview DOM. Durable job/request bookkeeping remains distinct from presentation bytes. |
| [threadViewDom.ts:267-364][thread-dom] | Full retained thread, explicit body/copy/inventory reads and paging; the Vendors reader uses the same renderer. Do not turn every thread message into an actionable Inbox row. |
| [lib.rs:1857-1955][native] and [lib.rs:2127-2145][native-calendar] | Existing native admission and message/attachment/proposal command boundaries remain authoritative. UI selection is not a replacement for native admission or backend version checks. |

Existing regression owners to reuse include `inboxThreadPane.test.ts`,
`inboxThreadContinuityRaces.test.ts`, `inboxThreadEntry.test.ts`,
`threadIntegration.test.ts`, `inboxMessageScroll.test.ts`,
`inboxActionState.test.ts`, `inboxRemovalLifecycle.test.ts`,
`automationDecision.test.ts`, `calendarConsent.test.ts`,
`calendarProposal.test.ts` and `threadViewDom.test.ts` under `desktop/test/`.
The #353 delta review reports 359 desktop tests, clean typechecking and a
17-mutant killing sweep at its exact head. Those existing tests are baseline
evidence; they do not prove the new presentation or action transitions.

There is no separately validated implementation of this new migration to port.
The earlier source-only browser packet (alias `inbox-persistent-pane`) belongs to
the other coding chat; its bytes, hashes and browser versions have not been
independently inventoried here. Before reusing that harness for
implementation qualification, obtain and hash its scripts, fixtures, settings,
versions and results. Reuse proven fixtures and read/lifecycle regressions; do
not represent an old geometry result as proof of this new surface.

## Proposed experience

- Keep compact message rows with full sender/address access, subject, received
  time, existing priority/category/state and action/attachment/truncation cues.
  The primary-row control opens its retained-chain context under R0c; keep its
  selected state and no automatic/hover selection.
- After primary-row activation, place a labelled **Selected message** section at the top of
  the existing Inbox reading wrapper. It shows that originating message's saved
  analysis, action/deadline, calendar information, attachments, existing results
  and controls in their current relative order. Keep its source subject, sender,
  received time and account distinct from the thread's newest-subject heading.
- The existing retained-thread timeline follows in the same reading wrapper.
  Preserve its history-outside-filters notice, resolution/chooser, linked-owner,
  partial/Sent/no-text states, explicit body/copy/inventory expansion and paging.
  Its other messages do not acquire the selected origin's action controls.
- Remove Inbox row-details expansion and its duplicate detailed/action DOM when
  this replacement lands. Saved summary/details are available in the selected
  section, not simultaneously mounted in expanded rows. Keep concise list cues
  so selecting a message is not required to discover a pending confirmation or
  failed action. Do not silently drop any existing indicator.
- Back clears the selected section and thread to the existing placeholder and
  returns to Messages with the current scroll/focus rules. The same selected
  section remains reachable in single-pane narrow mode. **Clear local history**
  and durable removal notices remain outside the reader and retain their
  history-wide meaning. Vendors content/read behavior is unchanged; it uses the
  same common header as the other views.

These are proposed product choices, not a claim that the current app already has
this behavior. No new provider verb, bulk action, compose/reply or message search
is introduced.

## R1 — one selected-message binding, one details renderer

Extend the existing `InboxThreadEntry` integration to expose a typed, immutable
selection context only for a complete, currently eligible Inbox origin. It binds
provider, account, mailbox identity and message reference to the committed query
epoch, mailbox-operation scope and selection lifetime. The current committed span
proves eligibility; its object/render identity is not part of the lifetime. A missing,
partial, hidden, previous-query, blocked or stale binding produces no actionable
context. Never fall back to the active account, an index, subject, sender, native
thread ID, or a message chosen from the thread timeline.

Use a single named current-context check at that owner. Do not make a parallel
selection/query/lifecycle controller or scatter approximate identity checks across
buttons. Typed context creation and that check are the strongest existing owner
mechanism; do not replace them with string matching or permissive defaults.

A pending step for A survives a same-query background refresh when the fresh
committed snapshot still contains the same complete origin, in the same scope and
selection lifetime, with no mutation hold. A span re-commit, new row object or
render alone cannot cancel it. Compare complete origin and selection lifetime,
not span identity or the [reader's refresh generation][reader-generation], which
also changes on ordinary invalidation. Keep the reader's existing generation
guards intact; do not use them as a pending-action cancellation token.

This is a routine overlap: [scheduled checks][scheduled-refresh] and
[Connect-queue changes][queue-refresh] refresh the Inbox while the
[asynchronous native confirmation][confirmation] can remain open. Preserve the
captured action target, parameters, versions and hash across this overlap; the
unchanged backend owners still decide staleness. A newly refreshed version is not
permission to rewrite the target or payload the user approved.

The selected detail source is the eligible `InboxItem` in the committed snapshot,
not a thread-message/copy projection. It can show existing saved details and
currently eligible actions while thread resolution is loading, ambiguous,
unavailable or failed; no body or vendor read is authorization for those actions.
Conversely, a successfully resolved old thread cannot revive an ineligible Inbox
origin. The current source-label resolution dependency must be adjusted at the
same integration owner, not bypassed with a second source label.

Extract the current row's detailed rendering/handlers into one Inbox-specific
renderer/adaptor, in `main.ts` or a small `inboxMessageDetails.ts` module. Give it
the current context and existing operation/state dependencies. Compact rows no
longer mount these controls. Keep `ThreadViewDom`'s shared message cards read-only;
do not duplicate the renderer inside that class or copy handlers into the pane.
Remove redundant Inbox expansion state and cleanup paths once no reader uses
them. Preserve message/attachment/job keyed operational state through that move.

## R2 — preserve every action's target, prerequisites and effects

| Existing control family | Required unchanged command/effect and target |
| --- | --- |
| Retry analysis | Existing eligibility and explicit `analysis_requeue` for the captured origin's message ID; no retry on selection, render or refresh. |
| Attachment Open | Existing `attachment_open` with that message ID and retained attachment part ID; default-app opening only after explicit activation. |
| Connect actions / Resume | Existing selected provider tuple, capability/version, parameter collection/validation, manifest features, confirmation requirement and `attachment_capability_invoke` payload. Keep original job reconciliation/request-ID rules and structured refusal/reselection behavior. No silent provider substitution or duplicate job. |
| Capability outputs | Existing message/part/job/artifact binding for presentation, preview and opening/export controls. No automatic preview, broader file access, changed output limit or hidden retained preview. |
| Automation confirmation/decline | Existing fire identity, prepared rule/version/state evidence, consent text and `runAutomationDecision` owner. Preserve the refresh-required barrier after a failed decision refresh and all truthful queued/completed/refused outcomes. |
| Calendar create/recheck/decline | Existing message ID, run ID, state/proposal versions and proposal hash, account identity, expiry behavior, invitation warning and confirmation. Preserve the distinction between a saved decision and a completed external write. |
| Delete locally | Existing `deleteInboxItem` and `InboxRemovalClient`, exact origin message ID, local-only confirmation, dual-reader hold and durable completed/deferred/refused results. Selection never broadens deletion to a thread, vendor, sender or account. |

The UI entry is new; no native IPC signature, API, schema, admission, entitlement,
version/CAS rule or backend target resolution changes in this slice. Preserve
empty/false/zero values where the existing contract admits them; do not invent
defaults for missing identity, version, parameter or permission fields. If an
existing boundary cannot support the migration safely, stop for a plan amendment
rather than extending the backend under a presentation change.

## R3 — confirmation and completion belong to the original message

For each operation, trace: current selection -> captured minimal operation target
-> parameter/confirmation awaits, if any -> current-context check -> existing
operation owner/native command -> target-owned outcome -> existing refresh owner.

- Check context before collecting input and again immediately before dispatch,
  after every intervening asynchronous parameter/confirmation step. Switching
  A-to-B, Back, query/account change, loss/change of the originating identity or
  scope, or a mutation hold before dispatch aborts the old UI attempt. An ordinary
  same-query refresh is not itself such a loss; apply R1's survival rule. A's
  dialog can never authorize B. Declines
  retain their existing explicit-activation behavior; do not add confirmation
  dialogs where none exist. No calls occur merely from focus, hover or rendering.
- If approval or parameter submission arrives while that same-query refresh is
  already in progress, keep one pending step and await that owner's revalidation
  outcome before dispatch. Show that the step for A is waiting and has not yet
  been sent. Do not enqueue a second refresh, hunt for A with extra pages, start a
  retry loop or require the user to approve again merely because of a re-render.
  Observe the existing refresh owner's actual commit/failure outcome;
  [`InboxReload.request()`][refresh-owner] is not a join signal: while active it
  can queue another request and immediately return false. Repeated same-query
  invalidations retain the same pending step; they do not create new attempts or
  replace the originally approved target/versions.
  After a fresh commit, unchanged eligible A dispatches exactly once with its
  original captured target and versions. Failed revalidation, absent A, a changed
  scope or a cancelled selection aborts visibly. If approval arrives after the
  fresh commit, apply the same check immediately. Already-dispatched operations
  retain the separate completion rule below.
- Every abort after explicit approval or parameter submission must show an
  accessible notice identifying the original message A: **not sent**, with the
  actual reason, such as changed selection/scope, unavailable origin or failed
  Inbox revalidation. Never use a bare return or suggest an external action was
  sent or completed. The notice remains observable outside the selected section,
  cannot be silently erased by the refresh that caused it, and does not steal
  focus or appear as B's result. Dialog denial/dismissal preserves its existing
  cancellation meaning.
- Capture the existing typed message/part/provider/capability/job/fire/proposal
  target and versions. Do not consult the then-selected row after an await or
  substitute B's IDs into A's operation. The original native/backend admission,
  consent and version checks still decide whether the operation is allowed.
- Once a command has actually been dispatched, selection change alone does not
  cancel, repeat or falsify that operation. Keep its minimal target and existing
  in-flight/request bookkeeping until its real outcome is reconciled. Do not keep
  an entire old `InboxItem`, old details DOM or preview bytes as operation state.
- A completion may update A's normal stored/keyed state and trigger the existing
  guarded Inbox refresh. It cannot insert A's result, error, loading state, focus
  or scroll into B's selected section. Any global notice about A must identify A
  truthfully rather than implying the currently selected B was acted on. Preserve
  B's continuity only through #353's existing fresh-span/read guards.
- Use existing per-operation in-flight guards across the whole UI lifetime. Moving
  or remounting the section, selecting A again or a duplicate click must not issue
  a second decision/job. An error refreshing the Inbox is not proof that the
  underlying external action failed; preserve its existing recovery barrier.
  Preserve the existing distinction between resuming an active job and a new,
  explicitly requested invocation after terminal completion; do not add a blanket
  ban on legitimate repeat actions.

## R4 — invalidate presentation without weakening the reader or removal barriers

Clear selected detail/preview DOM and the actionable selection context immediately
on invalidation, Back, tab hiding, query/account change or mutation hold. Disabled
or CSS-hidden old controls are insufficient. Retained previous-query list rows may
still show their existing concise Previous results projection; they cannot open
the new section or supply an actionable context.

Clearing the actionable presentation during an ordinary same-query refresh does
not cancel an already pending step's minimal captured target and selection-lifetime
token. Keep no old row/detail/preview payload in that pending step. It authorizes
no new action from stale rows and must pass R1/R3's fresh revalidation before any
dispatch; failed or ineligible revalidation produces the visible not-sent notice.

After a same-query refresh, render selected details only from the fresh committed
row with the same complete origin. Do not preserve old analysis/attachment/result
payloads in #353's bounded reading intent. Old body/copy/inventory bytes and
capability-preview DOM remain cleared; body restoration still requires the
existing fresh same-thread/context reads. A missing origin stays unavailable,
without a deletion claim, automatic replacement selection or extra paging.

Do not couple Inbox span commit to reader restoration again: Back during refresh
must still commit current rows with `refreshReader=false`, leaving eligible
primary-row activation enabled.
Keep the generation/identity/page/count/post-body-read checks proved in #353.

Begin the existing nested mutation hold before a removal dispatch and clear both
Inbox and Vendors reader payloads as today. Ending one nested hold cannot revive
the section or reader while another remains. Deferred/refused removals stay
truthful; physical retention does not authorize showing held bytes. Preserve
explicit Retry and the existing refresh scheduler; introduce no competing loop.

## R5 — bounded layout, keyboard and information parity

Keep the existing content-width-driven wide/split/single modes and their scroll
owners. Selected details and the timeline use the same bounded reading wrapper;
do not add a per-message details scroll box. Preserve the existing body-text
scroll protection. Long summaries, attachment names, results, addresses and
expanded controls wrap without horizontal document overflow or clipped controls.

Keep Search/Filters/applied scope, sender navigation, Load more and message scroll
behavior. List rows retain enough current state to identify a message requiring
attention without running its action. Preserve explicit opening, no initial read,
Back, selected-state semantics, short-height/200% accessibility fallback and
keyboard reachability, with R0b's Filters geometry invariant at every size.
Focus follows explicit user actions; background completion and resizing cannot
steal it. Preserve the corresponding control across a current selected-section
render, falling back to its heading if the control disappears. Hidden/closed
sections are absent from keyboard and accessibility navigation and hold no old
preview DOM.

## Implementation order and acceptance gates

1. This PR changes only this proposal. Oversight reviews its exact head; Juan must
   accept that revision and separately authorize implementation. Green CI is not
   plan acceptance or permission to merge this proposal.
2. PR #358 has completed the unchanged renderer extraction. Reuse that production
   owner and the admitted-origin/request-ID fixes now on main. Prove payload,
   consent, eligibility, request-identity and outcome parity before relocation;
   do not create a copied alternative implementation.
3. After acceptance and separate implementation direction, deliver the common
   header first as an independently reviewed stage, including all-view stability
   and Inbox available-height proof. Finalize Filters geometry against that header.
   Then deliver the Inbox interaction: stable Filters, primary-row activation and
   the single selected-message surface, with R1-R5's lifecycle/action guards.
   Move the existing renderer instance and retire redundant expanded-row details
   and expansion state together. Keep every stage usable at narrow widths with
   all action families reachable; never ship a wide-only or action-dropping stage.
   If the combined implementation would exceed about 1,000 lines including tests,
   use stacked header/Inbox PRs with explicit dependencies. Further mechanical
   guard stages may land with old presentation intact; do not remove Open thread
   before the replacement works or duplicate action surfaces during the split.
4. Require actual-handler regressions, the affected tests and full desktop suite,
   typechecking, cold diff and gap audits, source-only browser evidence, exact-head
   oversight/connector review and green CI. Each implementation merge requires
   Juan's approval for that PR. Local application build/package/install/promotion
   remains a separate decision; no live profile, mailbox, provider or model calls.

## Required verification for implementation — not results of this proposal

| Boundary | Evidence that must falsify a broken implementation |
| --- | --- |
| Common header | Compare header bounds across all existing views at fixed viewport/text/zoom, different content heights and previous scroll. Same anchor/dimensions/order; all labels, keyboard/focus/current/disabled/admission behavior and normal view effects preserved. Narrow wrapping, scrolling and enlarged text leave every destination reachable. |
| Stable Filters | At 1920x1080, 1524x785, 1440x900, 1280x720, 1024x768, 640x520, 320 CSS px, short height and 200% text/zoom, repeat closed/open/closed with unchanged data. Measure equal outer extent, pane bounds and underlying sender/message/reader offsets; preserve selection/drafts/row set and zero extra native calls. Exercise both fitted and already-scrollable base layouts, every field, Apply/Reset/Search, keyboard/Escape/Close and focus return. |
| Primary-row activation | Actual production owner: pointer and Enter/Space open one correct complete origin; no Open thread second step, initial/hover read, duplicate pending/ready same-origin read, or action on opening. Same-origin reactivation preserves the pending approval/lifetime. Action/link/disclosure/text-selection/drag/form activation does not open a row. Invalid/mixed/stale origins refuse; ambiguity/no-match/partial history/paging and eligible Inbox details remain truthful. Back restores exact target or visible fallback without paging. |
| One surface and parity | Every current action/detail family is reachable on a selected origin, mounted once, with exact old IPC payloads and consent. Compact unselected rows retain all indicators; old row expansion/hidden action clones are gone. Exercise no attachments, one/many attachments, unavailable analysis and valid false/zero values. |
| Identity and availability | Provider/account/mailbox/native-ID lookalikes, missing/partial context, previous-query rows and changed mailbox scope cannot target the wrong item. Valid vendor, non-vendor and unresolved/ambiguous/failed thread lookups retain eligible Inbox-owned details; no action is borrowed from another timeline message/copy. |
| Dialog and dispatch | For every async-confirmation family: open A, pause its parameter/dialog step, select B or Back/change query/hold, then approve A; no native dispatch. The current unchanged A positive dispatches once with its original exact target. Decline and cancelled dialogs preserve existing semantics. |
| Background refresh during a pending step | For each calendar, capability and automation confirmation/parameter path, let a scheduled-check refresh and a Connect-queue refresh land while A's step is pending. Same complete origin, scope and selection lifetime with no hold: exactly one dispatch using the original captured IDs, parameters, versions and hash, despite changed span/row objects or reader refresh generation. Absent A, changed scope, failed refresh, Back or A-to-B-to-A lifetime change: zero dispatch and an observable A-specific not-sent notice/reason after approval. Run both orders: refresh commits before approval; approval/submission arrives while refresh is in progress and is released only after its outcome. Assert waiting is visible, no duplicate call/refresh/paging, no bare-return approval loss and no A notice/focus transfer to B. |
| In-flight outcomes | Dispatch A then select B; finish success, refusal, transport uncertainty and refresh failure in both response orders. No B result/focus/scroll mutation or duplicate A call; durable A recovery remains reachable after returning and refreshing. Include equal subjects/filenames across accounts. |
| Writes and expiry | Calendar invitations and expired-recheck paths retain exact account/version/hash/consent. Automation stale versions and decision-refresh barriers remain enforced. Confirm/decline double clicks, remounts and selection loops cannot duplicate decisions or submit a second capability job. |
| Removal and hidden bytes | Cancel/accept/delete/clear with nested holds and both readers open; completed/deferred/refused outcomes, old callbacks and physically retained bytes. Context/old details/preview DOM clear before dispatch, source mail remains untouched, Clear remains history-wide and B cannot inherit A's target/outcome. |
| Refresh and prior regressions | Current span commits after Back independently of reader cancellation. Repeated invalidations, origin on a later loaded page, stale cursors, body failures, A-to-B/B-to-A late completions and the #353 replay/page/count guards retain their meaning. Fresh row state replaces old details without stale controls. |
| Real browser | Chromium and WebKitGTK: 1920x1080, 1524x785, 1024x768, 640x520, 320 CSS px, short heights and 200% text/zoom; long details, open Filters and populated actions. Exercise actual scroll/focus, primary-row activation/Back, same-origin re-render, action completion while focus is elsewhere and content-width changes independent of viewport width. No fake-DOM-only geometry claim. |

Use the existing fixture/native mocks, not the installed application. Record exact
browser versions and harness hashes. Run real production handler/renderer wiring
with controlled IPC, not tests that reimplement the proposed current-context rule.
Use fail-before/pass-after proof for any defect found while extracting handlers,
and per-clause mutants for the selection, post-await and outcome guards. Equivalent
survivors need a concrete dominance explanation. Browser/static-webview results do
not qualify packaged IPC or production CSP; keep that promotion gap explicit.

## Non-scope and proposal validation

No reply/compose, action on arbitrary historical thread messages, new provider or
API, HTML/Markdown rendering, bulk action, schema/storage/capture/retention change,
new query/lifecycle controller, dependency, native admission change or unrelated
view/content redesign. The common header presentation in R0a is the only cross-view
change; page routes, content widths and view-specific behavior remain unchanged.
Do not fold in #349-350's nonblocking test/cosmetic follow-ups, the
[Windows descriptor follow-up #360](https://github.com/canfieldjuan/eom-email-watcher/issues/360),
model work or frozen attachment-contract work.
Human LCS gap/promotion records remain held; this UI proposal writes none.

For this docs-only PR, check baseline/merge trees, owner citations and relative
links, open-work overlap, the single-file diff and whitespace. Do not run or claim
new application tests for a contract document. Proposal complete does not mean
behavior accepted, implemented, merged, installed or qualified.

[rows]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L1807-L1917
[calendar]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L1919-L2082
[attachments]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L2085-L2464
[row-actions]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L2482-L2529
[integration]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L927-L950
[entry]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/inboxThreadEntry.ts#L18-L240
[lifecycle]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L1241-L1265
[removal]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L2876-L2990
[history]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/threadHistory.ts#L36-L72
[outputs]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L1548-L1685
[thread-dom]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/threadViewDom.ts#L267-L364
[native]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src-tauri/src/lib.rs#L1857-L1955
[native-calendar]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src-tauri/src/lib.rs#L2127-L2145
[reader-generation]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/inboxThreadEntry.ts#L93-L124
[scheduled-refresh]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L5447-L5451
[queue-refresh]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/main.ts#L3180-L3196
[confirmation]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/confirmation.ts#L7-L20
[refresh-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/dc39c48d959ccbcf55a49c8905175295a112a419/desktop/src/inboxReload.ts#L61-L100

[current-header]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L516
[current-shell]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/styles.css#L39
[current-filters]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L540
[current-fit]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/inboxThreadEntry.ts#L178
[current-measure]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/inboxThreadEntry.ts#L192
[current-row]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L2582
[current-entry]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/inboxThreadEntry.ts#L233
[current-details]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L1807
[current-pane-fallback]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/styles.css#L241
