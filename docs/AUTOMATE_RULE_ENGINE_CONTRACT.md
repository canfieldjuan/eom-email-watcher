# Automate Connect rule-engine core contract

Status: **PROPOSED — no generic rule-engine runtime exists on `origin/main`.**

This document contracts the first implementation slice only. It is anchored to
Email Watcher commit `11ff930695ff55f5c91f3a3873d8e74901fc8f68` and to the
executable paths cited below. Local Claude branches are design donors, not
released behavior or migration baselines.

## 1. Problem-derived contract

### Root cause

The unpublished draft split one logical decision across three trust boundaries:

1. a strict parser that `Store.put_rule` did not call;
2. `mark_analyzed`, which captured a rule-set revision without evaluating it;
3. `record_rule_evaluation`, which later accepted an arbitrary evaluated
   revision and arbitrary `(rule_id, rule_version)` pairs.

That split permits stale edits, invalid stored definitions, revision mismatch,
and fires referring to versions that never existed. Its historical snapshot
query can also repeat an interrupted unbounded scan forever. Pagination,
deadlines, or an attachment cap would treat symptoms while preserving the
split.

### Correct fix

- Put strict rule parsing and canonical serialization inside the Store write
  boundary.
- Require compare-and-set versions for every mutation of an existing rule.
- Evaluate the current enabled rules inside the existing `mark_analyzed`
  `BEGIN IMMEDIATE` transaction.
- Persist the message's exact rule-set revision and all matched fires in that
  transaction.
- Generate fires only from rule versions and attachments read by that
  transaction; public callers do not submit fire identities.
- Stop at durable Connect fires. Dispatch and provider submission are later
  slices with separate contracts.

### Must not change

- The existing scheduling automation remains on its present code path.
- Existing interactive Connect invocation and the consumer-side queue remain
  unchanged.
- No Invoice Processor or provider-side queue behavior changes.
- No user interface, timer, notification, calendar-rule, or provider-dispatch
  behavior is added by this contract.

## 2. Current executable behavior

These are observations, not roadmap claims.

- The database schema is version 19 (`db.py:22`). There are no
  `automation.rules.*` operations, rule tables, or generic evaluator on
  `origin/main`.
- Message processing persists attachment descriptors before model analysis and
  calls `mark_analyzed` afterward (`service.py:1339-1361`).
- `mark_analyzed` already holds `BEGIN IMMEDIATE` across the message update and
  the existing scheduling admission (`db.py:5405-5467`). That is the atomic
  composition point for local, metadata-only rule matching.
- `analysis.requeue` calls `Store.requeue_analysis`, which rejects every
  message except a still-pending, permanently paused analysis
  (`engine_api.py:1792-1803`, `db.py:5365-5385`). It does not re-analyze an
  already evaluated event.
- IMAP permits up to 1,000 MIME parts, Gmail's MIME walker accumulates the
  descriptors it receives, and `replace_attachments` materializes and stores
  the complete iterable (`imap.py:41,555-580`, `mime.py:50-99`,
  `db.py:2873-2902`). A rule threshold of 64 is not an ingestion or workload
  bound.
- Existing scheduling automation calls the Microsoft calendar adapter, not
  Connect. Existing `connect.attachment.invoke` is an interactive engine API
  path. Neither is evidence that generic Automate-to-Connect composition is
  implemented.

## 3. Scope and reachability

The implementation lands as two code PRs after this contract is accepted.

1. **Rule authority (schema 20):** strict Connect rule definition, immutable
   revisions, compare-and-set mutation, and engine API CRUD.
2. **Atomic evaluation (schema 21):** pure matcher, message revision capture,
   durable pending fires, and attempt-one dispatch identities inside
   `mark_analyzed`.

Reachability proof:

- A rule is created through the real engine API request dispatcher.
- A real `watcher.check` request processes a pending message through the
  existing mailbox/model orchestration.
