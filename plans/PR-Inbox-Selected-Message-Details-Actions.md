# Email Watcher UX: conversations, one reader and per-email extras

Status: **revised contract-only proposal; exact-head oversight and Juan's
acceptance of this revision are pending**. The earlier PR #361 head `a55f075`
is superseded, including its PASS/green-CI result. Do not accept or merge that
older product definition. The operator directly confirmed **Revise the contract
only** for the conversation model below. No application implementation or live
change is authorized. Proposal merge is held for exact-revision acceptance;
implementation requires separate direction after acceptance.

Source baseline: merged `main` at
`e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e` (PR #358), including the existing
Inbox details renderer, admitted-origin owner and request-ID fixes. These are
implementation prerequisites, not proof of the new conversation UX.

## Product model and explicit supersession

The Inbox lists **conversations**, not one row per member email. An individual
email is a conversation of one. Several independent emails from the same sender
stay separate; each actual chain occupies exactly one row. Opening either kind
uses the same far-right reading pane. Within it, newest emails appear first and
**Load older messages** appends older pages. Each eligible Inbox email shows its
own saved details/actions inside its message card; there is no separate thread
viewer, row expansion or top Selected-message section.

| Superseded choice | Replacement and authority |
| --- | --- |
| PR #354's per-message list, separate Open thread and single Selected-message binding/surface | One conversation row and one reader; independent per-message bindings and extras. Existing mutation targets and safety rules apply to each email, never to a representative or whole conversation. |
| Earlier navigation/pane plans preserve expansion/Open thread and return focus to that button | One semantic primary conversation-row target; Back restores that eligible target or the visible heading fallback. No automatic selection/paging. |
| Expanded Filters may change pane height or outer scrolling | R0b: a viewport-bounded modeless panel outside flow; underlying geometry/scroll stays stable. |
| View-dependent shell/intro controls the shared tabs | R0a: one compact persistent common header; all existing routes, admission and page-content widths remain. |
| THREAD_VIEW_CONTRACT.md's oldest-first message presentation | Its amended **Thread view (desktop)** section owns one newest-first order and older-page contract for Inbox and Vendors, with the existing strict binding/stale rules. |

These proposed replacements take precedence only after exact acceptance. R0a and
R0b are unchanged from the earlier proposal, including the directly confirmed
sticky header. The former UI-only scope restriction is explicitly widened for
future read projections/wire models and shared ordering; it is not a hidden
backend change inside a presentation patch.

## Current owners and recorded inputs

| Owner at the source baseline | Confirmed role / required future change |
| --- | --- |
| [db.py:14356][inbox-query], [engine_api.py:2598][query-api], [lib.rs:1615][query-host], [main.ts:161][query-types] | Inbox currently filters/pages individual message rows. Grouping, page units and per-member action projections require an authoritative read-model change with matching strict Python/Rust/TS wire definitions. Frontend deduplication of a flat page is insufficient. |
| [thread_reads.py:334][members] and [D-identity][identity] | Existing scoped conversation identity and membership remain authoritative; do not group by sender, subject or a frontend-invented key. |
| [thread_reads.py:129][message-pages] and [:236][cursor-owner] | Shared messages are currently ascending; the same projection/cursor owner must implement the newly proposed order and identify that order in cursor binding. Do not add an Inbox-only reverse sort. |
| [threadViewDom.ts:293][reader-page] | One existing message-list renderer/page control serves both entries. Change the shared control to Load older messages; do not create another reader for single emails. |
| [main.ts:1807][details-owner] | Reuse the existing details/handler renderer through a per-message Inbox adaptor, one instance for each eligible loaded canonical email. Never substitute a thread body for its Inbox action projection. |
| [threadHistory.ts:24][view-owner] | Leaving Inbox closes its reader today. R3/R4 explicitly decide pending-before-dispatch and already-dispatched outcomes across that boundary. |
| [main.ts:516][header-owner], [styles.css:39][shell-owner], [inboxThreadEntry.ts:178][layout-owner] | Shared-header placement and flow-sensitive pane measurement remain the header/Filters owners audited in the previous proposal. |

There is no validated implementation of the new conversation list/per-email
extras to port. The existing renderer and lifecycle tests are prerequisites to
reuse, not proof of the new view model. The copied source-only Filters inputs
remain problem evidence: `ux-amendment-input-inventory` SHA256
`4cb64f616c7bb02a132960b0293f8f26b81f529f54cc63fb5ac52156b0f9ba04`
binds the proposals, probe, raw geometry and screenshots. Its semantic browser
version/source/server bundle were not sealed; no measurements were rerun here.
Re-pin/hash actual harness, fixtures, browser/runtime versions, settings and
results before implementation qualification. No model/prompt work or live calls.

The direct conversation direction is retained as `conversation-direction` SHA256
`0b53357da497aefbb72e07bf704a44cbf1ee6421ba35e36692cd106c02b338e9`. Deliberate differences from current code are exhaustive:
conversation projection/counting, unified single/many viewer with per-email
extras, shared newest-first/older pagination, and the header/Filters presentation
below. Capture, retention, attribution and identity derivation do not change.

## R0 — stable workspace and conversation entry

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

### R0c — one row per scoped conversation

- Use the existing full conversation identity from [D-identity][identity]: provider,
  account, effective mailbox identity and thread key. Null identity remains its
  own bucket, never a wildcard. Preserve canonical copies/aliases and IMAP component
  resolution; singleton and missing-provider-thread-ID cases use that existing
  owner. Different chains from the same sender/vendor never combine.
- The authoritative Inbox query groups before applying the conversation page
  limit. No one-row-per-member duplicates, no frontend-only grouping of loaded
  message pages, and no split conversation on a page boundary. Row keys, selection,
  list paging/Load more and every count describing this Inbox list count
  conversations. A separately labelled member count counts canonical emails;
  storage/Health totals retain their existing message units.
- Apply account/identity/admission/removal scope first. A conversation matches when
  at least one eligible Inbox member satisfies **all** active member predicates
  together, including exact sender and keyword/category/priority/status. Do not
  satisfy different predicates using different members or unadmitted context.
  Keep existing predicate meanings, All senders, drafts/Apply and account rules.
- Under a watched sender, show a matching conversation once when that sender has
  an eligible matching member. A mixed-sender conversation can appear once under
  each represented watched sender; All senders still shows it only once. Filtering
  by a non-newest member must not suppress the conversation or change its identity.
- Order conversation rows by newest eligible retained activity, with deterministic
  canonical tie-breaking. The row's title/activity may reflect a newer visible
  member than the one matching the filters; show that distinction truthfully.
  Keep subject/time/source-account and full sender/address access; do not hide
  addresses behind permanent ellipsis or imply a mixed chain has one sender.
  Use only members allowed by current thread visibility; no other sender's chain.
- Aggregate existing member indicators truthfully (pending/failed/action/attachment/
  truncation cues, with counts or mixed state where needed), without executing
  anything. Do not present a representative member's single value as the whole
  conversation's value or add a conversation-wide mutation. Preserve existing
  expiry/refresh owners and the minimum-expiry scalar handoff, not one timer per
  message or automatic paging to discover pending work.
- List cursors must bind the actual conversation order/key, complete query/scope
  and relevant projection revision. Reject old flat-message cursors and stale or
  mismatched group positions. Revalidate membership/identity in the query snapshot;
  never merge different scopes or repair an invalid cursor with a client offset.

### R0d — one far-right reader for one or many emails

- Give each eligible conversation one semantic primary-row control. Pointer and
  native Enter/Space activate the exact current conversation once in the existing
  right-hand reading pane. A single email uses the same path/renderer as a chain.
  Remove the separate Open thread control, row expansion and top Selected-message
  section only when this coherent replacement is usable at every width.
- Keep cards neutral and actions/links/disclosures separate. No nested buttons,
  controls-bearing listbox or card-wide bubbling that opens a conversation when
  clicking an action, selecting text, dragging or using a form control. No initial,
  hover or background-refresh selection; stale/partial/blocked rows remain inert.
- Same-current-conversation reactivation preserves ready/pending reading state,
  loaded pages, scroll and pending action lifetimes, without duplicate reads.
  Existing explicit Retry remains the failure recovery path. Back closes/clears
  through the same owner and returns to the initiating eligible conversation row
  or visible heading fallback, preserving the latest list scroll without paging.
- Use one shared message-reader/order/paging owner for Inbox and Vendors, with
  distinct scoped entry contexts. Inbox supplies the guarded per-email action
  adaptor under R1; Vendors retains its read-only action boundary. No alternate
  per-entry sort or separate singleton/thread UI. Preserve resolution/explicit
  ambiguous chooser, partial-history/Sent/no-text states, body/copy/inventory
  controls, textContent-only rendering and existing read/removal fences.
- [The shared thread contract][thread-view] owns newest-first message ordering,
  deterministic ties/unknown dates, initial newest page and Load older messages.
  No eager full history/body load, provider fetch or search for an older matching
  member. A list match on an old member still opens the newest page; that member's
  card/extras appear only after its older page is explicitly loaded.

## R1 — conversation lifetime and independent per-message bindings

Conversation selection is the exact scoped conversation plus committed query and
operation scope, not the row representative's identity or lifetime. Its owner
must rederive current eligible membership in one snapshot. If the read protocol
carries an origin message as an admission anchor, renew it only from a fresh
committed projection of this same conversation. Removing/changing the displayed
representative does not itself select another conversation. No cached, arbitrary
or stale thread reference authorizes a read.

For every loaded canonical email, create a typed immutable action context only
from its **own** current Inbox record/action projection, complete native origin,
conversation membership, operation scope and conversation/member lifetime. A
thread message/copy projection is not action admission. A valid member may have
extras even when a different member matched the list filters; show that it is
retained conversation context rather than a matching Inbox result. Account and
identity scope never widen. Missing/unavailable body content alone does not
suppress an otherwise current Inbox email's extras. Physical/Sent-context copies and other members with
no independently eligible Inbox action projection are content-only. Direction
alone neither grants nor substitutes for that record.

Mount the PR #358 renderer inside each eligible loaded email's card, exactly once
per canonical message, with that typed context and its existing dependencies.
Each email owns its analysis, deadline, attachments/results, calendar, automation,
Retry and Delete locally controls. There is no shared top action block, borrowed
member payload, hidden duplicate renderer or per-conversation batch action. The
shared reader may expose one guarded Inbox-specific adaptor/slot; do not copy
handlers or create a second selection/query/refresh controller.

Use one named current-context check at the existing integration owner. Invalid,
missing, old-query, removed, held or foreign member bindings produce no actionable
context. No fallback to active account, sender, subject, array index, row seed or
currently focused email. Native/backend admission and version/CAS remain final.

A same-query refresh may preserve a pending step's minimal captured target and
lifetime when fresh membership still proves the same conversation and that same
email in the same scope with no hold. New row/card objects, page rendering or
reader refresh generation alone do not cancel it; no old payload/DOM is retained
as permission. Failed revalidation, changed conversation/scope/lifetime or lost
member eligibility aborts it visibly under R3. Another member's eligibility never
revives it.

## R2 — unchanged per-email targets, prerequisites and effects

| Existing control family | Required unchanged command/effect and target |
| --- | --- |
| Retry analysis | Existing eligibility and explicit `analysis_requeue` for that email's captured origin message ID; no retry on selection, render or refresh. |
| Attachment Open | Existing `attachment_open` with that message ID and retained attachment part ID; default-app opening only after explicit activation. |
| Connect actions / Resume | The message's explicitly selected provider tuple, capability/version, parameter collection/validation, manifest features, confirmation requirement and `attachment_capability_invoke` payload. Keep original job reconciliation/request-ID rules and structured refusal/reselection behavior. No silent provider substitution or duplicate job. |
| Capability outputs | Existing message/part/job/artifact binding for presentation, preview and opening/export controls. No automatic preview, broader file access, changed output limit or hidden retained preview. |
| Automation confirmation/decline | Existing fire identity, prepared rule/version/state evidence, consent text and `runAutomationDecision` owner. Preserve the refresh-required barrier after a failed decision refresh and all truthful queued/completed/refused outcomes. |
| Calendar create/recheck/decline | Existing message ID, run ID, state/proposal versions and proposal hash, account identity, expiry behavior, invitation warning and confirmation. Preserve the distinction between a saved decision and a completed external write. |
| Delete locally | Existing `deleteInboxItem` and `InboxRemovalClient`, exact origin message ID, local-only confirmation, dual-reader hold and durable completed/deferred/refused results. Selection never broadens deletion to a thread, vendor, sender or account. |

Every table entry applies to the initiating email, never to the row's newest,
matching or focused member. Preserve admitted false/zero/empty parameter values;
do not manufacture identity, version, permission or provider defaults.
Only the read projection/wire/order surfaces listed below change. Existing
mutating command payloads, native admission, consent, versions/CAS and operation
owners do not change; selecting/opening/scrolling runs none of these actions.

## R3 — pending steps and outcomes belong to one email

Trace every operation: eligible per-email context -> captured minimal exact
message/part/provider/capability/job/fire/proposal target and versions -> existing
parameter/confirmation awaits -> same-owner current-context check -> existing
native operation -> target-owned outcome and guarded refresh.

- Check before collecting input and immediately before dispatch after every await.
  A's approval can only dispatch A's original target/parameters/versions/hash, once.
  Clicking/focusing/expanding B in the same current conversation does not retarget
  A or create another message-selection owner; A may proceed only if A's binding
  remains current. Never consult a then-focused or representative email after an
  await. Decline/dismissal keeps its existing semantics; no new consent dialogs.
- A-to-another-conversation, Back, changed query/account/scope, member removal,
  mutation hold or tab/view hiding before dispatch invalidates that UI attempt.
  Returning to the same conversation/tab does not revive its old lifetime. Same
  conversation reactivation and fresh same-query projection alone do not cancel
  A; preserve R1's survival rule and its exact captured payload.
- If approval arrives during same-query refresh, retain one minimal pending step
  and await the existing refresh owner's actual commit/failure outcome. Show A
  waiting/not sent. InboxReload.request is not a join signal. Do not queue another
  refresh, auto-page to find A, retry-loop or demand reapproval due only to render.
  On fresh unchanged membership, dispatch once; on failure/lost eligibility abort.
- After explicit approval/submission, every abort yields a persistent accessible
  **not sent** notice naming A and the real reason, outside hidden/removed cards.
  It survives the causal refresh, remains observable across a tab change, does not
  appear as B's result and does not steal focus. No bare-return approval loss.
- Once dispatch occurred, conversation change, tab hide or card unmount alone does
  not cancel/repeat/falsify it. Keep only minimal target/request bookkeeping until
  the real result is reconciled; no whole old InboxItem, old DOM or preview bytes.
  A completion may update A's keyed state and request the existing guarded refresh;
  it cannot place A's status/result/focus/scroll in B or a different conversation.
- Preserve per-operation in-flight/request-ID guards across remount/selection/page
  changes. Duplicate activation cannot issue another decision/job. Preserve the
  distinction between resuming an active job and a legitimate explicit new action
  after a terminal result. A refresh failure is not proof an external write failed;
  preserve the current decision-refresh and uncertainty/recovery barriers.

## R4 — invalidation, bounded restoration and message-only removal

On invalidation, query/account/conversation change, Back, tab hiding or mutation
hold, immediately clear old body/copy/inventory/details/preview DOM and actionable
contexts. CSS-hidden or disabled stale controls are insufficient. Previous-query
conversation rows may retain a concise Previous results projection but cannot
supply reads/actions. Leaving Inbox follows the existing view owner and closes
its reader; a same-view navigation click is not a new lifetime.

For same-query refresh, retain only bounded user reading intent and minimal
pending targets/lifetime tokens, not old member payloads, cursors, DOM or bytes.
Revalidate the fresh conversation and each relevant member before restoring from
fresh projections. Keep the existing page/count/post-body-read guards and bounded
replay of the user's already loaded span; never page to find a match, removed
representative or hidden action target. Back during refresh still permits current
list commit independently of reader restoration. Resizing causes no reopening or
new query, and background completion does not steal focus.

Begin the existing nested mutation hold before removal; clear both Inbox and
Vendors reader payloads. Ending one hold cannot restore anything while another
remains. Delete locally targets only the initiating canonical email and its
existing logical-unit/copy scope. It never deletes the conversation, another
member, all mail from a sender or an account. Removing a representative can renew
only the same conversation through a fresh eligible member after revalidation.
The conversation remains while other members are retained; its row remains only
if the current Inbox query still matches a remaining eligible member. Otherwise
clear the out-of-query selection truthfully, without saying the whole conversation
was deleted. Last-member removal, deferred/refused removal and retained held bytes
must remain distinct. Clear local history keeps its history-wide scope outside
the reader; selected conversation/filter must never narrow it.

## R5 — bounded layout, keyboard and information parity

Keep content-width-driven wide/split/single presentation and the existing scroll
owners. One right reading wrapper holds the loaded conversation, with per-email
extras and existing body-text scroll protection; no separate message-details
scroll boxes. Narrow mode still exposes the same viewer with usable Back. Long
text/addresses/names/results wrap without horizontal page overflow or clipping.

Preserve list cues, sender navigation, Search/applied scope/Load more and truthful
mixed/partial/no-match states. Focus follows explicit row/actions, stays with a
corresponding per-email control after current renders, and falls back to its
message/reader heading when absent. Hidden regions are absent from keyboard and
accessibility navigation. R0a/R0b govern header and Filters geometry; do not use
outer overflow suppression to meet a screenshot.

## Required future change surface and non-scope

After exact acceptance and separate implementation authorization, permitted owners
are the existing Inbox query/read projection in db.py/engine_api.py and matching
Rust/TS read wire/decoders; shared thread_reads message order/cursor; existing
entry/history/refresh integration; one message-reader adaptor and PR #358 details
renderer; header/Filters/main markup/styles and affected tests. Define explicit
closed conversation/member projections, required complete selectors and order
identity at those boundaries. Missing/old flat-message shapes fail closed; no
client guessing/grouping or permissive legacy fallback. Mutating native commands
remain unchanged.

No new provider verb, reply/compose, automatic action/analysis/body fetching,
identity derivation, capture/retention/attribution rule, storage schema migration,
CSP/dependency/workflow/model change or action authority on arbitrary historical
context. A non-Inbox member has no extras merely because another member does.
Vendors uses the same reader/order while retaining its read-only action entry.
If new persistent state, identity rules or mutation APIs are necessary, stop for
another contract amendment rather than widening this one silently. Windows issue
#360 and other unrelated work remain separate. No live app/config/timer/mailbox,
local application build/package/install/promotion or human LCS records here.

## Staging and acceptance gates

1. This PR changes only this plan and the marked selector/desktop-view clauses in
   THREAD_VIEW_CONTRACT.md. New exact-head oversight/connector/CI and Juan's exact
   acceptance are required. Green CI or an older PASS is not permission to merge.
2. Implementation requires separate direction. Header first, including all-view
   geometry and Inbox height proof. Then the authoritative conversation projection
   plus unified reader/per-message extras with the preserved safety guards.
3. Use stacked reviewable implementation PRs if the combined diff exceeds about
   1,000 lines including tests. Typed projection/guard stages may land with old UI
   intact; switch the list/viewer coherently only when all controls and narrow flow
   work. No duplicate rows/actions, wide-only stage or temporarily dropped family.
4. Each implementation PR requires actual-owner regressions, affected/full desktop
   checks, relevant Python/Rust/read-wire checks, type/lint/format, cold/gap audit,
   Chromium/WebKit source-only evidence, exact-head independent review/CI and Juan's
   PR-specific merge approval. Build/install/promotion stays a separate decision.

## Required verification — future proof, not results of this document

| Boundary | Both sides that must be proven |
| --- | --- |
| Header/Filters | All views at fixed viewport/text/zoom/scroll keep header bounds and reachable labels. Repeated Filters closed/open/closed preserves outer extent, pane bounds, sender/message/reader scroll, selection/drafts and zero extra reads, including already-scrollable/short layouts. Preserve every field, busy/admission state, Apply/Search/Reset/Escape/Close and focus. |
| Conversation identity | Singleton, one multi-member thread, several unrelated chains from one sender/vendor, mixed senders, equal subjects/native IDs across accounts/identities, aliases/copies and IMAP merges. One row per scoped conversation, no duplication across list-page boundaries. Never group by sender/subject. |
| Filtering/counts | Non-newest-member match; all predicates on the same eligible member versus cross-member false positives; watched-sender views and All; only-context/no-Inbox match refuses. Counts/page limits describe conversations after grouping, independently of member totals. Correct singleton, empty and last/representative-member removal. |
| Reader/paging | Inbox and Vendors use the same newest-first order, canonical ties and unknown-date placement. Exact page boundary, complete older append without gaps/duplicates on valid cursors, no old-page load without the explicit click. Concurrent arrivals exercise snapshot order versus stale rejection and explicit restart under the shared contract; old-order/wrong-scope/invalid/stale cursors refuse. An older matching member never causes automatic paging. |
| Per-email extras | Two admitted members A/B each show their own payloads and controls once. A's approval/completion cannot target or decorate B in the same conversation. Context/Sent copies without a current independent Inbox projection are content-only; an eligible record is not inferred from direction or body. No top Selected-message block or copied handler. |
| Pending/visibility | Pause every async action for A; focus/read B in the same conversation, repeat same-row activation, refresh before/during approval, switch conversation/Back/query/account, remove A and hide/show tabs. Unchanged A survives allowed refresh; changed/hidden context refuses with A-specific not-sent notice. Run tab hide both before dispatch (no command) and after dispatch (one real outcome, no hidden/wrong card/focus). Returning cannot revive cancelled lifetimes. |
| Actions/removal | Preserve exact IDs/provider/parameters/versions/hash/consent, stale-CAS and decision-refresh recovery, duplicate-click guards, uncertainty/resume versus legitimate repeat. Nested holds clear both readers; removing one email keeps other members and correct filtered-row eligibility. Clear remains global and no provider mail is mutated. |
| Browser/accessibility | Actual production owners/IPC mocks in Chromium and WebKit at 1920x1080,1524x785,1440x900,1280x720,1024x768,640x520,320 CSS px, short heights and 200% text/zoom. Pointer/Enter/Space opens once; actions/links/disclosures/text selection/drag/form controls do not open a row. Same-origin no duplicate read/lifetime reset; Back exact target/fallback; body/copy/preview byte clearing and scroll/focus parity. |

Reuse existing fixtures, not a duplicate rule in tests or the installed app. Record
exact source/harness/browser versions/settings and all warnings. Add fail-before/
pass-after and negative/mutation proof for new guards or defects; explain any
equivalent survivor. Source-only browser evidence is not packaged IPC/CSP or
installed-product qualification. For this documentation PR run only document
checks, including the existing Thread contract seam checker, not application
behavior suites. Proposal complete is not accepted/implemented/merged/installed.

[identity]: ../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys
[thread-view]: ../docs/THREAD_VIEW_CONTRACT.md#thread-view-desktop
[inbox-query]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/src/eom_email_watcher/db.py#L14356
[query-api]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/src/eom_email_watcher/engine_api.py#L2598
[query-host]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src-tauri/src/lib.rs#L1615
[query-types]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L161
[members]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/src/eom_email_watcher/thread_reads.py#L334
[message-pages]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/src/eom_email_watcher/thread_reads.py#L129
[cursor-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/src/eom_email_watcher/thread_reads.py#L236
[reader-page]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/threadViewDom.ts#L293
[details-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L1807
[view-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/threadHistory.ts#L24
[header-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/main.ts#L516
[shell-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/styles.css#L39
[layout-owner]: https://github.com/canfieldjuan/eom-email-watcher/blob/e7e5e0ba9d4a6fc13a3266b06f16a8a09ce66e6e/desktop/src/inboxThreadEntry.ts#L178
