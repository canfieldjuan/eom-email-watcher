# Thread View M3: Local Reads, Desktop View, CSP, and Inbox Copy

Status: proposed; docs-only draft for oversight review and Juan's acceptance of
an exact revision. No implementation is authorized by this document's creation.

Ownership lane: thread-view-m3. Baseline: `main` at
`1e39d16ad3e1d51979c5ec9b6699cf1cdb2f5110` (PR #323 merge).

This is the M3 plan required by [Milestones](../docs/THREAD_VIEW_CONTRACT.md#milestones).
The contract's definitions own behavior; this plan owns M3's mechanics. A
disagreement is a defect in this plan, not permission to amend the definition.
The source cited below is the baseline above, not the old primary checkout.

## Root cause

M2 has stored data without the M3 reading surface:

- `db.py:_refresh_logical_messages_view`, `_recompute_derived`, and
  `body_state.py:ensure_schema` provide logical messages, attribution/follow state,
  stored bodies, and unavailable reasons. No thread/body read operation is
  registered in `engine_api.py:OPERATIONS`; `docs/ENGINE_API.md` explicitly leaves
  thread keys, locations, and recipients for this milestone.
- `desktop/src/vendors.ts:vendorsView` describes vendor records and addresses,
  while `main.ts` renders their management controls. Neither lists conversations.
- `desktop/src-tauri/tauri.conf.json` sets `app.security.csp` to `null`.
- `main.ts` still introduces the app with "Only messages from watched senders or
  Gmail labels appear here." That excludes M2's admitted thread and Sent mail.
- A direct body-table read would bypass the retained-content boundary:
  `logical_messages` applies `removal.RECORD`, while body eligibility separately
  requires `removal.CONTENT` and current followed membership. Physically present
  bytes are not evidence that a body may be displayed.

## Required change surface

This PR adds this plan and the separately committed narrow D-body amendment
authorized by Juan. After final exact-revision acceptance, implementation PRs
may touch the following owners, with tests beside them:

| Surface | Purpose |
| --- | --- |
| `src/eom_email_watcher/db.py`, new `thread_reads.py`, `body_state.py` | One local read projection, scoped identity/alias resolution, paging and body-state selection |
| `src/eom_email_watcher/engine_api.py`, `docs/ENGINE_API.md` | Four explicit read operations and their request/response/error contracts |
| `desktop/src-tauri/src/engine.rs`, `src/lib.rs` under `desktop/src-tauri/` | Typed forwarding and command registration |
| `desktop/src-tauri/tauri.conf.json`, `desktop/index.html`, `desktop/src/styles.css` | CSP, external asset compatibility, and scoped view styles |
| `desktop/src/main.ts`, `vendors.ts`, new `threadView.ts` | Vendors-to-threads navigation, request fencing, plain-text rendering, copy |
| `tests/test_thread_reads.py`, `test_engine_api.py`, `test_thread_bodies_review.py`, `test_deferred_removal.py` where the existing owner is exercised | API, logical-copy, body/removal, and scope regressions |
| `desktop/test/threadView.test.ts`, CSP/view integration tests, existing vendor/inbox tests as affected | Host forwarding, DOM behavior, CSP enforcement and preserved Inbox behavior |

Exact test file placement may follow adjacent test owners; it does not expand
behavior. No schema migration or dependency change is proposed. If existing
storage cannot support these reads, return to the plan with evidence before code.

## Explicit non-scope

- M4/M6 claims, discrepancies, extraction, invoice pairing, and M5 domain/suggestion
  controls. No placeholder that implies those features already work.
- Capture, provider calls, reconcile scheduling, body fetching or storage, retention,
  entitlement definitions, removal acceptance/completion, and source-lock ownership.
- Sending, replying, moving, labeling, external-link opening, remote images, HTML
  email rendering, and thread-level summaries.
- New attachment downloads, Connect starts, automation decisions, or copies of
  their controls in the thread view. Existing Inbox controls remain their owner.
- Inbox layout, compacting/hiding addresses, name-only navigation, new filtering,
  counts, unread state, and a general visual redesign. The independent UX review
  is input to a later proposal, not an amendment to this one.
- Live app/settings changes, installation, promotion, deployment, merge, and
  native Windows runs. Any later native qualification follows its own authorization.

## Assumptions and acceptance blockers

1. [D-ops](../docs/THREAD_VIEW_CONTRACT.md#d-ops-operation-classes-and-gating)
   classifies all four new operations as reads. They must work when Connect is
   inactive and when a provider/model is absent. Reading initiates no check,
   credential refresh, repair, discovery, reconciliation, or content acquisition.
2. [D-removal](../docs/THREAD_VIEW_CONTRACT.md#d-removal-deferred-local-deletions)
   and the existing `removal` owner apply to every page, direct body lookup, copy,
   and alias. They are not implemented a second time in the desktop.
3. Local source inspection found no durable "all history complete" evidence.
   M3 always describes the view as locally retained, potentially partial history;
   it never claims a complete mailbox conversation. No new history-proof storage
   is proposed. Existing Sent-scope health remains the source of that notice.
4. Oversight accepted the plan-only correction at `54a4936`. Juan then authorized
   the narrow D-body amendment for retained vendor-attributed messages with
   unresolved mailbox identity on 2026-11-03 (America/Chicago), as a separate
   contract commit for review. That commit is `475a843`. Scope authorization is
   not final acceptance: oversight PASS and Juan's explicit acceptance of the
   final contract-and-plan revision remain pending. Changes to accepted behavior
   require a reviewed delta and fresh
   exact-revision acceptance. A review PASS alone does not authorize code.
5. Preflight found the old primary checkout dirty with benchmark/model work.
   This plan uses an independent clone from the baseline and leaves it alone.
   The existing Email Watcher session was idle; its latest task was a #278 closure
   audit. Open PRs #314, #311 and #237 have no changed file in common with this
   initial one-file plan. A second overlap check before the authorized D-body
   amendment also found no open PR changing that contract. This is not evidence
   that Juan has paused those owners. Recheck
   activity and file overlap before any implementation branch.

## Local read contract

All operations keep the existing protocol-1 response envelope and strict payload
validation. New request/response types are explicit in Python, Rust and TypeScript;
the host forwards every field and does not silently drop scope fields.

### Identity and one projection owner

`thread_ref` contains `provider`, `account_id`, `mailbox_identity_key`, and
`thread_key`. It is a data selector, not authority to fetch mail. The first two use
the existing account/provider validators; `thread_key` is nonempty text bounded
by `MAX_THREAD_KEY_BYTES` (512 UTF-8 bytes), not assumed to be a UUID. Identity is
an explicit string or `null` for an unproven legacy bucket, never an omitted
"use the current mailbox" fallback. Non-null values use
`db.py:_require_mailbox_identity_key` (the existing 64-character lowercase hex
identity validator). Message IDs use the existing nonempty-string local ID
contract, not provider-specific parsing.

Thread membership starts from `logical_messages`, grouped by the complete scope
using `_effective_message_identity_sql`. Thus proven legacy continuity uses the
same owner as M2. Unproven legacy identity remains a separate labeled bucket;
it is not silently joined to a connected mailbox or granted body eligibility.
Read retained accounts even if disconnected, without logging into them.

For IMAP only, resolve `thread_key_aliases` to a survivor, then verify visible
membership in the requested complete scope. Never resolve an alias into another
account/identity. Detect a missing target or cycle and fail closed, without
repairing storage. Responses return the resolved ref. A non-IMAP key is opaque
and is never interpreted as an IMAP alias. A thread without visible members is
`not_found`; neither its subject nor its count leaks through a stale follow row.

Attribution, direction, owner and follow state read their existing derived owners
under [D-derived](../docs/THREAD_VIEW_CONTRACT.md#d-derived-derived-state-and-invalidation).
Do not infer a vendor from admission provenance or confuse a message's attributed
vendor with the thread's owner. A thread is listed for a vendor through
[D-attribution](../docs/THREAD_VIEW_CONTRACT.md#d-attribution-a-messages-vendor):
the projection emits `owned` if that vendor owns it, otherwise `linked` if a
visible message is attributed to it. Both conditions produce one row, `owned`.

Each response is built in one SQLite read transaction, with membership, metadata
and eligibility read from that same snapshot. Store helpers accept that connection;
they must not open independent snapshots for body bytes or copy contents. The
snapshot is the read's linearization point. A read begun after a removal commits
cannot observe a pre-removal result. No body response is cached on disk.

### Operations and response shape

| Operation | Request payload | Data returned |
| --- | --- | --- |
| `threads.list` | `vendor_id`, optional `provider`, `account_id`, `limit`, `cursor` | `{items, next_cursor}`; each item has `thread_ref`, `relation`, `owner_vendor`, `subject`, `latest_received_at`, `message_count` |
| `threads.messages` | `vendor_id`, `thread_ref`, optional `limit`, `cursor` | `{thread, items, next_cursor}`; thread has resolved ref, relation, owner, history notice; each item has canonical `message_id`, subject, direction, sender/address/name, attributed vendor, received time, `body_state`, `copy_count` |
| `threads.body` | `vendor_id`, `thread_ref`, canonical `message_id` | `{thread_ref, message_id, body}` with the discriminated body shape below |
| `threads.copies` | `vendor_id`, `thread_ref`, canonical `message_id`, optional `copy_ref`, `limit`, `cursor` | `{thread_ref, message_id, items, next_cursor}`; without `copy_ref`, pages of copies; with it, pages of that copy's attachment inventory metadata |

Every request rechecks the requested vendor relationship and canonical membership;
a `message_id` alone never selects a body or copy. `vendor_id` uses the existing
UUID validator. A valid-but-missing vendor/thread/message relationship returns
`not_found`. Unknown fields, invalid types, overlong selectors, or malformed
cursors return `invalid_request`, never a wider query. `provider`/`account_id`
are both absent/null for all retained accounts or both supplied for one account.
The UI labels that account scope and each returned mailbox bucket distinctly.

`limit` defaults to 25 and is an integer from 1 through 100; booleans are invalid.
Lists contain no body text. A body is fetched from local storage on expansion,
one at a time per open card, bounded by M2's existing 200,000-character storage
cap. Empty stored text is `stored`, not "missing". The complete conversation is
reachable by paging; no last-N or first-page-only truncation is presented as all.

Copy pages satisfy M2's retained-copy requirement without listing one logical
message several times. A copy is a stored row or recorded source location; group
multiple location rows of one source into one copy. A copy row returns `copy_ref`
(source provider-message ID within the already validated scope), locations, saved
summary or null, and attachment count. A saved summary belongs to its actual
stored row; a location-only copy has no fabricated independent summary.
Inventory across the logical unit follows its recorded
`message_attachments.source_provider_message_id`, including a catalog recorded
through a location-only copy; do not assign it by canonical row ID alone. Legacy
catalogs without that field use their stored row's source binding, as the existing
inventory owner does. Copy order is canonical source byte order.
Supplying that returned `copy_ref` selects only the inventory recorded for that
copy; verify its membership again and return `not_found` for another unit's copy.
Inventory rows are ordered by stored position then part identity and use the same
25/100 paging bounds. They contain only name, media type, declared size and part
identity: no file paths, provider tokens, result bodies or retrieval. Both forms
bind their cursors to their distinct selector shape. There are no action controls.

### Ordering and pagination

- Thread lists order by newest logical received time descending. Messages in a
  thread order by logical received time ascending. Both use the logical unit's
  received time under [D-scope](../docs/THREAD_VIEW_CONTRACT.md#d-scope-in-scope-messages-and-retention),
  not whichever physical copy the query happened to select.
- Equal message times use [D-identity](../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys)'s
  canonical source byte order. Equal thread activity uses the thread's least
  canonical source, then complete scope/key byte order to make paging total.
  Reuse `_thread_member`'s timestamp/source rules. Invalid legacy dates show
  "Date unavailable" and sort after dated entries in either view; stable source
  tie-breaks still apply. No date is invented.
- The thread title is the newest visible message's subject (same tie-break),
  with "No subject" for empty subjects. Counts count visible logical messages.
- Use versioned keyset cursors bounded to 4 KiB, bound to operation, normalized
  account/vendor/thread/message selectors, limit, ordering version, last tuple,
  and a SHA-256 revision of the ordered projection. Compute the revision in the
  same read transaction from the relevant visible ordered IDs, scope, ordering,
  relationship and returned metadata. Body bytes are excluded from list reads.
  Copy revisions include their projected summary/inventory and eligibility.
  No new persistent generation, secret, or schema is needed.
- Paging re-evaluates visibility. A changed projection or resolved alias returns
  `stale_cursor`; the desktop clears that list and restarts at page one, informing
  the user that local history changed. It never appends across revisions. An
  invalid selector binding is `invalid_request`, with no unfiltered fallback.
  The cursor is untrusted and never substitutes for validating the live selector.
- A stable dataset must paginate completely without repeats or missing rows.
  This proposal computes a retention-bounded projection revision; measure it on
  large synthetic retained history before adding any performance/schema change.

### Body state and removal precedence

`body` is a discriminated union: `stored` carries `text`, `stored_chars`, and
`source_chars`; the other states carry no text. M3 projects, rather than changes,
the rules of [D-body](../docs/THREAD_VIEW_CONTRACT.md#d-body-stored-bodies) and
[D-removal](../docs/THREAD_VIEW_CONTRACT.md#d-removal-deferred-local-deletions):

1. First require visible canonical membership through `logical_messages` and
   the requested scope/vendor. Delete/clear targets are absent, including direct
   reads, list counts, subject selection and copy lookups.
2. Then consult shared `removal.CONTENT` for the entire logical unit before
   selecting text. A body-only target returns `removal_pending`, even after a
   refollow, with "Body removal pending until the local upgrade completes".
   It returns none of the held text or unavailable reason as a substitute.
   Retained headers, summaries and inventory keep their existing eligibility. Do not
   recreate a target predicate in `thread_reads.py`.
3. Project D-body's unresolved-identity case as `mailbox_identity_unavailable` in
   both the message-list body state and the expanded body response. Select it
   from the stored attribution and `_effective_message_identity_sql` in the same
   snapshot, and render D-body's exact label. No body or unavailable-marker lookup
   is needed for that branch. Keep the message's existing retained-header read
   scope. This narrowly authorized case adds no general `not_followed` state and
   is not inferred merely from an absent follow-cache row.
4. For the remaining eligible followed-message cases, project `stored`,
   `source_gone`, `outside_folders`, or `pending`
   from `message_bodies`/`message_body_unavailable`, with the exact D-body labels.
   Partial text displays the D-body N/M marker from stored lengths, independent
   of the Inbox's model-input truncation marker.

Use one helper for message-list body state and expanded body responses. There is
no body fallback through duplicate rows, an old alias, a summary, or provider
fetch. Copy readers apply RECORD through the shared owner before selecting
retained summaries/inventory and never return bodies. Body-only removal does not
erase or hide retained summaries; a summary is never presented as the missing body.
An unexpected or malformed body discriminator is an invalid read response, not
a successful body state. Reject that response, clear the body container and use
the ordinary load-error path; never render its text or retain a prior body. This
adds no fallback body label. A valid case outside the owning definitions cannot
be hidden, relabeled, treated as corrupt data or used to weaken a body barrier;
such a case requires a separate contract decision before implementation.

### Review finding and authorized amendment

[Oversight review 5762105258](https://github.com/canfieldjuan/eom-email-watcher/pull/324#pullrequestreview-5762105258)
correctly identified the extra `not_followed` state and "Body state unavailable"
label in `a8adb22`. The plan-only correction at `54a4936` removed both; oversight
confirmed that correction in mail `6848e593ec094cb5b41e5219faf6ee48`.

The review's general explanation that linked threads need not be followed needs
qualification. D-attribution's linked condition entails a vendor-attributed
message, and D-follow classifies a thread with such a message as followed.
In the current source, `db.py:_recompute_derived` writes attribution before
skipping follow-cache creation for an unresolved effective mailbox identity
(`:6201-6213` at the baseline). That is the concrete retained legacy case to
resolve; lack of a proven identity must not be presented as a proven unfollow.
When removing the last attribution ends the vendor relationship, this plan's
same-snapshot membership check specifies `not_found` for a stale vendor
request. It does not need a successful unfollowed body state.

Juan's subsequent verbatim authorization, relayed from the Lead Intake chat,
permits the narrow D-body amendment in PR #324 with the label
"Body not stored (mailbox identity unavailable)", as a separate contract commit
for oversight review and his final exact-revision acceptance. Commit `475a843`
contains only that contract amendment and its status/revision record. This plan
consumes the definition rather than defining another body state for the same case.
Generic `not_followed` and unknown-state success labels remain excluded. Following,
admission, reconciliation, removal, and retained visibility are unchanged. No
implementation is authorized until the final contract-and-plan revision is accepted.

## Desktop behavior and CSP

### CSP precedes bodies

The first implementation slice installs and verifies a production CSP while no
body UI exists. Proposed minimum policy:

```text
default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self';
font-src 'self'; connect-src ipc: http://ipc.localhost;
object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'
```

Only packaged application assets and Tauri's local IPC transport are permitted.
Do not add arbitrary `http:`, `https:`, wildcard hosts, `data:`/`blob:` script
sources, `unsafe-inline`, or `unsafe-eval`. No CSP null/disable fallback. Keep
Tauri's asset CSP processing enabled. Its build-generated hashes/nonces for trusted
framework/application assets are not permission to render email as scripts; the
packaged policy and actual initialization must be inspected together. The
[Tauri CSP documentation](https://v2.tauri.app/security/csp/) describes this
processing; its [configuration reference](https://v2.tauri.app/reference/config/)
distinguishes production `csp` from `devCsp`.

All application script entry points stay external, with event listeners instead
of inline handlers. Styles use bundled CSS. If trusted initialization needs a
policy change, document the exact asset and platform requirement in this plan's
review; do not weaken the mail boundary to make the window load. Verify the
actual generated policy, not just a string in JSON.

For development, an explicit `devCsp` may add only the configured local Vite
origin and its localhost:1420 HMR websocket; it cannot grant remote email loads
or arbitrary inline script. Development allowances cannot enter the production
config. A localhost browser preview alone does not prove packaged webview CSP.

Before the body slice can be declared complete, prove malicious body strings
remain text and independently prove the real CSP blocks injected inline script,
event handlers, remote scripts, image/beacon/fetch loads, frames and forms.
Use synthetic content and an isolated app profile. Preserve startup, IPC,
dialogs, existing navigation and bundled styles. Unsupported or untested platform
behavior is a qualification gap, never grounds to disable CSP.

### Vendors, threads, and messages

- Add a read-class "View threads" control to each vendor card. Keep vendor
  management and the inactive-Connect notice/control classes as they are.
- Within Vendors, show the selected vendor, the retained account selection,
  a back control, and its paged thread list. Each thread shows subject, last
  activity, logical-message count, and `Linked` plus owner when it is not owned
  by this vendor. No rows from another selection appear under this heading.
- Opening a thread shows its paged oldest-first messages, each with direction,
  sender name and full address, attributed vendor or "No attributed vendor",
  received time, subject, and one body state. Expand a body by an explicit
  accessible button; load local text and set `textContent` on a wrapping text
  container. No `innerHTML`, Markdown, automatic linkification, images, or iframes
  receive any message/header/provider text. Use text nodes for other untrusted
  fields too. Quotes remain the stored text, with whitespace preserved.
- Copy details use their separate paged read and do not duplicate logical
  timeline entries. Existing Inbox summaries and actions remain available there;
  the new view makes no promise that M4 claims or M6 results are present.
- Show "History partial — only locally retained messages are shown". Preserve
  the existing account's "Sent mail unavailable" notice when recorded. Neither
  a refresh nor an empty state initiates a mailbox check. Empty/error/loading
  states say which vendor/account/thread is being read; an error is not "no mail".
- Back navigation retains the parent selection and restores focus to its trigger.
  Thread/message/copy expansion controls expose `aria-expanded` and
  `aria-controls`. Use existing tokens; support the app's 640px minimum width,
  text zoom, wrapping of long addresses/body lines, and keyboard-only navigation.
- Bind request generations to vendor, complete scope, thread and expanded
  message/copy. On a change, clear old rows and text before loading. Late
  responses cannot render into a new selection, append to a new list, reopen a
  collapsed body, or resurrect deleted/unfollowed content. A failed load leaves
  no previous-scope data visible. `stale_cursor` restarts only the current list.
- Vendor/address changes, accepted delete/clear/unfollow, account changes and
  existing check/history refresh events invalidate affected thread data and
  expanded bodies before reload. Returning to the view or app focus revalidates
  it; an explicit "Refresh local history" also uses only these read operations.
  Failed revalidation leaves content cleared with a retry action. Preserve vendor
  input drafts and do not change the Inbox's own generation/selection machinery.

### Inbox and watchlist copy only

Replace the app/Inbox description with:
"Locally retained messages from watched senders and Gmail labels appear here.
Followed threads and Sent replies to vendors also appear when captured with Connect."

Add to the Watchlist explanation:
"Watching an address keeps messages from that sender in the Inbox. Vendor records
can also capture followed threads and Sent replies while Connect is active."

Make unfiltered empty/list accessibility copy say "retained messages" instead of
implying that watched senders are the only admission source. Preserve the
selected-sender empty states and account/filter distinctions. No new status copy
claims full history, current entitlement, or a successful capture when unknown.

[PR-Two-Column-Inbox.md](PR-Two-Column-Inbox.md) remains authoritative for the
sender rail: every full watched address remains visible, exact normalized
`sender` filtering stays distinct from `sender_query`, filter Apply/Reset retains
selection, removal falls back to All messages, cursors reset on scope changes,
and late responses cannot render under a new selection. M3 does not redesign
the long address list. Review its UX separately with Juan.

## Implementation sequence after acceptance

1. **M3.1 — CSP and local reads.** Ship the verified restrictive CSP, typed read
   API and API documentation. No body rendering. Test the complete scope/removal
   boundary, copy semantics and pagination before exposing it in the UI.
2. **M3.2 — Thread view and copy.** Consume only those reads; add the view,
   request fences, accessibility, and the approved Inbox/Watchlist wording.
   Exercise end-to-end synthetic cases and actual CSP enforcement. No dependency
   on later milestone UI. Split by these concerns if either PR grows beyond the
   repository's small-PR review budget; do not omit proof to fit the budget.

Both slices require independent exact-head review and CI. This plan grants no
merge or install authority. No application code is written before plan acceptance.

## Verification plan

This docs-only PR runs the existing thread-contract seam tests, checks relative
links/definition anchors, audits every named source owner at the pinned baseline,
and checks the final diff is one Markdown file with no whitespace errors. Builds,
live mailbox probes and implementation tests are not part of this docs change.

After acceptance, implementation evidence must fail before each behavior exists:

| Boundary | Required evidence |
| --- | --- |
| Read class | Expired/missing entitlement and unavailable provider/model still permit every read; fail-on-call sentinels prove no provider/model/discovery/check/repair calls and no new domain writes |
| Identity | Gmail/Microsoft matching native thread IDs in two accounts and reused account IDs with distinct mailbox identities stay separate; null-identity legacy rows do not join; IMAP alias resolves only in its scope; missing/cyclic alias fails closed |
| Logical content | Several physical copies appear once in the timeline; location-only copies and retained duplicate summaries/inventory remain distinguishable; canonical promotion and equal source/date ties preserve order |
| Vendor relationship | Owned plus linked produces one owned row; linked vendor sees the actual owner and each message's actual attribution; a vendor removal/re-attribution invalidates prior direct reads |
| Pagination | More than 100 threads/messages/copies, tied dates, invalid legacy dates, last page, empty page, malformed/oversized/wrong-scope cursor, mid-page alias merge, arrival, delete and ownership change; stable sets have no omission/repeat, changed sets restart explicitly |
| Body | Full, empty, 200,000-character bounded, truncated, source-gone, outside-folders, pending and removal-pending cases; no text in list responses or non-stored union variants; lengths match stored evidence; malformed discriminators reject the read and clear prior text |
| Identity-unavailable body | A retained vendor-attributed logical message with neither stored identity nor proven continuity remains in its existing retained view and projects the D-body label without body text, including when old body/marker bytes remain; pending removal takes precedence; proved continuity and a stored identity each use normal body eligibility on a fresh read; an absent follow row alone does not select this state; no read resolves identity or fetches content |
| Removal | Use the existing unqualified 33-row held unit: delete/clear hide metadata/counts/copies/body while bytes remain; unfollow preserves permitted headers but hides content; refollow cannot revive a pending body target; restart and qualified completion preserve these outcomes |
| Snapshot/race | Commit removal before a read begins and prove no content; pause a read across a writer to prove a consistent snapshot; invalidate a UI request during delete/unfollow/selection and prove its late body/page cannot repaint |
| Host boundary | Each selector, identity null/string, cursor, limit, discriminator and error survives Python-to-Rust-to-TS; missing/unknown fields are refused rather than silently dropped |
| UI | Owned/linked navigation, oldest-first paging, local refresh, focus return, keyboard expansion, narrow window/zoom, no stale heading/data on failed/superseded loads, no lost vendor drafts |
| CSP | Packaged startup/IPC works; synthetic markup is inert text; effective CSP blocks independent script/remote-load/frame/form probes; no production development allowances or disable fallback |
| Inbox preservation | Full watched addresses, exact sender vs substring control, selected-sender Apply/Reset, removal fallback, scope clearing and stale-response fences remain unchanged; copy acknowledges M2 admissions |

Planned implementation commands: targeted `uv run --locked pytest` for the new
read/API tests and existing body/deferred-removal/identity suites; targeted Ruff;
`node --test --experimental-strip-types desktop/test/*.test.ts`;
`pnpm --dir desktop build`; focused Rust forwarding tests, `cargo fmt --check`
and `cargo clippy --all-targets -- -D warnings` for the desktop manifest. Actual
CSP/DOM proof uses an isolated fixture build, with no live settings or mail.
Record exact commands, revisions and outcomes in implementation evidence.

## Implementation summary

This revision contains the M3 proposal and Juan's narrowly authorized D-body
amendment in separate commits. It defines the local read boundary, preserves
the storage/removal owners and Inbox navigation, and orders CSP proof ahead of
body UI. No application code, dependencies or schema change.

## Cold diff audit

`plans/PR-Thread-M3-Read-View-CSP.md` is added. Its sections trace to the M3
milestone, referenced definitions, M2's retained-copy requirement, and the
existing Inbox contract. `docs/THREAD_VIEW_CONTRACT.md` changes only D-body's
authorized identity-unavailable case and the status/revision record. API shapes
are proposals, not claims of existing code. Contract seam/link checks and the
two-document scope check cover this docs revision;
the behavior matrix is required future evidence, not a claimed test result.

## Gap audit

NOT DONE

The narrow contract-amendment scope is authorized and documented. Oversight
review of that amendment and Juan's final exact-revision acceptance remain
pending. Scope authorization alone does not permit implementation. Implementation,
packaged CSP/interaction proof and platform qualification have not run. The plan
must not be read as acceptance, implementation completion, or deployment approval.