- The observable result is one durable pending fire and one stable attempt-one
  request identity per matching rule and attachment.
- The proof asserts zero provider submission; this slice records work but does
  not execute it.

Parking predicate: findings about dispatch, retry policy, timer wakeups,
confirmation delivery, notification expiry, calendar-rule migration, retained
provider tombstones, or UI behavior are parked unless they prove this
create-rule-to-durable-fire path unsafe or incorrect.

Parked hardening: none. The parked items are future product mechanisms, not
hardening mechanisms added to this slice.

## 4. Closed rule definition

The stored definition is strict JSON. Unknown members, wrong JSON types,
unsupported field/operator pairs, and unsupported actions are rejected before
storage. Definitions are bounded to 16 KiB, conditions to eight, action
parameters to sixteen, and `in` values to six.

The engine-owned values `rule_id`, version, enabled/system/deleted flags,
revision boundaries, and timestamps are not definition members.

### Closure declaration: action kinds

1. **Membership:** CLOSED. The only admitted action is `connect.invoke`.
2. **Source:** ENUMERATED here for this slice. Calendar and notify actions are
   explicitly deferred rather than inferred from existing subsystems.
3. **Outside behavior:** any other `kind` is rejected as `invalid_rule`; this is
   the safe side because no durable or external action is authorized.

`connect.invoke` contains:

- `capability: {id, version}`;
- `provider: {app_id}`;
- at most sixteen scalar `parameters`;
- `confirm_each`, defaulting to false.

It must include an `attachment.media_type` condition, so every admitted action
selects a concrete persisted attachment.

### Closure declaration: condition fields and operators

1. **Membership:** CLOSED. The canonical vocabulary is the `ConditionField`,
   `ConditionOp`, and field-to-operator map in the rule-definition module.
2. **Source:** ENUMERATED in that module; parser and matcher import the same
   definitions rather than copying the matrix.
3. **Outside behavior:** unknown fields/operators and unsupported pairs are
   rejected as `invalid_rule`, so novel input cannot become an action.

The closed matrix is:

| Field | Operators |
|---|---|
| `sender` | `equals`, `domain_equals` |
| `sender_name` | `equals`, `contains` |
| `subject` | `contains`, `starts_with` |
| `category` | `equals`, `in` |
| `priority` | `equals`, `in` |
| `action_required` | `equals` |
| `attachment.media_type` | `equals` |
| `attachment.filename` | `glob` |
| `attachment.byte_size` | `lte` |
| `attachment.count` | `gte`, `lte` |

Sender addresses are normalized; domains and media types are lower-case;
sender names, subjects, and filename patterns are case-folded. Filename
patterns contain no path separators. `attachment.count` accepts 0 through 64
as a rule threshold, but evaluation compares it with the actual persisted
attachment count and makes no 64-attachment workload claim.

### Open-input parser default

JSON object shape is open input. Admission is a single strict Pydantic model:
only positively recognized members and values are stored. Malformed,
ambiguous, extra, or oversized input is rejected. The property tests derive
both accepted and rejected shapes from the canonical model and field/operator
matrix; they do not grow a denylist from review examples.

## 5. Rule authority and mutation API

The schema contains:

- singleton `automation_rule_set(revision)`;
- current `automation_rules` identities and flags;
- immutable `automation_rule_versions` with definition/tombstone kind,
  canonical definition bytes and digest, enabled state, global revision,
  retirement revision, and accepted timestamp.

Every mutation holds `BEGIN IMMEDIATE`, increments the global revision once,
retires the prior version, and inserts one immutable successor. Direct version
updates and deletes are rejected by database triggers. No compaction exists in
this slice; every historical version remains available while any fire can refer
to it.

Create omits `rule_id` and `expected_version`. Edit, delete, and enable/disable
require the current version read by the caller. The comparison occurs inside
the same write transaction before a no-op or new revision. A stale request
returns `stale_rule`; it never silently overwrites an intervening edit or
enablement change.

Engine operations:

