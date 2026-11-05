# Inbox: resolve a retained message and open its thread

Status: **contract proposal prepared for oversight review — awaiting Juan's exact-revision acceptance**.
Source baseline: `main` at `3ed72aec73672a4334f5fcd949b7c60198d40ef8`.
Local navigation/filter base: `e39c32d39439481fa19f4b6847d8ec4f4b94c906`.

Juan chose: **“We should connect the full thread first.”** This authorizes this
contract-first proposal, not acceptance of the API/interaction choices below.
Juan subsequently instructed “Let’s send it to oversight for review.” That authorizes
this docs-only review PR. Thread-connection implementation still awaits acceptance;
merge requires Juan's approval for this PR. No build, package or install is authorized.
This plan supersedes the message-details-first sequencing proposed in
[the three-pane plan](PR-Inbox-Three-Pane-Reading.md). Its P1/P2 local implementation
is preserved; its P3 reader must become thread-first under this proposal.

## Root cause and evidence

An Inbox row is a retained logical message, not a thread selector. The UI currently
has no safe way to turn that selection into M3's scoped thread plus vendor context.

| Current owner | Evidence and boundary |
| --- | --- |
| `Store.query_inbox`, `db.py:14352`; typed `InboxItem` in Python/Rust/TS | Reads `logical_messages` and returns message/provider/account plus analysis/actions. It does not project effective mailbox identity or a thread-entry binding. Rust's existing top-level provider/account fields have legacy defaults; those are insufficient evidence for a new reader. |
| `_effective_message_identity_sql`, `db.py:3870` | Uses stored identity, or proven legacy continuity for that provider/account; otherwise identity is unresolved. Active account identity is not a substitute. |
| `logical_messages`, `_logical_unit`, `message_derived` | Canonical message visibility, copies, direction and attribution already have owners. Inbox and M3 both use the logical projection. |
| `thread_reads._selected_thread`, `_members`, `_selected_message` | M3 validates complete `ThreadRef`, resolves scoped IMAP aliases, and requires the selected vendor to be represented. Body/copy selectors require a visible canonical message. |
| `body_state.project`, removal RECORD/CONTENT owners | Existing owners decide availability before body text or unavailable markers are read. |
| `ThreadReadClient`, `ThreadViewState`, `ThreadViewDom` | Reusable local reads, paging, explicit body/copy expansion, generations and safe text rendering. Current entry starts at a vendor; it has no Inbox-origin guard. DOM IDs and navigation labels currently assume one vendor reader. |
| `main.ts`: `loadInboxPage`, `inboxReload`, `refreshThreadHistory`, mutation/view handlers | Existing query epochs, mailbox-operation revisions, removal holds and lifecycle invalidation must cover the new entry and reader too. |

The binding must originate at the selected row, not a sender lookup, a subject
comparison, an active-mailbox guess, or a scan of every vendor's thread lists.
Metadata queries must retain explicit provider/account predicates and exact effective
identity/thread membership; measure large synthetic histories before considering any
index/schema change. Do not broaden a query and rely only on client-side filtering.

## Contract owners and intended experience

