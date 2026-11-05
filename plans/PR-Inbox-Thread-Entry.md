# Inbox: resolve a retained message and open its thread

Status: **contract proposal prepared for oversight review — awaiting Juan's exact-revision acceptance**.
Source baseline: `main` at `3ed72aec73672a4334f5fcd949b7c60198d40ef8`.
Deferred local navigation/filter work: `e39c32d39439481fa19f4b6847d8ec4f4b94c906`;
it is not a dependency of this Inbox-link implementation.

Juan chose “We should connect the full thread first” and authorized this review PR.
Oversight passed the earlier head `1f93855`, then note `2472ea66` relayed two direct
answers: **“Open its thread too”** for non-vendor Inbox messages, and **“Link first”**
for sequencing. This revision addresses both before code. It explicitly proposes
amendments in the referenced thread contract; the old PASS does not cover this head.
Fresh oversight review and Juan's exact-revision acceptance are required. Merge
requires his approval for this PR. No build, package or install is authorized.

First connect every valid retained Inbox message to its stored conversation in the
**current Inbox layout**. The [three-pane design](PR-Inbox-Three-Pane-Reading.md) and
its unpublished P1/P2 work are a later, separate concern. Their implementation is not
a prerequisite or an implicit permission granted by acceptance of this contract.

## Root cause and evidence

An Inbox row is a retained logical message, not a thread selector. The UI currently
has no safe way to turn that selection into M3's scoped conversation, and M3 currently
requires a vendor even when the selected retained message has no vendor relationship.

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