- `automation.rules.list` returns the global revision and bounded summaries of
  live rules.
- `automation.rules.get` returns one rule and its canonical definition.
- `automation.rules.put` creates from `{definition}` or edits from
  `{rule_id, expected_version, definition}`.
- `automation.rules.delete` accepts `{rule_id, expected_version}` and writes a
  tombstone version.
- `automation.rules.set_enabled` accepts
  `{rule_id, expected_version, enabled}`.

Public callers cannot set `system`, revision values, timestamps, digests, or
version numbers. Errors are `invalid_rule`, `stale_rule`, `rule_limit`,
`system_rule_protected`, or `not_found`. List is summary-only so the maximum
rule count cannot create a response containing every maximum-sized definition.

A definition is parsed and canonically serialized inside `Store.put_rule`.
Reads parse again. A row made invalid by external database corruption is shown
as invalid with a bounded reason, never evaluated, and never causes another
rule to become an action.

## 6. Atomic analysis-time evaluation

`mark_analyzed` remains the single commit boundary:

1. Begin `IMMEDIATE` and require the message to be pending.
2. Read the singleton rule-set revision.
3. Read current, enabled, non-deleted definition versions, ordered by rule
   creation time and `rule_id`.
4. Parse each definition and exclude invalid rows.
5. Read the message and its persisted attachment descriptors ordered by
   position and `part_id`.
6. Run the pure matcher in memory. It performs no network, filesystem, model,
   Connect, calendar, or notification call.
7. Update the message analysis and set `rules_revision_at_analysis` to the
   exact revision read in step 2.
8. Insert every matched fire and its attempt-one identity.
9. Run the existing scheduling-admission block unchanged.
10. Commit.

Any exception before commit rolls back the analysis, revision marker, fires,
attempts, and scheduling admission. The pending message can be retried through
the existing path. There is no historical catch-up query,
`rules_evaluated_version`, progress handler, deadline, partial checkpoint, or
public `record_rule_evaluation` API.

Because rule writes and analysis both use `BEGIN IMMEDIATE`, a concurrent
analysis sees the complete rule set before or after a mutation, never a mixed
revision. Historical versions remain for fire explanation; they are not
searched to evaluate a newly completed analysis.

Migration behavior:

- messages already analyzed when schema 21 is installed receive
  `rules_revision_at_analysis = 0` and never fire retroactively;
- pending messages retain `NULL` until their first successful analysis;
- unpublished local draft databases are not a compatibility target.

## 7. Matching and fire identity

Scope provider/account comparisons are exact. Message-level conditions are
evaluated once; attachment-level conditions are evaluated for each descriptor.
All conditions are ANDed. A rule that matches two attachments creates two
fires; two matching rules may create independent fires for the same attachment.

Matching uses only:

- provider and account id;
- normalized sender, optional sender name, and subject;
- model category, priority, and action-required boolean;
- attachment media type, final filename component, byte size, count, position,
  and part id.

It never reads the body, headers, generated summary, suggested action, source
bytes, provider catalog, or entitlement state.

Each fire stores an engine-generated UUID, stable source event digest,
`rule_id`, immutable rule version, non-null artifact key, message/part ids,
`connect.invoke`, `pending_dispatch`, state version 1, and timestamps. A unique
constraint on `(event_id, rule_id, rule_version, artifact_key)` prevents a
duplicate committed match. Insert guards require the referenced definition
version and message attachment to exist.

Each fire receives attempt 1 with one UUID `dispatch_request_id` before any
future provider call. No job is created or submitted here. `confirm_each`
remains part of the immutable rule definition; confirmation is evaluated only
by the later preparation/dispatch contract after real bytes and provider
effects can be checked.

Deleting a message before any dispatch mechanism exists silently deletes its
pending fires and attempts. It does not create a review or completion intent.
Rule deletion does not rewrite already committed fires.

## 8. Acceptance evidence

### Rule authority

