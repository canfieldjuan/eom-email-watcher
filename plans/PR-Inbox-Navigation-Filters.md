# Inbox navigation and visible filters

Status: **proposed for oversight review and Juan's exact-revision acceptance**, 2026-11-06. Juan authorized publishing this docs-only proposal. It is not acceptance of the redesign or permission to implement, merge or install it. The earlier unpublished navigation commit `e39c32d39439481fa19f4b6847d8ec4f4b94c906` remains frozen and is evidence only.

Baseline: `main` at `19ca82287dfd6d62dcd20fee7de3335123e298a9`. The Inbox thread-entry stack (#343–347) is merged. PR #347's reviewed head, `381c3bbccd719076a887c81572666944ecf51b08`, includes the return-to-Inbox button-availability correction. This proposal builds on that completed behavior.

References: [Two-Column Inbox](PR-Two-Column-Inbox.md), [Wide, Compact Inbox](PR-Wide-Inbox.md), [Inbox thread entry](PR-Inbox-Thread-Entry.md), [Thread View Contract](../docs/THREAD_VIEW_CONTRACT.md) and the [deferred three-pane design](PR-Inbox-Three-Pane-Reading.md). This proposal isolates the navigation/filter portion; it does not accept the entire deferred design.

## Root cause and current capability

The long watched-address list has no search or scrolling boundary, so it extends the whole page. Full addresses are required by the accepted Two-Column Inbox contract and must remain available. The existing filter disclosure hides both keyword search and active criteria. The user can lose track of whether the current list is scoped by an exact watched address, a substring filter, an account or another criterion.

Current source owners:

- `desktop/src/inboxSenderNav.ts` resolves exact selection from the complete watchlist, handles removal/error fallback and labels the current scope.
- `main.ts`: `renderInboxSenderNavigation`, `selectInboxSender`, `queryFromInboxControls`, `commitInboxQueryFromControls` and `renderMailAccounts` own navigation, filter drafts, committed query and account reconciliation.
- `loadInboxPage`, `runInboxRefresh`, `InboxReload` and the query/mailbox generations own paging and refresh. `InboxThreadEntry` owns current-row eligibility and Back/return focus.
- `styles.css` uses two natural-height Inbox columns and stacks them at 760px. There is no persistent third reading pane yet.

The next slice improves finding and understanding messages. It deliberately leaves message expansion, saved actions and the reviewed full-thread entry intact. This is the first step toward the three-pane direction Juan described.

## Required behavior

### 1. Searchable, contained watched senders

- Add a labelled **Find watched sender** input above the navigation. Match literal, case-insensitive name/address text in the already loaded watchlist; preserve the existing order. No regex, provider fetch, mailbox search or watchlist edit.
- Typing or clearing the finder changes only navigation visibility. It does not change `sender`, `sender_query`, any Inbox predicate, cursor, query epoch, row expansion or open thread. Enter here must not accidentally submit the message-search form.
- Rename the unfiltered navigation action **All senders**, with helper text **All retained messages in the selected account scope.** It still means `sender: null`; label-admitted mail and captured Sent replies remain eligible. Other predicates remain in force.
- Keep All senders and the finder outside the scrolling result region. Keep a selected address visible when the finder would exclude it, without duplicating it in results. Selection validity is computed from the full watchlist before filtering.
- Preserve full addresses, wrapped when needed. Names supplement addresses; neither permanent ellipsis nor tooltip-only text replaces them. Keep exact-address selection and `aria-pressed`; never select by display name, domain or substring.
- Put the result list in one vertical scroll region. Proposed cap: at most 32rem or 60% of the viewport height, whichever is smaller, adjusted downward when necessary to keep controls usable. No minimum height may force clipping on short windows. Sender count must not increase the outer page height without bound.
- Distinguish no finder matches, an empty watchlist, and a failed watchlist refresh. Failure preserves the selected sender; a successful removal of that sender falls back through the existing All/query-reset owner. Clearing the finder reveals the complete list.

### 2. Compact search and visible committed scope

- Keep **Subject or summary** search visible beside Search and Filters. Search/Enter explicitly commits the same controls as Apply; retain the existing literal matching, 200-character input bound and no query on each keystroke.
- Filters is a compact disclosure containing **Sender contains**, account, priority, topic, status and page size. The wording distinguishes substring `sender_query` from exact watched-sender selection. Preserve the existing 320-character substring input bound, options and defaults.
- Show committed keyword, sender-contains, priority, topic and status as removable labels. A label is derived from the committed query, never an unsubmitted draft.
- Removing one label changes only that committed criterion and its matching draft field. It must not commit other draft values, silently change the account, reset the selected exact sender or acknowledge any action. Close/open Filters preserves all drafts.
- Always show the committed account scope. Active displays the currently resolved account; if none exists, show that fact and retain the current unavailable-account refusal. Active has no remove control; selecting its label reveals/focuses the account selector. Removing an explicit or All-accounts label returns to Active, not to All accounts.
- Show page size as, for example, **25 per page**, separately from removable predicates. Reset keeps its current meaning: restore form defaults, including Active account and 25 rows, while preserving exact sender selection.
- Search and Apply continue to commit all draft controls together. All senders changes only the exact sender. Every predicate still combines with AND.
- Labels describe the committed requested query. If an older query's retained rows are still displayed during replacement or failure (`inboxItemsEpoch !== inboxQueryEpoch`), identify them as **Previous results** beside the existing status. Do not imply that they match the new labels, discard them as a workaround, or enable their Open thread controls. This annotation is an explicit additional UI proposal for review.

### 3. Keep the full-thread connection stable

- Preserve the merged #347 implementation at the baseline above. Finder-only changes must not invalidate the reader or fetch thread data. A real query/sender/account commit still closes and invalidates it through the existing owner.
- Open thread remains an explicit action alongside current row expansion. Back restores the same finder text, sender-list scroll, filters/drafts, rows, expansions and exact-origin focus (heading fallback).
- Preserve complete provider/account/mailbox identity, current-span eligibility, vendor choice and fresh-origin Retry, nested holds, deletion barriers, request generations, text-only body/copy display and existing CSP.
- Returning to Inbox after a hidden-tab refresh must recompute entry-button availability; consume #347's repair rather than introducing a second availability owner here.
- Load more remains explicit and keeps its current cursor, ordering, page-size and deduplication behavior. This slice does not change the thread timeline, body expansion or action/confirmation owners.

## Small-screen and keyboard behavior

Keep the existing two-column layout and 760px stacking point for this slice; the full three-pane width/breakpoint change is later. At 640×520, 320 CSS pixels and 200% text, controls may wrap and the outer view may scroll when necessary, but the watched-sender result region remains bounded. No horizontal document overflow and no clipped Apply, Reset, Load more or Clear local history.

Use native buttons, inputs and a disclosure, clear labels, visible focus and a named scroll region. Finder typing keeps focus in the finder. Sender selection preserves its trigger where possible. After removing a filter label, focus moves to the next available label or the Filters control; no focus falls into a hidden/removed element. Loading/error/no-match changes are announced once politely, not once per sender. No listbox containing action controls, new shortcuts or focus trap.

Reuse the existing palette and type. Reduce the weight/space of the filter disclosure and use hierarchy and separators; do not add another layer of large cards or invented unread/count badges.

## Explicit contract changes and non-scope

The acceptance request is limited to searchable/bounded watched navigation, All senders wording, compact keyword/Filters controls, visible committed labels and Previous results annotation. If accepted, this plan supersedes only the relevant presentation choices in [Two-Column Inbox](PR-Two-Column-Inbox.md) and [Wide, Compact Inbox](PR-Wide-Inbox.md): the All messages label, the unfiltered/unbounded sender list, placing keyword search inside the collapsed filter panel, and leaving committed scope hidden behind a static filter summary. The new labels sit outside the disclosure; they do not turn its summary into a filter count. Exact sender, full-address visibility and all query/data rules remain. The original plan files and the Thread View Contract are unchanged in this docs-only PR.

No backend/Rust/schema/index/API/capture/retention/dependency/CSP/workflow change. No watchlist management redesign, message counts, unread state, Gmail-label navigation, automatic paging, body search, reply/compose, bulk actions or new action vocabulary. No application build/install/live-settings access locally. GitHub CI builds and packaging remain authorized, including for this docs-only review PR; the local build/install restriction remains in force.

Do not move saved analysis, attachments, capability actions, calendar/automation decisions, deletion or expiry timers into a new reader in this slice. Do not merge the frozen navigation commit wholesale: its stale base, prior authorization statement and old validation do not establish current compatibility or acceptance.

## Implementation sequence and dependencies

1. Submit this docs-only proposal for exact-head oversight and connector review; obtain Juan's acceptance of the final reviewed revision before redesign code. Refresh current main, worktree and live-owner checks before implementation. Reassess any overlapping `main.ts`/`threadHistory.ts` changes without altering another session's branch. A plan or review acceptance does not replace Juan's separate approval for this PR's merge.
2. After plan acceptance, on an isolated branch from the verified merged dependency, implement sender finder/pinning/scroll containment and focused model/DOM tests. Treat e39c32d only as a source to compare against current owners, with no automatic cherry-pick or inherited qualification.
3. Add compact search/Filters and committed-label transitions through the existing query owner. Preserve drafts and #347 entry invalidation; add real-handler integration/race tests. If the combined patch becomes difficult to review, split navigation and filters into consecutive PRs without changing the approved semantics.
4. Run the tests below, submit each authorized UI PR to oversight at its exact head, let GitHub CI run, and wait for separate Juan approval for each merge. No automatic merging or installation.
5. Later, under a separate accepted plan, embed the existing thread reader into a persistent third pane and move selected-message details/actions without duplicating their owners. That slice changes current multi-row expansion and Back/layout behavior and needs its own acceptance. It also supplies full viewport-contained message and reading panes, tablet/phone pane navigation, and native UI qualification when authorized.

## Verification plan (future evidence, not results)

| Boundary | Required proof |
| --- | --- |
| Finder | 220+ senders; long addresses; same name/different addresses; literal special characters; case-insensitive name/address matching; order unchanged; no-match/empty/error states; selected nonmatch pinned once; zero Inbox/thread/provider calls on finder edits. |
| Exact selection | `bob` vs `jimbob`, display-name twins, All retaining non-watched/label/Sent rows, successful selected-sender removal, failed refresh retaining selection. |
| Draft/committed filters | Every removable predicate; keyword removal while draft account/priority differ; account removal back to Active; missing active account; Reset retaining sender; All retaining secondary criteria; page-size preservation; Search and Apply/Enter equivalence; finder Enter isolation. |
| Refresh and entry | Hidden-tab refresh then return; same-query complete-span revalidation; secondary-query old rows labelled Previous results and unable to open; account/sender switch clears rows; both stale-response orders; Load more unchanged; nested holds; Back restores finder/scroll/drafts/expansion; no late thread reopen. |
| Existing actions | Existing desktop regression suite unchanged in meaning; unselected expiry/action updates, confirmation and retry fences, pending/completed/deferred removals remain owned by existing code. |
| Layout/accessibility | 1920×1080, 1524×785, 1024×768, 640×520, 320 CSS pixels and 200% text. Verify sender count cannot grow the page indefinitely, long full addresses remain accessible, disclosure/focus/scroll/labels work with keyboard, and no horizontal overflow or clipped controls. |

Add fail-before/pass-after behavioral tests at source; retain existing thread/removal/query tests. Use installed dependencies for Node tests and TypeScript noEmit, plus synthetic browser checks without building/installing the application. GitHub CI owns authorized builds/package/native checks. Record any native/platform gaps without claiming synthetic browser checks prove them.

## Required change surface, assumptions and conflict check

This PR adds only `plans/PR-Inbox-Navigation-Filters.md`. Future implementation belongs in `desktop/src/inboxSenderNav.ts`, `desktop/src/main.ts`, `desktop/src/styles.css` and focused desktop tests. A small pure filter-presentation helper may be introduced if it makes draft/committed transitions testable; it must not become a second query or lifecycle owner. The existing `InboxReload`, `InboxThreadEntry` and `ThreadHistory` owners remain authoritative.

The pre-publication check found the source worktree clean at #347's reviewed head and verified `origin/main` at the merged baseline above. The recorded heads/statuses of 26 existing checkouts were unchanged, including unrelated dirty benchmark/model work, which was left untouched. The other owner session's latest completed activity was Local Connect Status qualification, with no active overlapping Inbox work observed. This is a point-in-time check, not a pause or ownership claim over other sessions.

Open PRs #314 (model/schema), #311 (Windows admission diagnostic) and #237 (attachment contract) do not touch this plan path. A new isolated docs-only worktree and branch hold this proposal. Repeat the overlap check for implementation, whose application files have a wider conflict surface.

## Implementation summary

Documentation only. Compared the proposal with current source, the accepted Two-Column/Wide Inbox requirements, the merged thread-entry behavior and the deferred design. No new navigation/filter application code or deployment is part of this PR; the frozen navigation work is not imported.

## Cold diff audit

The diff contains one new plan, with explicit presentation proposals, preservation requirements, staged implementation and observable acceptance tests. It does not alter the accepted thread contract or authorize the later persistent reading pane/action migration. PR #347's return-tab correction is already in the baseline and has no duplicate repair here. Documentation checks cover relative references, the single-file change surface and whitespace; the matrix above describes future evidence, not tests already run for this proposal.

## Gap audit

NOT DONE for redesign acceptance or implementation. Exact-head oversight/connector review and Juan's acceptance of the final reviewed plan remain pending. Any semantic review correction must be included in the revision he accepts. Each PR also requires green CI, clean connector review, zero unresolved review threads and Juan's PR-specific merge approval. Packaged/native qualification and installation remain separate gates; no local build or install is authorized.
