# Calendar cancellation after accepted unfollow — accepted contract

Refs #290. **Accepted; implementation must follow this contract.**

## Acceptance

On 2026-10-18 the operator accepted contract commit
`377fa0ed1ea3ad08f1ad03a3e27e79df75dc926d` and authorized implementation, verbatim:
**“Accept contract and implement (Recommended)”**. This acceptance record is a
separate commit before tests or production changes. The
[oversight technical review](https://github.com/canfieldjuan/eom-email-watcher/pull/291#issuecomment-6255236832)
agrees with the owner/storage boundary; it does not replace operator authority.

Baseline: merged main `9fc701e157cad3de91a2207dca02551a21937bb0` (#261).
All source anchors below refer to that revision. Refresh the inventory if main
changes before implementation. #278 and installation/promotion are separate.

## Decision and inventory

The [operator decision](https://github.com/canfieldjuan/eom-email-watcher/issues/290#issuecomment-6252386328)
is “Cancel cleanly (Recommended)”: an approved event that has not been sent is
cancelled without a calendar call, with **“Not added: you unfollowed this thread”**.
Writing, possibly sent and reconciling work finishes its normal recovery and is
never automatically deleted. Re-follow plus fresh approval makes a new admission;
re-follow alone never resubmits the old write.

Existing validated observation, before this contract:

- Alias `calendar-observation`, SHA256
  `abcf4ed11b5d4eeb83046a245230a7d74e49e4ab67905c279366789a3f2b477e`,
  retained outside the checkout in the #261 eligibility evidence packet.
- Synthetic SQLite scheduling proposal and confirmed write; accepted held
  unfollow; direct invocation of the existing begin owner. Observed
  `write_authorized -> source_unavailable`, then
  `RuntimeError: Automation write lost its expected-state race`; one pending
  unfollow intent. No provider was called by that probe.
- That probe ran on `8bfb5a6d0052e2411ba7e6b92fe997aacba94ca1`; it is historical
  evidence, not a fresh test at this baseline. It did not exercise uncertain or
  writing work. The merged source and existing regression assertions below are
  an inventory, not a claim that the new policy is implemented or qualified.
- No model/runtime/prompt change or model performance result is involved. The
  future reproduction reuses `tests/test_db.py`'s scheduling fixtures and real
  removal acceptance. It must prove the wrong outcome on the pinned merged base
  before a production edit; no new standalone harness is authorized.

| Owner at baseline | What it currently controls |
| --- | --- |
| `db.py:1776 _admit_scheduling_automation` | One run per bound source/rule version; source uniqueness currently makes repeat admission a no-op. |
| `db.py:1841 _mark_automation_sources_unavailable` | Cancels an `authorized` write and transitions its run to generic `source_unavailable`; removes source-derived proposal/extraction payloads. Writing/uncertain states are not transitioned by this branch. |
| `removal.py:434 _accept_in_transaction`, `:661 accept_unfollow_units`; `db.py:5827 _follow_owner` | Acceptance kind and frozen targets are known in the same guarded transaction as derived unfollow. The reason must originate here. |
| `db.py:12510 decide_automation_proposal` | Consent, proposal identity, replay, and reservation of a unique calendar transaction before any submission. |
| `db.py:12684 pending_automation_calendar_writes`, `:12740 begin_automation_calendar_write`, `:12847 transition_automation_calendar_write` | Work selection, durable handoff to `writing`, and transaction/event identity through recovery. |
| `service.py:1047 process_scheduling_writes`, `:1236 decide_scheduling_proposal` | Existing authorization checks, create versus lookup-only recovery, and decision results. |
| `db.py:12050 recoverable_automation_runs`, `:12967 reserve_automation_extraction`, `:12298 record_automation_proposal` | Preparation and versioned consent material; these must support fresh preparation without reviving cancelled consent. |
| `db.py:14126 _hydrate_inbox_rows` | Calendar UI currently depends on a surviving proposal payload and excludes generic source-unavailable outcomes. |
| `engine_api.py:2646 _calendar_automation_decide`, `:2572 _hide_locked_automation_previews` | Bound decision transport and entitlement-aware projection. |
| `desktop/src/main.ts:190 CalendarProposalPreview`, `:1745` calendar rendering; `desktop/src-tauri/src/lib.rs:2106 calendar_proposal_decide`; `desktop/src-tauri/src/engine.rs:1099 CalendarDecisionResult`, `:1172 InboxItem` | Existing calendar preview, status/decision UI, and typed native bridge. The native DTOs must carry the same optional outcome. |
| `microsoft_calendar.py:898 create_calendar_event`, `:1024 find_calendar_event_by_transaction` | Provider idempotency key and bounded reconciliation lookup. |

Existing controls to preserve include
`test_source_cleanup_cancels_authorized_write_before_submission`,
`test_calendar_write_lifecycle_preserves_transaction_and_event_identity`,
`test_confirmed_scheduling_proposal_writes_once_after_durable_reservation`,
`test_ambiguous_calendar_write_reconciles_without_resubmission`, and
`test_confirmed_calendar_write_reconciles_after_entitlement_expiry`.
The existing unresolved-expiry and writing-survival tests define the current
retention boundary; this change neither shortens nor extends it.

## Root cause

The generic source-cleanup/calendar-write foundation was introduced by
`d8bdd48896f54ec4e84dbe773603db5ee5f916ca` (#108). Its cancellation is already
durable, but it does not distinguish accepted unfollow from other source loss.
The expected-state check in the begin owner treats a matching cancelled admission
as an arbitrary race. The inbox's proposal-payload join then removes the only
calendar status surface when cleanup removes the payload. Source uniqueness and
confirmation replay also prevent treating a later fresh approval as a separate
admission. These are the origin seams; an exception catch or a UI-only notice
would leave the missing durable reason and admission identity unresolved.

The requested behavior is a separately accepted policy amendment. It was not an
implementation requirement of the frozen #261 calendar behavior.

## Required behavior and storage

### One cancellation owner and a precise handoff

1. Accepted unfollow supplies its typed cause from the removal acceptance kind
   to the existing source-invalidation owner. Other deletion, purge and generic
   source-loss callers supply their existing generic cause explicitly. Do not
   infer an unfollow from current visibility, a missing body or missing proposal,
   and do not copy the frozen-target membership rule into calendar readers.
2. A single transaction helper owns calendar cancellation. In the existing
   guarded `BEGIN IMMEDIATE`, it changes only an admitted, unsubmitted write
   (`automation_calendar_writes.status='authorized'`) to `cancelled`, records
   `failure_code='thread_unfollowed'`, preserves its transaction and source
   identities, advances the run's version, and appends one immutable terminal
   event carrying that cause and transaction identity. The existing run remains
   `source_unavailable` with its required generic run failure code. The cancelled
   **write admission**, not a newly invented run enum, is the terminal state.
3. Use existing status/failure-code columns and immutable `automation_events`.
   Keep schema 36, table layouts and existing CHECK constraints. A new state enum,
   migration or table is not authorized by this contract. If the existing model
   cannot satisfy the identity/history requirements, stop for an explicit
   contract amendment before implementation continues.
4. The durable transition from authorized to `writing` is the existing
   conservative possibly-submitted boundary. If unfollow commits first, no
   provider create may begin. If `writing` commits first, the admitted write may
   finish or enter normal uncertainty/reconciliation. There is no promise that
   a process can tell whether the first network byte was sent after a crash.
5. `begin_automation_calendar_write` returns an explicit typed cancelled outcome
   for that exact cancelled admission, rather than its generic race exception.
   Validate the original confirmed version/admission identity, including its
   immutable event and transaction binding. Unrelated stale versions, foreign
   bindings and inconsistent states retain their existing refusal behavior.
   Its direct callers consume the typed result and never perform provider work
   for it; they do not re-derive cancellation from source data or swallow all
   `RuntimeError`s. Admission replay must work without a deleted proposal payload.
6. Duplicate acceptance, restart, and repeated begin/confirmation of the same
   cancelled admission leave its terminal result unchanged. They do not allocate
   a new transaction, append duplicate transitions, or enqueue a write. Failure
   during cancellation rolls back the source change, write/run state and event
   together under the existing acceptance owner.

### Provider-owned work and fresh approval

- An accepted unfollow does not cancel or change a `writing`, `unresolved`,
  `reconciling`, or completed provider admission. Preserve transaction UUID,
  principal/calendar binding, interval and known Graph event ID. Existing
  authorization, bounded lookup, retries, timeouts and retention remain in force.
  An uncertain result is resolved by the existing lookup for the same transaction;
  absence remains the existing unresolved result. No replacement POST, automatic
  event delete, invented successful result or discarded recovery identity.
- A refollowed source may prepare a fresh proposal through the existing
  extraction/proposal owners only when the shared CONTENT predicate permits
  content again. Refollow while the old body-removal targets remain applicable
  does not bypass them. Preparation never performs calendar submission.
- Fresh consent creates a fresh transaction UUID against a fresh proposal
  generation and current state version. Preserve the canonical source/run
  uniqueness; a preparation cycle may reuse that run's identity with monotonic
  state versions. It must retain the old cancellation event and transaction
  identity. The current-write row may be replaced by a newly confirmed admission
  only after its previous admission is terminal-cancelled; it is a current
  projection, not permission to reopen the cancelled transaction.
- The proposal owner controls monotonic proposal versions across cancellation
  and payload cleanup. Do not reset them such that an old proposal hash/version
  or old confirmation can authorize the new generation, even when content is
  identical. The immutable ledger distinguishes old cancellation and fresh
  confirmation. Existing active/uncertain/completed admissions cannot be replaced.
- The re-preparation eligibility rule belongs at the scheduling admission/
  extraction seam and is shared by its selector/start, not added as several
  recovery filters. It applies only to the specifically cancelled-unfollow
  lineage after refollow. No new universal “must follow” condition is imposed
  on ordinary initial calendar proposals. Normal source, consent, entitlement,
  principal, expiry and retention requirements still apply.

### Durable, content-free status and exact notice

Project one optional `calendar_write_outcome` on inbox rows and calendar-decision
results from the write/run ledger, independently of proposal payloads. Its fields
are `run_id`, `state_version`, `transaction_id`, existing write `status`,
`failure_code`, and nullable `graph_event_id`. Use the existing validated source
binding and RECORD visibility owner; never join on a bare reused message ID.
It contains no title, attendee, body, extracted content, time interval or token.

The same projection covers the existing authorized/writing/uncertain/terminal
write states, so a provider-owned write can still report its normal recovery
outcome after unfollow removes its proposal payload. It does not grant access to
that payload or relax CONTENT gating. A history-only status remains readable
under the existing entitlement-expiry policy; a new proposal/approval retains
its existing authorization requirements.

One frontend calendar-outcome formatter, shared by inbox refresh and decision
results, renders **“Not added: you unfollowed this thread”** only for
`status='cancelled'` with the persisted `thread_unfollowed` cause. Generic source
loss must not get that wording. Render the status in the existing calendar/message
action surface even when there is no `calendar_proposal`; do not fabricate a
proposal to keep the card alive. Existing text for other outcomes remains intact.
No enabled old-confirmation action remains. No ntfy/desktop notification bypass,
new content channel, or new calendar-operation endpoint is authorized.

## Required change surface

Implementation, after acceptance, is limited to the source/invalidation,
calendar admission/start/re-preparation and inbox projection owners named above;
their service/API/native direct consumers; the existing desktop calendar UI and
a shared outcome formatter; and tests of those behaviors. Changes to the thread
contract must record this one accepted policy amendment at its canonical location.
Remove any obsolete generic-cancellation special cases replaced by the shared
helper. All original assertions remain; internal typed-result API translations
must preserve their meaning and be individually mapped to this contract.

## Verification plan

Run and retain the baseline reproduction before production edits. Each new test
must fail on the policy assertion, not on fixture setup or ambient entitlement.
Use existing public/synthetic fixtures, real operation/source locks and actual
acceptance transactions. No real OAuth, live mailbox or production calendar.

| Proof | Required observation |
| --- | --- |
| Approved, then accepted unfollow, immediate and held cleanup | Durable cancelled write and exact cause, normal typed begin/replay, unchanged transaction identity, exact notice, no provider create or delete. |
| Both handoff orders, including a selected-but-not-started worker | Acceptance-first cancels; writing-first retains normal completion/lookup. Deterministic barriers, not sleeps. Reopen at both commits and after a lost response. |
| Writing / unresolved / reconciling / completed at unfollow | Same transaction and principal survive; found/absent/transport-error lookups follow current behavior. No second create, event delete, fabricated completion or early purge. Keep existing expiry controls. |
| Cancellation replay and transaction rollback | Duplicate work is idempotent; injected failure before commit leaves no partial source/run/write/event transition. Wrong expected version and missing/foreign admission remain refused. |
| Refollow and fresh approval | No automatic POST; pending old targets still withhold content. After normal eligibility returns, a fresh proposal plus explicit consent gets a new UUID and monotonic versions; old cancellation history remains and stale confirmation/start cannot activate either admission. |
| Binding/cause controls | Other message, provider, account or mailbox reuse cannot cancel or read this admission. Delete/clear/purge and generic loss retain existing meanings and never get the unfollow notice. |
| UI and API after proposal deletion | One bounded status projection, exact text, no stale action or removed content, correct reload/restart and refresh-race behavior; uncertain/completed outcomes remain visible without content resurrection. |
| Boundary and omission mutations | Reject missing/mismatched identifiers and invalid versions, including false/zero/empty values. Removing the origin cause, omitting the begin cancellation result, or cancelling writing work must fail the corresponding behavior proof. Restore exact candidate bytes afterward. |

Use `tests/test_db.py`, `tests/test_service.py`, `tests/test_engine_api.py`,
`tests/test_deferred_removal.py`, `tests/test_microsoft_calendar.py` and existing
desktop calendar/transport tests. Runtime/render assertions are required; source
regex tests alone do not prove the notice or disabled action. Reuse existing
test infrastructure rather than adding a new diagnostic harness.

After focused and adjacent checks, run applicable Ruff, desktop type/build/tests,
Rust format/Clippy/native tests and the full Python suite once on the final
candidate in the repo environment, under umask 022 with empty isolated XDG config
and data directories. Classify every new failure before edits. Retain raw results,
changed-assertion mapping and source/ref freeze outside worktrees. Available
Windows-native and final-head installed/populated lifecycle qualification remain
hard gates; add the bounded cancellation/recovery case to the existing native
proof tests if required, not to a new harness. New harness/infra requirements
require a contract amendment first. No budget, timeout, oracle, skip or expected
difference relaxation is permitted.

## Explicit non-scope

No provider API redesign, new provider, new Graph permissions, shared SDK change,
calendar auto-delete, unattended resubmission, new general retry product, schema
bump, unrelated migration, source identity algorithm, removal-target rule,
entitlement exception, new notification channel, #278 cutover, installation,
promotion, live data, broad refactor, dependency change or history rewrite.
#261 stays merged and unchanged. Its eligibility and deletion guarantees remain.

## Assumptions and stop conditions

The existing cancelled write plus immutable event history can represent terminal
admission and later fresh consent without changing run uniqueness or schema.
This is the proposed implementation boundary, not a claim that all reuse paths
already support it. The acceptance/cancellation reason must be supplied at its
origin; absence of a trustworthy reason is not permission to guess.

Stop for a concrete amendment if that representation fails, if new public actions
or different user wording are needed, if an existing recovery/retention defect
requires a behavior change beyond preserving normal recovery, or if new native
harness/diagnostic infrastructure is required. Follow the standing introducing-
commit, reproduce/isolate/explain/fix/prove/regression rule for every finding;
one grouped push per accepted repair round. No downstream compensation.

## Implementation summary, cold audit and gap audit

The accepted policy is implemented at its existing owners:

- `removal._accept_in_transaction` supplies the acceptance cause.
  `db._cancel_automation_source` owns payload cleanup, cancellation, run version
  and immutable event as one transaction. Purge and generic cleanup supply their
  generic cause explicitly. The old begin-time cleanup special case uses this
  same owner rather than leaving an authorized write behind.
- `db._cancelled_calendar_admission` recognizes the exact original confirmation
  and cancellation events. `AutomationCalendarWriteCancelled` is consumed by
  begin/decision callers; unrelated stale/missing identities remain refused.
  Historical cancellation survives replacement of the mutable current-write row.
  The worker catches only `AutomationCalendarWriteStateChanged`; invariant errors
  propagate. Its former broad catch came from `b49c165f` at baseline
  `service.py:1109`, and the new real-ledger regression failed with DID NOT RAISE
  before this correction. This is the contract's explicit no-swallowed-errors
  consumer requirement, not a new recovery policy.
- `db._scheduling_preparation_available_sql` is shared by selector and extraction
  start, with legacy-schema preparation preserved. Refollow needs shared CONTENT
  availability. The proposal owner retains monotonic generations using the write
  floor and immutable proposal transitions. Fresh approval requires write
  authorization and creates a new transaction; old replay creates no work.
- `db.calendar_write_outcome` is the common content-free projection for current
  writes and immutable cancellations. Inbox reads select the binding, run version
  and write metadata in one joined SQLite statement through the existing
  source/RECORD owner. My initial implementation in `55b05e0` read the run and
  write separately: deterministic handoff probes reproduced an old version paired
  with a newer status in both the getter and inbox. The owner now returns one
  coherent snapshot; no downstream status correction or extra filtering is used.
  The decision transport adds the same optional projection under its existing
  exclusive operation owner.
- `desktop/src/calendarOutcome.ts` owns inbox/decision wording. Production
  rendering works without a proposal, renders as text and has no stale action.
  Decision refresh prefers a newer outcome and leaves old buttons disabled until
  a fresh projection renders. Fresh pending/declined proposals keep their own
  existing wording while the old cancelled admission remains in history.
- `desktop/src-tauri/src/engine.rs` forwards the shared optional metadata through
  both typed DTOs to Tauri serialization. A cold audit of implementation commit
  `55b05e0fa4177b8d6eade61054a489cc6017f350` caught my omitted native reader: Serde
  discarded the field in inbox and decision results. Both actual round-trip
  regressions failed with Null instead of the outcome before the owner fix.
  All write statuses round-trip without proposal payloads; legacy absence remains
  accepted, and malformed/partial outcome objects are refused.
- `docs/THREAD_VIEW_CONTRACT.md` records this accepted policy amendment. Provider,
  schema, dependencies, workflows and native command endpoints are unchanged.

### Changed-assertion map

All original Python assertions are retained. The existing generic cleanup test
adds an exact generic-cause assertion. The engine decision test preserves every
original result and binding assertion and adds the accepted optional metadata to
its stub and expected response. New database, service and API tests cover the
contract table above: actual immediate/held unfollow, restart/lost reply, both
handoff orders, selected worker cancellation, rollback, exact replay/version,
provider-owned found/absent/error recovery, refollow/content gating/fresh write
permission/new transaction, immutable old history, binding reuse and metadata
without content or entitlement renewal.

In `desktop/test/calendarProposal.test.ts`, the former source-text checks for
no-event, uncertain-write and rejected-write wording become exact runtime checks
of the common formatter. The former adjacency check for decision refresh now
checks the new projection choice and executes that production segment against a
newer completed outcome. All other original controls/interval/invitation/assertions
remain. Existing Rust assertions remain; the added native serialization and
legacy/partial-shape probes exercise the actual DTO boundary. Added production-segment rendering verifies exact text with no proposal,
HTML write or confirmation button; the fresh-decline regression protects the
existing wording. No assertion is skipped or relaxed.

The cause, begin-result and writing-boundary omission mutations each failed at
its behavior proof, then exact candidate bytes were restored. Local fixture/
implementation errors found during development were classified before repairs:
missing SQL interpolation at the extracted binding owner; legacy-schema removal
references at the new shared preparation owner; invalid new fixture inputs and
an unavailable test transpiler API; and stale-admission wording at the new common
formatter. The latter has its own fail-before/pass-after regression. These were
unpublished working changes, not failures attributed to a different commit.

Earlier full Python invocations were deliberately interrupted before completion
for the refusal-boundary, fixture-lock, omitted native DTO and read-snapshot corrections. Their raw outputs are
retained as interrupted pre-final evidence, not completed qualifications. The new
API regression explicitly binds config.database_file to Store.path, and the common
calendar fixture now uses the existing service config's database path. Final
qualification runs the entire Python test collection with `uv run --locked pytest`,
under umask 022 and fresh empty XDG config/data and external temporary directories.
The CI coverage job remains required; no test oracle or collection is reduced.

**NOT DONE:** final local/full/native gates; final-head Windows installed/populated
qualification, automatic review/reconciliation and posted oversight PASS; merge.
Require clean local == actual remote, required checks green and no unresolved
threads/changes requested; merge with a merge commit and reviewed-tree equality.
No installation/promotion or #278 work.


## Accepted origin amendment

A public Store
reproduction found that a followed, approved calendar source with neither a cached
body nor an unavailable marker remains authorized after unfollow and can enter
writing. No provider was called. The pre-unfollow capture starts from body-bearing
units (`db._capture_followed_content_sql`, pinned-base line 5733, introduced by
`e6b3e21fb99e05c67bf714f456429b39c197f747`), so removal acceptance never receives
that source. My local `55b05e0` calendar implementation relied on that receipt path.

The bounded proposal adds the trusted calendar-admission before-image and derived
follow-loss origin to the accepted change surface, while keeping body-removal
selection/targets/receipts, source identity, schema, endpoints, recovery, initial
proposal policy and all publication/merge gates unchanged. It authorizes no
inferred-unfollow fallback in begin, readers or the UI. The operator accepted this bounded extension; implementation and qualification
remain required.

The complete proposal and reproduction are posted at
[issuecomment-6255876370](https://github.com/canfieldjuan/eom-email-watcher/pull/291#issuecomment-6255876370).
Durable aliases: `calendar-follow-loss-amendment`, SHA256
`a38f38e72e5fb0262553c2e993494b220e597ba84da7d768c79bd97c708fc71a`;
`no-body-unfollow-observation`, SHA256
`d3a82bb4e8ea1a27c595eb6469b90aa5d4b022caf74e8bbcdfcb3a52a9f9440c`.
The operator accepted the amendment directly in this session: **“Accept amendment
and implement (Recommended)”**. Acceptance is recorded in this plan alone before
any amendment code. Oversight independently relayed **“Accept (Recommended)”**.
Full Python qualification is
incomplete; deliberately interrupted runs remain retained as incomplete evidence.
The published contract-only head's green CI does not qualify the implementation.
No implementation push, merge, installation or promotion has occurred.