This plan specifies new read mechanics and entry UX under the existing definitions:
[D-identity](../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys),
[D-attribution](../docs/THREAD_VIEW_CONTRACT.md#d-attribution-a-messages-vendor),
[D-follow](../docs/THREAD_VIEW_CONTRACT.md#d-follow-followed-threads),
[D-derived](../docs/THREAD_VIEW_CONTRACT.md#d-derived-derived-state-and-invalidation),
[D-body](../docs/THREAD_VIEW_CONTRACT.md#d-body-stored-bodies),
[D-ops](../docs/THREAD_VIEW_CONTRACT.md#d-ops-operation-classes-and-gating),
[D-removal](../docs/THREAD_VIEW_CONTRACT.md#d-removal-deferred-local-deletions), and
[the accepted M3 plan](PR-Thread-M3-Read-View-CSP.md).
It does not amend those definitions. Historical acceptance-status paragraphs in
those documents predate subsequent accepted/merged M3 work; they are not a new gate
on the already implemented body states. This proposal neither rewrites that history
nor broadens body eligibility.

- Selecting an Inbox message opens the **retained thread** in the reading pane after
  resolution. It does not first require a message-details-only redesign.
- “Full thread” means all eligible locally retained logical messages in that one
  conversation, available through M3 paging. It does not mean recovering uncaptured
  mail, fetching every body at once, or claiming the provider conversation is complete.
- The message-list predicates stay in force for that list. The reader explicitly says
  **“Thread history includes other senders and messages outside these Inbox filters.”**
  Its account is fixed to the selected row, including when the Inbox shows All accounts.
- Keep M3's newest-subject heading, oldest-first timeline, history/Sent notices,
  explicit Load more, copy inventory, body expansion, truncation and no-text states.
  Do not automatically load every page or expand body text when selecting a row.
- Mark the originating message when its canonical ID is on a loaded thread page.
  If it is on a later page, show “Selected message is not on the loaded thread page”
  with the normal Load more control; do not claim that it is absent from the thread.
- Existing saved analysis/actions remain available in a clearly labelled secondary
  **Selected message details** section for the originating Inbox row. They keep their
  current owners/targets; thread cards do not acquire new automation/action controls.
  If no eligible vendor thread exists, show that outcome and still offer these details.
  A summary is never substituted for an email body.

## R1 — a complete Inbox message reference

Add a **required** `thread_entry_ref` field to each `inbox.query` item:

```ts
type InboxMessageRef = {
  provider: string;
  account_id: string;
  mailbox_identity_key: string | null;
  message_id: string;
};
```

The field is projected from that canonical `logical_messages` row and its effective
identity using the existing SQL owner. The row fields and reference must agree.
The UI captures this immutable reference from a current committed query page;
it must not construct it from account controls, display text or default values.

The nested object requires every key; explicit null identity is meaningful. Missing
identity, omitted provider/account, extra keys or wrong types are not normalized
into a broader selection. Provider/account/identity validation uses existing M3
validators; the message ID is the exact nonempty retained identifier, never trimmed
or case-normalized for lookup. A new engine/host pair ships this typed projection
together; an older/malformed response missing the reference fails closed rather
than synthesizing one. This is an API projection change, not a storage migration.

A null identity matches only the currently unresolved effective-identity bucket.
If continuity becomes proven after the Inbox page was read, resolution requires
refreshing the Inbox reference. A known identity is never downgraded to null.

## R2 — one local resolver and explicit vendor choice

Propose engine operation `threads.resolve` and host command `threads_resolve`:

```ts
type ThreadResolveQuery = {
  message_ref: InboxMessageRef;
  vendor_id?: string;       // explicit choice from the returned vendor candidates
  limit?: number;          // candidate paging only; default 25, integer 1..100
  cursor?: string | null;  // candidate paging only
};
```

Unknown fields are rejected. With `vendor_id`, paging fields must be absent; that
form revalidates the choice rather than ignoring a cursor. Without `vendor_id`, a
cursor is bound to the same reference/limit and the complete candidate projection.
Malformed or wrong-selector cursors return `invalid_request`; changed candidate
scope/ownership/names return `stale_cursor` and restart the chooser once visibly.
Validate a non-null cursor before returning any alternative state, including when
an earlier multi-vendor result has become a single candidate.

In one query-only SQLite snapshot, the resolver:

1. Finds the canonical RECORD-visible row by exact provider/account/message ID and
   compares its effective identity, with null-safe equality, to the supplied one.
2. Derives its current complete thread key/scope and logical membership using the
   existing owners. Resolved IMAP aliases must stay within provider/account/identity.
3. Derives eligible vendor contexts from current visible message attribution in that
   thread and the existing follow owner. The seed itself may be unattributed.
4. Returns metadata only, from that same snapshot. No bodies, saved summaries,
   attachment inventory, provider/model calls, reconciliation, repair or migration.

The response is a strict tagged union (required fields, no mixed alternatives):

| State | Fields in addition to `state` | Behavior |
| --- | --- | --- |
| `resolved` | `message_ref`, `vendor: VendorRef`, `thread: ThreadItem` | One eligible vendor, or a currently valid explicit vendor choice. The existing ThreadItem carries full scope, current subject/count, owner and selected relation. |
| `choose_vendor` | `message_ref`, `thread_ref`, `owner_vendor`, `items: VendorRef[]`, `next_cursor` | More than one eligible vendor context for the same conversation. Require user choice; do not silently pick the first, owner, or seed's attributed vendor. Label owner versus linked contexts. |
| `unavailable` | `message_ref`, `reason: "not_vendor_related"` | The canonical message exists but its thread has no current eligible vendor relationship. Say “No vendor-related retained thread is available for this message.” Do not say no conversation exists. |

Candidate pagination is deterministic by vendor ID byte order, uses the existing
bounded cursor mechanism (at most 4096 bytes), and binds the resolved reference,
seed reference, owner, candidate IDs/names and ordering. It must not infer uniqueness
from a page length or truncate the candidate set into an automatic selection.

Errors carry no thread/body data:

- `invalid_request`: malformed input or forbidden fields, not a broader retry.
- `not_found`: no visible canonical message in the requested provider/account;
  this includes stale physical-copy IDs and RECORD-excluded rows.
- `stale_selection`: the visible row's effective identity changed, or an explicitly
  chosen vendor is no longer eligible. Clear the reader; refresh the Inbox or chooser.
- Existing sanitized internal error: inconsistent derived membership/ownership or
  contradictory thread mapping. Never silently classify corrupt/missing derived
  data as an ordinary non-vendor message or offer several guessed thread matches.

Read unavailability does not settle a removal operation. Existing durable removal
status continues to say pending/completed/deferred independently and truthfully.

## R3 — duplicates and guarded follow-on reads

Inbox already lists canonical logical messages. The resolver does not invent a
second deduplication rule. Physical copies remain under M3's copy inventory; an
input ID that has become an alias after canonical promotion is stale and requires
a fresh Inbox selection. Do not redirect it based on Message-ID, sender, subject,
source-provider ID alone or an arbitrary `logical_of` lookup outside scoped visibility.
Messages lacking Message-ID retain D-identity's current best-effort behavior.

A canonical seed has one current complete thread identity. Multiple vendor contexts
are the chooser case; contradictory thread identities are an error, never a chooser.
IMAP merges may resolve through the existing alias owner only within exact mailbox
scope and with the seed still a visible member of the resolved survivor.

Extend `threads.messages`, `threads.body` and both `threads.copies` query forms with
an optional **`inbox_message: InboxMessageRef`** guard. Absence retains existing
vendor-entry behavior; an explicitly supplied null/partial/malformed object is
rejected. Inbox-origin state requires the guard on every request, retry and page.
No client/host layer may silently strip it or fall back to an unguarded request.

In the same snapshot as each projection, validate both:

- the supplied thread/vendor selector through M3's current owner; and
- the originating message's current canonical RECORD visibility, exact effective
  identity and membership in that resolved thread.

A body/copy target may be another message in that thread, but still passes M3's own
canonical membership and operation-specific eligibility checks. Body text additionally
goes through the existing CONTENT/body-state owner. The origin guard bypasses none
of these checks and does not redefine saved-copy-summary eligibility. Bind the guard into message/copy/inventory cursor selectors and
revision inputs; a cursor from a different seed or vendor entry cannot be reused.

When origin membership is gone, clear the Inbox reader rather than showing remaining
thread content through the stale Inbox link. Do not automatically choose another
seed, vendor, mailbox, alias or new thread. A valid same-scope IMAP survivor is the
only existing alias behavior retained here. Current ownership/relation metadata
must come from the new response, not an old resolver card. Invalidated vendor choices
return to resolution; no automatic substitution of the remaining candidate.

## R4 — body/removal/security boundaries

No new body state is introduced. The current D-body and `body_state.project` owner
remain authoritative, including unresolved mailbox identity for unattributed visible
messages, removal precedence, and explicit no-text alternatives. The existing
accepted extension is already implemented on the baseline.

A null-identity vendor thread may resolve and display its eligible headers, with
M3's identity-unavailable notice and no body text. A disconnected retained account
may still resolve. Neither case borrows the currently active account's identity.
Non-vendor messages do not become body-eligible by entering this UI.

RECORD-held delete/clear targets cannot resolve, seed a chooser or authorize a child
read while their bytes remain. CONTENT-held unfollow cleanup cannot expose held body
bytes or body-unavailable marker data through a new endpoint. Resolver reads select metadata only; subsequent
body/copy projections keep their existing owners. Existing eligible saved analysis
remains distinct from body storage and is never a substitute for inaccessible text.
No direct messages/body-table shortcut may bypass logical visibility.

The operations remain local read class with current startup admission checks, no
Connect entitlement gate and no provider/model/network side effect. Existing paid
capture/body-storage rules are unchanged. Preserve M3's production CSP, text-only
DOM sinks, strict body union, length bounds, escaped copy metadata, and explicit body
expansion. No HTML, markdown renderer, remote image, attachment download or automatic
link activation is added. New error logging must not include message text or identifiers.

## R5 — entry state, lifecycle and reading-pane integration

Use the existing `ThreadReadClient`/`ThreadViewState`/`ThreadViewDom` projection and
rendering owners, extended for an Inbox origin. Do not call `selectVendor` as a
shortcut that lists every thread before opening the selected one. Provide a direct
resolved-entry transition that sets validated vendor, thread and required origin
guard together, clears child state, then requests that thread's first message page.

The Inbox and Vendors may have separate state instances of the same reusable reader.
Give each DOM instance a stable unique prefix for headings, regions, controls and
focus keys; hidden vendor controls must not share IDs with the Inbox reader.
Inbox entry uses Back to messages and its fixed account label, not Back to vendors
or an account picker that silently changes the guard's scope.

The Inbox selection owner binds requests to the exact message reference, committed
query epoch, entry/vendor-choice generation, mailbox-operation revision, and visible
view. Invalidate before notifying/rendering. A superseded resolver, chooser, message,
body, copy or inventory response cannot commit data or trigger another read.

Preserve the original plan's query/action safeguards, applied to the thread reader:

- Query commits, exact-sender/account changes, reset, accepted deletion/clear and
  configuration invalidation clear old reader data immediately. Previous-query rows
  retained after a failed filter refresh are labelled and cannot open a reader.
- Load more appends the existing Inbox cursor page and preserves current selection.
  It does not open a row or restart the thread. Same-query refresh clears child data
  while revalidating the selected source row; an absent selected ID in the loaded
  span becomes unavailable, not a claim of deletion or a reason to auto-page.
- Account/catalog/verified-identity, vendor, watchlist, inbox-span and relevant
  configuration changes must invalidate/revalidate the Inbox entry as well as the
  current Vendors reader. Preserve mailbox-operation effect fencing and mutation
  begin/end nesting. Centralize lifecycle dispatch so one surface cannot be missed.
- Hiding a view suspends its reads and clears bodies/copies. Returning, focus refresh
  or retry revalidates the current origin before displaying retained content again.
- Existing removal holds apply to all reader instances before writes begin. Pending
  outcomes survive restart through the existing status owner. Late async completions
  cannot reopen held or removed content.
- Retain one owner for saved-analysis actions, confirmations, expiry timers, pending
  decisions and refresh fences. Selection alone performs reads, never an action.
  Actions in Selected message details remain bound to that row; in-flight completion
  cannot transfer to another selected row or any other thread member.

Use the accepted direction of three contained panes: searchable watched senders,
filtered message list, retained-thread reader. Preserve P1/P2 in `e39c32d` and all
existing filter/account/page-size meanings. The responsive layout still reduces to
two/one pane with explicit navigation, preserves selection/scroll, exposes Load more,
and keeps full addresses and controls usable at narrow widths and enlarged text.

## Implementation slices after contract acceptance

1. **Scoped projection/resolver:** add required Inbox reference and Python resolver;
   document the API, strict unions, candidate paging and errors. Prove scope,
   canonical visibility, metadata-only reads and removal barriers. No reader UI yet.
2. **Guarded M3 reads:** add optional origin validation to the shared message/body/copy
   owners and cursor bindings. Preserve all vendor-entry behavior. Prove deletion,
   identity changes, promotion, alias and stale-cursor cases before host wiring.
3. **Typed host/client and entry state:** add strict Rust/TS types, read-class command,
   complete forwarding, resolver/chooser state and direct guarded entry. Wire every
   lifecycle/mutation owner to both reader instances. No raw/untyped fallback.
4. **Thread-first pane:** connect Inbox row selection, vendor chooser, source context,
   the reused M3 reader and secondary saved-analysis/actions; namespace DOM IDs and
   preserve action ownership. A successfully resolved row opens a thread here.
5. **Contained layout and qualification:** finish three/two/one-pane geometry,
   accessibility, large-history tests and synthetic native proof under production CSP.

Keep each change reviewable; split a slice further if its diff mixes independent
owners. Recheck main, worktrees and PR overlap before each implementation branch.
The prior local navigation commit is a frozen dependency, not a submitted or accepted
PR. Preserve it or deliberately carry it forward only within later authorized work;
do not overwrite it or publish it as a side effect of this docs task.

## Acceptance tests and evidence

| Boundary | Required proof |
| --- | --- |
| Inbox projection and strict wire | Every reference field present and row-consistent; explicit null identity succeeds; Python/Rust reject missing/null-object/extra/wrong-type fields. TS types require the reference and forwarding retains invalid runtime fields for host rejection. No legacy provider/account default manufactures a new selector. Older response fails closed. |
| Account/identity | Same native thread ID and source-message lookalikes across providers/accounts and reused account IDs with distinct mailbox identities stay separate. Test a null legacy bucket beside known identities and null→proven transition. Assert exact returned seed/thread/vendor and both provider/account predicates; removing either filter must fail the tests. |
| Vendor contexts | Single vendor, multiple linked/owned vendors, unattributed seed, outbound seed, seed's vendor different from owner, no vendor, renamed/deleted vendor. More than 100 candidates page without omission or false uniqueness; changed choices/owner/name stale the chooser cursor. A stale explicit choice never silently falls back. |
| Duplicates/aliases | Canonical plus physical/location-only copies appears once; physical-copy input and canonical promotion require refresh; no-Message-ID best-effort remains. IMAP survivor/alias, equal provider keys in another identity, missing/cyclic alias and corrupt derived mapping fail safely or resolve only the correct survivor. |
| Every child operation | Guard present on message/body/copy/inventory first page, append, retry and stale restart. Forged/wrong-scope/removed seed cannot authorize a valid unrelated thread. Guard-bound cursor cannot move between seeds or into vendor entry. Same-snapshot assertions cover check/projection races. |
| Removal/body states | Held 33-row logical delete/clear with bytes retained, restart, partial completion, unfollow/refollow and CONTENT-held body; no resolver/body/copy leak. Stored/partial/pending/source-gone/outside-folders/removal-pending/identity-unavailable states remain exact, with no text on no-text branches. Deferred public status remains truthful. |
| Request fencing | Resolve A/B in both orders; vendor choice then account/identity change; hidden/resumed views; query change and retained old rows; deletion during resolver/messages/body/copy/inventory reads; alias or vendor change during paging; rejected refresh. No old content, child invocation, focus jump or action transfers to the new selection. |
| Existing UX/actions | P1/P2 search/committed-vs-draft filters, exact sender, All accounts, missing Active, page sizes, Load more and old error behavior stay intact. Saved actions retain source ownership; selected off-page source is not mislabelled missing. Vendor-reader workflows and IDs remain correct alongside the Inbox instance. |
| Safety/qualification | Fail-first regressions, focused Python/host/TS/DOM suites plus relevant existing M3/removal suites; TypeScript build, Rust formatting/Clippy. Synthetic native IPC with production CSP, hostile subject/body/filename/URL text, two reader instances, offline/inactive entitlement, 220+ senders and >100 messages/copies. No network effect or dependency/schema change. |
| Layout/accessibility | Wide 1920/1524, 1024, 640×520, 320 CSS px and 200% text/zoom: contained lists/reader, no horizontal page overflow, keyboard scroll/choice/Back, unique aria-controls targets, clear empty/loading/error states, focus return and no query change on resize. |

## Local conflict check, review gates and audit

On 2026-11-05, remote main still matched the baseline; the owned checkout was clean
at `e39c32d`. All 19 pre-existing worktrees matched the earlier inspection, including
the untouched dirty benchmark checkout. The Email Watcher owner was idle after work
in the separate `local-connect-status` repository. Nothing was paused or altered.

Open PRs #314 (model schema), #311 (admission diagnostic) and #237 (inspection contract
status) do not overlap these two plan documents. The future host-command slice will
touch `desktop/src-tauri/src/lib.rs`, also changed by #311; resolve/recheck that overlap
before code there. This does not block the local contract proposal.

Required change surface is identified above: Inbox projection/API/types, shared thread
read owners/cursors, typed host dispatch, reader state/DOM, Inbox lifecycle/layout and
focused regression/qualification evidence. No capture, retention, vendor matching,
body normalization/storage, automation policy, schema, dependency or CSP relaxation.
No new persistent token, authorization secret or provider call is proposed.

Juan must accept the exact final contract revision before thread-connection code.
Any separately authorized oversight/connector review is tied to that revision; a
semantic review fix requires renewed acceptance of the changed contract.
If oversight finds a definition needs changing, stop and seek authorization for that
narrow amendment instead of patching around it. This review submission does not authorize a merge. Juan's latest direct instruction
requires his approval for each PR, in addition to exact-head oversight PASS, connector
review and all applicable checks. No build, package, install or live app/settings
change is allowed. Later build/native qualification in the acceptance matrix requires
separate authorization; contract acceptance alone does not lift that restriction.

The repository's pull-request CI builds installers even for docs. This review-only
head uses `[skip ci]` to obey the no-build instruction, without changing workflows or
branch protection. CI is unfulfilled, not green; it is not waived as a merge gate.
The performed checks are doc scope, relative links/anchors and whitespace. No runtime
or native-qualification result is claimed.

Implementation summary: two plan documents only against main. The unpublished
navigation/filter implementation is preserved on its own local branch and excluded
from this PR. Existing application code and contract definitions are unchanged. Cold diff audit: this file owns the
new resolver/guard/entry mechanics, slices and tests; the earlier plan records the
chosen direction and points here rather than retaining competing sequencing.

Gap audit: **DONE for the local proposal; NOT DONE for implementation/qualification.**
Current native behavior is not claimed by this document. Review and Juan's
exact-revision acceptance remain outstanding; implementation is paused.