- Parser boundaries cover unknown members, unsupported action kinds,
  field/operator pairs, normalization, all size/count boundaries, and opposite
  controls.
- Store tests prove canonical storage, immutable history, live-rule cap, system
  protection, and rejection of invalid definitions at the Store boundary.
- Edit/delete/enable tests prove current versions succeed and stale versions
  fail, including a stale enablement no-op and two concurrent writers.
- Engine API tests exercise all five operations and every error mapping.
- Schema 19 to 20 migration preserves existing data.

### Atomic evaluation

- Matcher tests cover every canonical field/operator pair, scope, missing
  optional data, case-folding, final-component filename globs, actual
  attachment counts above 64, mixed conditions, and deterministic order.
- A failure injected between analysis update and fire insertion leaves the
  message pending with no marker, fire, attempt, or scheduling admission.
- Concurrent rule mutation and analysis commit one coherent revision.
- Duplicate fire, nonexistent rule version, and nonexistent attachment inserts
  are rejected.
- Schema 20 to 21 migration marks historical analyzed messages revision 0 and
  leaves pending messages null.
- A real engine `watcher.check` request with test mailbox/model adapters creates
  the expected durable fire and attempt through the production dispatcher and
  performs zero provider submissions.
- Existing scheduling, Connect queue, and full pytest suites remain green;
  Ruff reports clean code and formatting.

## 9. Review-thread disposition

- **Oversized history starvation / resumable snapshot:** confirmed defects in
  the unpublished split evaluator. The split and historical query are removed;
  no compaction or checkpoint mechanism is needed for current-rule evaluation.
- **Attachment cap:** the 64-message-attachment premise is false. The matcher
  uses the actual persisted descriptors; 64 remains only a rule-threshold input
  bound and is not used as deadline evidence.
- **Stale rule mutations:** confirmed. Every existing-rule mutation uses
  expected-version compare-and-set inside its write transaction.
- **Version retention:** current draft triggers already reject every deletion.
  This contract specifies no compaction, so versions referenced by fires remain.
- **Removal review intent:** the old invariant contradicted silent removal.
  Pending, unsubmitted core fires are deleted silently with their source.
- **Re-analysis snapshot:** contradicted by the executable API, which requeues
  only still-pending permanent failures. This slice adds no analyzed-message
  rewrite path.
- **Dispatch SQL deadline:** no dispatch phase or ten-minute deadline exists in
  this scope.
- **Prepared descriptor retention:** no prepared descriptor is stored in this
  scope.
- **Calendar tombstone retention:** no calendar rule action, fire link, or
  calendar cleanup change exists in this scope.

## 10. Deferred and explicit non-scope

- Dispatching pending fires into the existing consumer-side Connect queue.
- Entitlement timing and whether an unlicensed match may later dispatch.
- Provider discovery, effects comparison, source-byte hashing, confirmation,
  retries, settlement, notification delivery, and timer wakeups.
- Rules/history/approval user interface.
- `calendar.propose`, `notify`, and replacement of the shipped scheduling
  automation.
- Rule-version compaction.
- Provider-side queueing or any Invoice Processor change. Invoice Processor
  continues to accept one job at a time by design; any future watcher dispatch
  uses the watcher's existing consumer-side queue.
- Windows/macOS release artifacts, signing, auto-update, icons, and unrelated
  infrastructure issues.

## 11. Landing order

1. Accept and merge this docs-only contract.
2. Land schema 20 rule authority and CRUD.
3. Land schema 21 atomic evaluation and durable fires.
4. Demonstrate the real create-rule to `watcher.check` to pending-fire path.
5. Choose a separate dispatch/UI slice only after that proof is green.

## 12. Revision history

- **Core revision 1 (2026-09-12):** replaced the unimplemented all-future
  engine/dispatch design with the code-derived Connect-only atomic core;
  classified the current review claims against executable code; added stale
  mutation CAS, Store-boundary validation, closure declarations, and explicit
  non-scope.
