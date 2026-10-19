# Pending calendar work after deliberate unfollow — contract for #292

Status: **contract only; implementation requires operator acceptance of this document**.
Base: merged #291 at `b8258b6de14ebc4438d4aec0c6317edc013843ac`.
Decision: [issue #292](https://github.com/canfieldjuan/eom-email-watcher/issues/292#issuecomment-6258924816).
This is one concern in a separate PR. The first commit changes this document only.

## Contract

### Root cause

`src/eom_email_watcher/db.py:6000` `_capture_followed_calendar_sql`, introduced by
`9b6b7a09db9e50151c1a8fc2f12f80a03008d26d`, captures only a bound run in
`write_authorized` with an `authorized` calendar write. The guarded derived
unfollow consumer at `db.py:6254` therefore sees no bodyless run that is still
`detected`, `extracting`, `proposing`, or `awaiting_confirmation`. With no retained
body, the body-removal acceptance path has no target either. A deliberate unfollow
can leave the proposal and its confirmation action live. This is the policy gap
recorded with a synthetic reproduction in issue #292; that issue's evidence is
historical, not a passing or failing test of this PR.

The shared transition at `db.py:1881` already invalidates preparation and
prevents an unsubmitted authorized write. It currently returns before advancing
`manual_review` and `ambiguous` runs, which can leave a review intent after
deliberate unfollow. Its event carries the typed `thread_unfollowed` cause only
for an approved write. A pre-write cancellation needs that durable cause for
accurate replay and eligibility; it must not fabricate a calendar write.

### Required change surface

- **Before-image and cause owner:** capture every physically bound, previously
  followed scheduling run with cancellable preparation or an unsubmitted
  authorized write before follow inputs change, whether a body is retained or
  not. Consume that evidence only when its logical source has actually lost
  follow in the same guarded transaction. Use one shared pre-send eligibility
  definition for capture and consumption, preserving the physical source and
  canonical logical binding from #291. A missing or mismatched source must
  never cancel another run. The body-bearing removal path and bodyless path
  must converge on one cancellation owner and one typed cause.
- **Transition:** on accepted `thread_unfollowed`, remove current proposal and
  extraction payloads, advance cancellable pre-write preparation and review
  states to terminal `source_unavailable`, record the typed cause in the
  immutable event, and suppress the deliberate-unfollow review notification.
  A pre-write run has no calendar transaction or cancelled-write row. An
  `authorized` write keeps #291's exact cancellation, transaction identity,
  replay and existing notice. Generic `source_unavailable` still requests
  review. Already writing, unresolved, reconciling, completed,
  failed or user-declined work is not recast as a pending cancellation.
- **Re-follow:** old proposal identity/version and stale confirmation cannot
  authorize a write. If the same bound source becomes followed again and
  satisfies the existing CONTENT, principal, run-lifetime and retention rules,
  the existing scheduling preparation owner may issue a fresh proposal with
  monotonic identity/version and requires new confirmation. This explicit
  recovery uses the recorded unfollow lineage; it does not impose a new
  follow requirement on ordinary initial proposals. Alias/canonical recovery
  remains single-owner and must not create duplicate preparation.
- **Consumers:** pending-work, confirmation, direct/inbox and notification
  readers consume the resulting run/payload/event state. They do not infer
  unfollow from missing content, filter a still-live proposal downstream,
  create a fake write outcome, or add new user-facing wording. Review-state
  notifications are suppressed at the origin for both initial and refollowed
  generations. Existing approved-write wording stays as in #291.
- **Commit ordering:** `Store.decide_automation_proposal` and the accepted
  unfollow through `Store.derived_connection(operation)` are serialized by
  their SQLite `BEGIN IMMEDIATE` write transactions. The latter transaction
  is owned by `removal._RuntimeAcceptance.transaction` under
  `removal.operation_scope`; the operation guard alone is not the race oracle.
  If confirmation commits first, it durably authorizes the write and the
  subsequent unfollow applies #291's authorized-write cancellation. If
  unfollow commits first, it removes the proposal and advances the run; a
  later confirmation is refused as stale and never saves `write_authorized`.
  Neither order creates a provider call in the acceptance transaction.
- **Tests:** use the real store, follow mutation and removal operation scope
  in `tests/test_db.py`, with adjacent deferred-removal/service tests where
  needed. Cover bodyless and retained-body pending proposals, earlier
  preparation states, both review states, guarded transaction refusal,
  rollback, source/alias binding, stale confirmation, re-follow/fresh consent,
  generic source loss, writing/uncertain/completed controls, and both
  confirmation/unfollow commit orders using deterministic barriers rather
  than sleeps. Each changed assertion must map to the accepted behavior here;
  retain old assertions.

### Explicit non-scope

No schema or migration, public API or native DTO, entitlement, provider retry,
calendar create/lookup/delete, UI copy, notification transport, initial
proposal admission policy, timeout, dependency, generated-file or harness
change. Do not delete an event already handed to the provider or relax CONTENT
and held-removal eligibility. Do not fold the generic-source-loss replay-test
gap tracked in #293 into this PR. Any need to change product behavior or these
boundaries requires a contract amendment before code.

### Assumptions and blockers

The direct “Let’s start” after the #292 policy explanation authorizes this
contract-first round. Code remains held until the operator accepts this exact
contract. The issue's old synthetic observation and #291's passing controls
guide the reproduction; neither proves the new behavior at this base.

### Verification plan

1. Freeze the exact main base and reproduce the bodyless pending-proposal
   survival, retained-body comparison and review-state notification with
   existing synthetic fixtures. Record the before result outside the worktree.
2. Add fail-first real-store regressions that fail at the policy assertion,
   with boundary controls for missing/mixed bindings, cached/held/no-body
   source, run state, re-follow, old versions, generic loss, and the two
   serialized commit orders. Identify each introducing commit and owning
   function before production edits.
3. Fix at the before-image/cause/transition owners. If fresh preparation needs
   a reader change, use the existing shared scheduling eligibility owner and
   its persisted event signal. Prove isolated and original cases pass; use an
   omission mutation to show tests fail without the origin fix.
4. Run focused and adjacent tests, Ruff and diff checks, then the full local
   Python suite under `umask 022` in isolated XDG/TMP directories before the
   grouped implementation push. Classify each new failure before editing.
   Keep raw logs, assertion mapping and source hashes in durable evidence
   outside the worktree. Required final-head CI, native Windows installed and
   populated qualification, exact-head independent review and zero unresolved
   threads gate merge. Keep the implementation round at about 1,000 changed
   lines or fewer including tests; split before pushing if it would exceed
   that limit. No installation or promotion.

## Implementation summary

Implementation in progress on the accepted contract. The before-image capture
and its consumer now use one pending-admission predicate; the cancellation
owner records a typed cause for all accepted-unfollow transitions. Fresh
preparation and alias recovery consume the immutable cause event. Qualification,
exact-head review and merge remain pending.

### Assertion mapping for the implementation round

All new assertions in the named test are covered by its mapped contract clause:

| Test in `tests/test_db.py` | Accepted clause and asserted behavior |
| --- | --- |
| `test_pending_calendar_unfollow_revokes_proposal_at_origin` | Capture, transition and consumers: with or without retained body, terminal run, absent payload/write, typed cause, refused stale confirmation. |
| `test_calendar_review_state_unfollow_suppresses_review_at_origin` | Transition and consumers: review-state invalidation, suppressed review intent, typed cause, no write. |
| `test_pending_calendar_preparation_unfollow_has_typed_event_without_write` | Capture and transition: detected, extracting and proposing all terminate with typed event and no transaction. |
| `test_pending_calendar_refollow_requires_fresh_proposal_and_confirmation` | Re-follow: existing CONTENT gate, monotonic proposal, refused old identity, fresh authorization required. |
| `test_pending_calendar_confirmation_and_unfollow_commit_orders` | Commit ordering: both guarded transaction orders and their distinct durable outcomes, with no pending write after unfollow. |
| `test_pending_calendar_unfollow_rolls_back_with_follow_mutation` | Commit ordering and transition: injected failure rolls back follow removal, run, proposal and event together. |
| `test_generic_source_loss_does_not_recast_calendar_review_as_unfollow` | Transition: generic source loss keeps its review-state behavior and does not forge an unfollow cause. |
| `test_pending_calendar_unfollow_alias_recovery_has_one_owner` | Capture and re-follow: physical alias binding, canonical precedence and one recovery owner. |
| `test_second_pending_unfollow_does_not_borrow_old_cancelled_write` | Transition: a later pending generation gets no transaction identity or new write row. |
| `test_cancelled_alias_fallback_never_duplicates_an_existing_canonical_run` | Capture and re-follow: the prior `canonical == detected` assertion was replaced with an exact terminal-state/version/typed-cause assertion because the accepted contract revokes that pending canonical run. The existing single-owner and alias refusal assertions remain. |

## Cold diff audit

The implementation round changes this contract, the database origin and its
real-store regression tests. It changes no schema, native DTO, provider call,
UI, dependency or generated file. The full Python run under `umask 022` exited
successfully; its private raw log is held as alias `full-python.log`, SHA-256
`cfbefed75080482f88a2e94e641e719f8a0f701f479fef1c742dc560fe2f6b6b`.
The Windows-only platform contracts were excluded on this Linux host; exact-head
native proof and independent review are still gates.

## Gap audit

**NOT DONE:** full local qualification, grouped implementation push, exact-head
CI and Windows proof, independent review and merge.