This revision explicitly amends
[D-attribution](../docs/THREAD_VIEW_CONTRACT.md#d-attribution-a-messages-vendor),
[D-identity](../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys),
[D-body](../docs/THREAD_VIEW_CONTRACT.md#d-body-stored-bodies) and
[D-ops](../docs/THREAD_VIEW_CONTRACT.md#d-ops-operation-classes-and-gating).
Those definition changes are proposed and marked unaccepted in a separate commit.
[D-follow](../docs/THREAD_VIEW_CONTRACT.md#d-follow-followed-threads),
[D-derived](../docs/THREAD_VIEW_CONTRACT.md#d-derived-derived-state-and-invalidation),
[D-removal](../docs/THREAD_VIEW_CONTRACT.md#d-removal-deferred-local-deletions) and
[the accepted M3 plan](PR-Thread-M3-Read-View-CSP.md) retain their existing owners.
The read-context extension and one new explanatory no-text state do not broaden
capture, body storage, content eligibility, retention or claims.

- Add **Open thread** to each current Inbox row, including non-vendor mail. That
  explicit action resolves the row and opens a conversation view within Inbox.
  Existing row expansion, saved analysis and action controls remain available unchanged.
  No row is automatically opened or acted on.
- While the thread is open, its region replaces the Inbox columns without destroying
  their query/list/scroll state. **Back to Inbox** restores that state and returns
  focus to the originating row's Open thread control, or the list heading if it is
  no longer present. This uses the current layout, not a new three-pane reader.
- A vendor conversation shows the selected vendor and its owned/linked relation,
  including the owner when linked. A non-vendor conversation is labelled **“No vendor
  associated”**; it has no invented vendor or owner. Both show their exact retained
  account/mailbox context and current thread subject.
- The thread contains the eligible locally retained messages, available through M3
  paging. Keep the history/coverage notice; do not fetch uncaptured provider history,
  load all pages automatically or expand all bodies on entry.
- Inbox predicates continue to filter its message list, not the opened timeline.
  Say **“Thread history includes other senders and messages outside these Inbox
  filters.”** All-accounts Inbox entry still binds to the selected row's exact account.
- Keep the current newest-subject heading, oldest-first timeline, explicit Load more,
  copy inventory, body expansion, truncation, Sent notice and applicable no-text states.
  Mark the originating canonical message when its page is loaded. If not yet on a
  loaded thread page, say so; do not claim it is absent from the conversation.
- Saved analysis remains saved analysis, accessible through the existing Inbox row;
  it is never substituted for an email body. Thread cards gain no new action/automation
  controls. Reading a non-vendor conversation does not start watching/following it.

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

## R2 — one local resolver for both conversation contexts

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
an earlier multi-vendor result has become one or zero candidates.

In one query-only SQLite snapshot, the resolver:

1. Finds the canonical RECORD-visible row by exact provider/account/message ID and
   compares its effective identity, with null-safe equality, to the supplied one.
2. Derives its current complete thread key/scope and logical membership using the
   existing owners. Resolved IMAP aliases must stay within provider/account/identity.
3. Derives the represented vendor set and current follow owner from visible thread
   members. A zero-vendor set is a valid non-vendor conversation, not unavailability.
   The originating message need not itself have an attribution. Missing/inconsistent
   derived data must fail rather than impersonate an empty vendor set.
4. Returns metadata only, from that same snapshot. No bodies, saved summaries,
   attachment inventory, provider/model calls, reconciliation, repair or migration.

The response is a strict tagged union (required fields, no mixed alternatives):

| State | Fields in addition to `state` | Behavior |
| --- | --- | --- |
| `resolved` | `message_ref`, `context` | Context is the closed vendor/non-vendor union below. |
| `choose_vendor` | `message_ref`, `thread_ref`, `owner_vendor`, `items: VendorRef[]`, `next_cursor` | Several represented vendors for one conversation. Require an explicit choice with owner/linked labels; never silently auto-pick. |

```ts
type ResolvedContext =
  | { kind: "vendor"; vendor: VendorRef; thread: ThreadItem }
  | { kind: "non_vendor"; thread: NonVendorThreadItem };
// NonVendorThreadItem has ThreadItem's ref, subject, date and count fields,
// but relation is exactly "non_vendor" and owner_vendor is required null.
```

Without an explicit vendor choice, zero represented vendors returns `non_vendor`,
one returns `vendor`, and several return `choose_vendor`. An explicit `vendor_id`
returns a vendor context only while that exact vendor remains represented. If it
has disappeared, return `stale_selection`, even if the set is now empty or has one
other member. **Remove the former `unavailable/not_vendor_related` alternative.**
Every current visible canonical Inbox message with valid derived membership can
open its retained conversation; ordinary absence of a vendor is never a refusal.

Candidate pagination is deterministic by vendor ID byte order, uses the existing
bounded cursor mechanism (at most 4096 bytes), and binds the resolved reference,
seed reference, owner, candidate IDs/names and ordering. It must not infer uniqueness
from a page length or truncate the candidate set into an automatic selection.

Errors carry no thread/body data:

- `invalid_request`: malformed input or forbidden fields, not a broader retry.
- `not_found`: no visible canonical message in the requested provider/account;
  this includes stale physical-copy IDs and RECORD-excluded rows.
- `stale_selection`: the visible row's effective identity changed, an explicitly
  chosen vendor is no longer eligible, or a non-vendor read context gained a vendor.
  Clear the reader; refresh the Inbox or chooser.
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

Extend `threads.messages`, `threads.body` and both `threads.copies` query forms
with **two strict selector alternatives**, not a nullable vendor bypass:

```ts
type VendorSelector = {
  vendor_id: string;
  thread_ref: ThreadRef;
  inbox_message?: InboxMessageRef;
};
type NonVendorSelector = {
  inbox_thread: { thread_ref: ThreadRef; message_ref: InboxMessageRef };
};
```

Each operation adds its existing paging/message/copy fields to exactly one selector.
The `inbox_thread` field discriminates the new alternative. Reject mixed forms,
unknown fields, partial/null objects and `vendor_id: null`; never infer a non-vendor
mode from a missing or invalid legacy vendor selector. Existing vendor entry keeps
its exact request shape. Inbox-origin vendor reads require `inbox_message`; non-vendor
reads always require the nested message reference. A client/host must never strip
these fields or retry without the origin.

In the same snapshot as each projection, validate:

- complete scope and current thread membership through the shared projection owner;
- the originating message's canonical RECORD visibility, exact effective identity
  and membership in that conversation for either Inbox-origin alternative; and
- the context: a vendor selector still requires that represented vendor; a non-vendor
  selector requires an empty represented vendor set and no owner.

This factors shared scoped membership without weakening the vendor-only entry point.
No general unguarded vendor-independent listing API is added. `threads.list` remains
vendor-scoped. No client-side scan or synthetic vendor is used.

For message responses, `thread` is a closed context union: existing owned/linked
relations retain their required owner; `relation: "non_vendor"` requires
`owner_vendor: null`. Scope, current subject and history notice remain required.
List responses for vendor browsing keep their existing vendor-only types. Resolver
and message metadata must agree with their validated context; malformed combinations
fail strict host decoding. No display fallback supplies an owner or a known identity.

A body/copy target may be another message in the validated conversation. It still
passes the existing canonical membership and operation-specific eligibility owner.
Body projection follows R4; saved-copy-summary eligibility remains unchanged. Bind
selector mode, complete origin, context and relevant current classification into
message/copy/inventory cursor selectors/revisions. Cursors cannot move between
origins, vendor/non-vendor modes or vendor browsing. Context changes invalidate the
read/chooser before any old data is returned; retry means resolving afresh, never
silently substituting a vendor or dropping its guard.

When the origin is gone, clear the Inbox thread view rather than continuing through
the stale link. Canonical promotion requires a fresh Inbox selection. A recorded
same-scope IMAP survivor remains the existing allowed alias case, with origin
membership checked in the survivor. Current owner/relation/subject come from the
new response. Contradictory scope, alias cycles and missing derived mapping fail closed.

## R4 — body/removal/security boundaries

The explicit D-body amendment adds one read state, **`not_followed`**, with label
**“Body not stored (thread not followed)”** and no text/length/marker payload. It
explains why a newly readable non-vendor timeline has headers without a stored body.
It is not a capture/body-storage policy change or a new reconcile task.

The centralized `body_state.project` owner must apply the amended order, using thread
classification validated inside the read snapshot (never a client-supplied boolean):

1. RECORD exclusion rejects the target/origin before projection.
2. Any existing CONTENT exclusion in the logical unit produces `removal_pending`,
   including unfollow cleanup/refollow cases whose bytes remain physically stored.
3. An unfollowed conversation produces `not_followed` without reading body bytes or
   unavailable-marker data, including when its effective identity is unresolved.
4. A followed conversation with unresolved identity retains
   `mailbox_identity_unavailable`, also before body/marker access.
5. Only an eligible followed, identified message uses the existing stored/partial,
   pending, source-gone or outside-folders projections. Valid stored bodies appear
   where the current owners permit; physical presence alone never grants visibility.

The unresolved-identity followed case remains derived from visible attribution,
not the absence of a follow-cache row (that cache cannot represent a null key).
No duplicate body-state selector belongs in the resolver, host or renderer. Host and
UI closed unions must explicitly admit the new state and refuse text on every no-text
alternative. The already accepted identity-unavailable behavior for unattributed
members of a vendor-related thread remains intact.

Both conversation contexts show eligible retained headers and existing copy metadata.
Disconnected accounts still permit local reading. No active-account identity is
borrowed; no raw bytes are exposed through saved analysis or alternate endpoints.
Eligible saved summaries keep their distinct existing semantics. A visible no-vendor
thread with an unexpected residual body still cannot expose it outside the order above.

Delete/clear barriers, pending-status persistence and public completed/deferred outcomes
remain with D-removal. New reads never equate unreadability with completed deletion.
All new operations are local read class with current startup admission, no entitlement
gate and no provider/model call. Capture, body storage, retention and claims stay
unchanged. Preserve production CSP, text-only DOM sinks, explicit body expansion,
length limits, escaped copy metadata and existing no-text labels. No HTML/markdown
renderer, remote images, attachment downloads or automatic link activation is added.
New error logs contain neither message content nor mailbox identifiers.

## R5 — link first in today's Inbox, with shared lifecycle fencing

Reuse `ThreadReadClient`, `ThreadViewState` and `ThreadViewDom`. Add a direct resolved
entry transition that installs context, thread and required origin together, clears
children and loads the first message page. It must not list every vendor/thread first.
An active non-vendor entry is an explicit state, not a fake VendorRef: existing
`vendor !== null` visibility/allowed checks must become mode-aware without enabling
unselected or suspended readers. Vendor browsing keeps its current behavior.

Use a separate Inbox-origin instance of the reusable reader with unique DOM prefixes
for headings, body/copy regions, aria-controls and focus keys. The existing Vendors
instance remains independent. The Inbox thread region has Back to Inbox and the fixed
source-account label, not an account picker that can widen its origin. Keep current
Inbox columns, filters, row expansion/actions, widths and pagination; hide and restore
them as specified above. No three-pane layout or e39c32d cherry-pick in this step.

Entry requests bind to the complete source reference, committed query epoch,
entry/vendor-choice generation, mailbox-operation revision and visible view.
Invalidate all children before notifying/rendering. Superseded resolver, chooser,
message, body, copy or inventory responses cannot commit or launch further reads.

- Scope/query changes, reset, accepted delete/clear and configuration invalidation
  clear the old thread. Retained previous-query rows cannot open it until current.
- Existing Inbox Load more preserves state without reopening a thread. Same-query
  refresh revalidates the source row before refreshing the reader; absence from the
  current loaded Inbox span is unavailable, not a deletion claim or auto-page trigger.
- Account/catalog/verified-identity, vendor, watchlist, Inbox-span and relevant
  configuration changes invalidate both reader instances. Centralize this dispatch
  and preserve mailbox-effect fencing and nested mutation holds.
- Hide/Back suspends the Inbox reader and clears bodies/copies; returning via its
  entry action resolves again. Focus/resume refresh cannot reuse stale origin data.
- Deletion/clear/unfollow holds invalidate both readers before writes. Durable pending
  notices stay with their existing owner; late completions cannot reopen content.
- Existing Inbox action handlers, confirmations, expiry timers, decision refresh and
  source ownership remain unchanged. Open thread is read-only. A returning action
  result never applies to another selected row or another thread member.

## Implementation slices after contract acceptance

1. **Projection/resolver:** required Inbox reference, Python resolver, zero/one/many
   vendor context classification, strict unions, candidate paging and API documentation.
2. **Scoped reads/body state:** shared membership plus strict vendor/non-vendor selector
   alternatives, per-request origin guard, cursor bindings and the central no-text
   state/precedence amendment. Prove barrier and identity cases before host wiring.
3. **Typed host/client and entry state:** strict Rust/TS contracts, local command,
   complete forwarding, mode-aware resolver/chooser/direct entry and shared lifecycle
   dispatch for the Vendors and Inbox-origin reader instances.
4. **Current-Inbox entry:** Open thread on every current row, existing reusable thread
   region with Back to Inbox, correct vendor/non-vendor labels, preserved row actions,
   focus/scroll and narrow-window checks. This delivers the link before redesign.

Run permitted tests in each slice; any build/native qualification requires separate
authorization under the current no-build rule. Before release/promotion, qualify the
new native entry under production CSP once that work is authorized; no implicit release.

**Later and separate:** review/accept the three-pane design, then deliberately carry
forward the frozen navigation/filter work `e39c32d` and implement contained panes.
That code is unsubmitted and is neither a dependency nor part of PR #343. Do not
publish or overwrite it through this contract review. Recheck live work, main and
PR overlap for each small implementation concern. No milestone depends on the later
redesign to make its shipped link usable.

## Acceptance tests and evidence

| Boundary | Required proof |
| --- | --- |
| Inbox projection and strict wire | Every reference field present and row-consistent; explicit null identity succeeds; Python/Rust reject missing/null-object/extra/wrong-type fields. TS types require the reference and forwarding retains invalid runtime fields for host rejection. No legacy provider/account default manufactures a new selector. Older response fails closed. |
| Account/identity | Same native thread ID and source-message lookalikes across providers/accounts and reused account IDs with distinct mailbox identities stay separate. Test a null legacy bucket beside known identities and null→proven transition. Assert exact returned seed/thread/vendor and both provider/account predicates; removing either filter must fail the tests. |
| Vendor/non-vendor contexts | Zero vendors opens a non-vendor conversation, one resolves its vendor, several require choice. Include unattributed/outbound seed, seed's vendor different from owner, rename/delete, and empty→one/many or vendor→empty changes during every read. No null-vendor bypass, synthetic owner or stale-choice fallback. More than 100 candidates page correctly; a cursor from a former chooser cannot become an implicit new context. |
| Duplicates/aliases | Canonical plus physical/location-only copies appears once; physical-copy input and canonical promotion require refresh; no-Message-ID best-effort remains. IMAP survivor/alias, equal provider keys in another identity, missing/cyclic alias and corrupt derived mapping fail safely or resolve only the correct survivor. |
| Every child operation | Strict vendor and non-vendor shapes; missing/null/mixed selectors fail. Guard present on message/body/copy/inventory first page, append, retry and stale restart for both contexts. Forged/wrong-scope/removed seed cannot authorize a valid unrelated thread. Guard-bound cursor cannot move between seeds or into vendor entry. Same-snapshot assertions cover check/projection races. |
| Removal/body states | Held 33-row logical delete/clear with bytes retained, restart, partial completion, unfollow/refollow and CONTENT-held body; no resolver/body/copy leak. Add not-followed alongside the existing states. Assert CONTENT before not-followed before unresolved identity before bytes/markers, including residual body and null identity. Every no-text branch carries no text/length payload; no provider/model or reconcile task is launched. Deferred public status remains truthful. |
| Request fencing | Resolve A/B in both orders; vendor choice then account/identity change; hidden/resumed views; query change and retained old rows; deletion during resolver/messages/body/copy/inventory reads; alias or vendor change during paging; rejected refresh. No old content, child invocation, focus jump or action transfers to the new selection. |
| Current Inbox UX/actions | Each retained row has Open thread, including exact-sender/label-admitted non-vendor mail. Existing filter/sender/account/page-size semantics, row expansion/actions and Load more remain unchanged on main, without e39c32d. Back restores query/list/scroll/focus; no action executes on entry. Both reader instances have unique IDs; an off-page thread origin is not mislabelled absent. |
| Safety/qualification | Fail-first regressions, focused Python/host/TS/DOM suites plus relevant existing M3/removal suites; TypeScript build, Rust formatting/Clippy. Synthetic native IPC with production CSP, hostile subject/body/filename/URL text, two reader instances, offline/inactive entitlement, 220+ senders and >100 messages/copies. No network effect or dependency/schema change. |
| Link accessibility before redesign | Wide 1920/1524, 1024, 640×520, 320 CSS px and 200% text/zoom: usable current Inbox and reused thread region, keyboard Open/choice/Back, no new clipping or horizontal overflow, unique aria-controls, focus return and unchanged query on resize. Prove this on main without the navigation/pane redesign. Full contained three-pane geometry is deferred to its separate plan. |

## Local conflict check, review gates and audit

On 2026-11-05, remote main still matched the baseline; the owned checkout was clean
at `e39c32d`. All 19 pre-existing worktrees matched the earlier inspection, including
the untouched dirty benchmark checkout. The Email Watcher owner was idle after work
in the separate `local-connect-status` repository. Nothing was paused or altered.

Open PRs #314 (model schema), #311 (admission diagnostic) and #237 (inspection contract
status) do not overlap these plan documents and the thread contract. The future
host-command slice will touch `desktop/src-tauri/src/lib.rs`, also changed by #311;
resolve/recheck that overlap
before code there. This does not block the local contract proposal.

Required change surface is identified above: Inbox projection/API/types, shared thread
read and body-state owners/cursors, typed host dispatch, reader state/DOM, current-Inbox
entry/lifecycle and focused regression/qualification evidence. No capture, retention,
vendor matching, body normalization/storage, automation policy,
schema, dependency or CSP relaxation. The proposed no-text read state is explicit.
No new persistent token, authorization secret or provider call is proposed.

Juan must accept the exact final contract revision before thread-connection code.
Any separately authorized oversight/connector review is tied to that revision; a
semantic review fix requires renewed acceptance of the changed contract.
If oversight finds a definition needs changing, stop and seek authorization for that
narrow amendment instead of patching around it. This review submission does not
authorize a merge. Juan's latest direct instruction
requires his approval for each PR, in addition to exact-head oversight PASS, connector
review and all applicable checks. No build, package, install or live app/settings
change is allowed. Later build/native qualification in the acceptance matrix requires
separate authorization; contract acceptance alone does not lift that restriction.

The repository's pull-request CI builds installers even for docs. This review-only
head uses `[skip ci]` to obey the no-build instruction, without changing workflows or
branch protection. CI is unfulfilled, not green; it is not waived as a merge gate.
The performed checks are doc scope, relative links/anchors, whitespace and the existing
thread-contract seam test. No runtime or native-qualification result is claimed.

Implementation summary: two plan documents plus the explicit thread-contract amendment
against main. The unpublished
navigation/filter implementation is preserved on its own local branch and excluded
from this PR. Application code is unchanged; only the marked
D-attribution/D-identity/D-body/D-ops amendment changes definitions. Cold diff audit:
the amended definitions own read context/scope/body-state rules; this file supplies
wire mechanics, slices and tests; the three-pane file explicitly defers redesign.

Gap audit: **DONE for the local proposal; NOT DONE for implementation/qualification.**
Current native behavior is not claimed by this document. Review and Juan's
exact-revision acceptance remain outstanding; implementation is paused.
