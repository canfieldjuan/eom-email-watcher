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

- The existing scheduling automation keeps its decision/state behavior; only its
  source identity and joins gain the mailbox key required by schema 20.
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
- `watcher.check` admits only INBOX messages whose normalized sender is already
  in the global allowlist (`service.py:1142-1145`). Rules evaluate that retained
  set; this slice does not widen mailbox discovery.

## 3. Scope and reachability

The implementation lands as one vertical schema-20 code PR after this contract
is accepted. Strict definitions, immutable rule authority, compare-and-set
mutation, engine API CRUD, the pure matcher, message revision capture, durable
pending fires, and attempt-one dispatch identities become reachable together.
There is no deployable state in which callers can save rules while
`watcher.check` omits evaluation.

Reachability proof:

- A rule is created through the real engine API request dispatcher.
- A real `watcher.check` request processes an allowlisted pending INBOX message
  through the existing mailbox/model orchestration.
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

The definition has exactly these members:

- `name`: 1 through 80 printable characters;
- `scope`: the source selector below, defaulting to `{}`;
- `trigger`: exactly `{source_kind: "mail.message"}`;
- `conditions`: 1 through 8 condition objects;
- `action`: the one closed action below;
- `confirm_each`: strict boolean, defaulting to false.

Every definition has a `scope` object, defaulting to `{}`:

- optional `provider` uses the Connect `Identifier` pattern and 100-character
  maximum;
- optional `account_id` is exact trimmed text of 1 through 128 characters and
  is accepted only when `provider` is also present;
- omitted members are wildcards, so `{}` matches every retained mailbox
  account.

An account-scoped version also stores an engine-owned
`scope_mailbox_identity_key`. The 64-character IMAP key is SHA-256 over the
UTF-8 NUL-joined literal `imap-mailbox-v2`, the existing
`imap_mailbox_identity` credential hash, and the base-10 `UIDVALIDITY` observed
from the authenticated read-only INBOX. Other keys are the existing Microsoft
`MicrosoftPrincipal.key`, or SHA-256 over the UTF-8 NUL-joined literal
`gmail-credential-v1` and the installed Gmail refresh token. The refresh token
is read under its existing credential lock and is never stored in the database
or returned by the engine; only its one-way credential-epoch key is persisted.
Gmail exposes no immutable principal identifier through its current profile
adapter, so replacing the installed refresh token intentionally creates a new
mailbox identity even when the normalized account address is unchanged. IMAP
credential verification opens INBOX read-only and binds the observed
`UIDVALIDITY`; recreating/resetting the mailbox therefore advances the key even
when host, address, username, and password are unchanged. A verifier derives
the key from installed credentials before an account-scoped rule is accepted
and before `watcher.check` admits messages. `Store.put_rule` binds the verified
key inside its mutation transaction; a missing account or unverifiable identity
is `invalid_rule`.

Schema 20 adds nullable `mailbox_identity_key` to mail accounts and messages.
Every newly admitted message receives the key derived from the gateway that
produced it, not a later account lookup. If credentials were replaced but the
process died before database bookkeeping, the next verifier observes the
installed identity and updates the account key before fetching. Gmail and
Microsoft retain their existing provider-specific baseline-reset behavior. An
IMAP epoch change instead preserves the stale cursor and `last_success_at`; the
existing `changes_since` mismatch must drive `recover_since` from that last
successful observation rather than baselining at the new epoch's highest UID.
The recovered result supplies the new cursor only after admitted messages are
processed. A mismatch can therefore make an old account-scoped rule inert but
cannot let it follow a new mailbox or skip mail already present in the new IMAP
epoch. Microsoft or Gmail address mismatches remain rejected. Provider-only and
wildcard rules intentionally span mailbox keys.

Schema 20 also makes the mailbox key part of every persisted source identity:
message lookup and uniqueness, `has_seen_message`, retained suppression keys,
and source-event digests use `(provider, account_id, mailbox_identity_key,
provider_message_id)`. The old three-part message uniqueness index is removed.
For an upgraded account, schema 20 records a separate immutable
`legacy_mailbox_identity_key` when the first production verifier establishes the
current key. Legacy null-key message rows and deletion suppressions participate
in `has_seen_message` only while the admitted key equals that one migration key;
their original suppression expiry remains unchanged. A later credential/epoch
change advances only the current key, so legacy seen/deleted markers cannot
follow a replacement mailbox. A provider message id reused by that replacement
therefore denotes a new message rather than colliding with the old namespace.

Before `_process_pending` fetches content for any retained row, it requires the
row's non-null mailbox key to equal the current gateway key. A null or mismatched
row is recorded as a non-retryable `mailbox_identity_unverified` analysis
failure; its provider message id is never fetched through the replacement
gateway, its attachment descriptors are not replaced, and no rule is evaluated.

That pre-fetch check is not the concurrency fence. Each polling session captures
the gateway key it opened, and schema-20 `add_message` and `set_state` accept it
as an expected key. Each mutation uses one transaction to compare that key with
the current `mail_accounts.mailbox_identity_key` before inserting a message or
advancing the cursor. `mark_analyzed` repeats the same comparison inside its
atomic analysis/fire transaction. A concurrent credential change makes an old
session fail with `mailbox_identity_changed`; it cannot admit more messages,
overwrite the replacement mailbox's reset cursor, or commit an old-key fire
after the account key advances. Credential reconciliation advances the account
key and applies the provider-specific cursor transition in one database
transaction. This compare-and-set fence is used instead of holding a
credential-file lock across network/model work.

Dry-run is strictly non-mutating but holds the same production mailbox operation
lock as a real check for its entire preview, including runtime/gateway
construction, identity comparison, mailbox reads, and model analysis. It never
reconciles the key, resets a cursor, binds a rule, inserts a message, or advances
state. A mismatch returns `mailbox_identity_changed` before mailbox polling or
analysis, leaving production state untouched. Unsupported locking returns
`unsupported_platform`; contention returns retryable `mailbox_busy`, matching
rule mutations.

### Closure declaration: source scope

1. **Membership:** CLOSED for the two scope members, `provider` and
   `account_id`; their string values are open but bounded.
2. **Source:** ENUMERATED in the canonical `Scope` model. Parser and matcher use
   that model rather than copying its members.
3. **Outside behavior:** unknown members, malformed values, or account ids
   without a provider are rejected as `invalid_rule`. A well-formed provider
   value that does not exist matches nothing. A named account that does not
   exist is rejected by `Store.put_rule` while binding its mailbox identity.

### Closure declaration: action kinds

1. **Membership:** CLOSED. The only admitted action is `connect.invoke`.
2. **Source:** ENUMERATED here for this slice. Calendar and notify actions are
   explicitly deferred rather than inferred from existing subsystems.
3. **Outside behavior:** any other `kind` is rejected as `invalid_rule`; this is
   the safe side because no durable or external action is authorized.

The `connect.invoke` action object contains exactly:

- `kind: "connect.invoke"`;
- `capability.id` and `provider.app_id` use the canonical Connect `Identifier`:
  strict lower-case ASCII matching `^[a-z0-9]+(?:[.-][a-z0-9]+)*$`, maximum
  100 characters;
- `capability` contains exactly `id` and `version`; `provider` contains exactly
  `app_id`, `version`, and `instance_id`. `capability.version` uses the canonical Connect
  `CapabilityVersion`: strict text matching `^[0-9]+\.[0-9]+$`;
- `provider.version` uses the canonical Connect app-version pattern
  `^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$`; `provider.instance_id`
  is a lower-case UUIDv4;
- `parameters` has at most sixteen entries. Every key is an `Identifier`; every
  value is exactly a strict string of at most 1,000 characters, a strict integer
  from -9,007,199,254,740,991 through 9,007,199,254,740,991, or a strict
  boolean. Null, floating-point, array, and object values are rejected.

`confirm_each` remains the top-level definition member, not an action member.

The canonical value types and bounds are shared with or mechanically compared
against `connect.py`; the rule model may not define a looser parallel wire type.
Rule creation records the caller-selected provider identity without discovering
or substituting another registration. The immutable rule version therefore
preserves the exact app id, app version, and instance id for a future dispatcher;
an unavailable identity may fail later but is never silently re-resolved.

It must include an `attachment.media_type` condition, so every admitted action
selects a concrete persisted attachment.

### Closure declaration: condition fields and operators

1. **Membership:** CLOSED. The canonical vocabulary is the `ConditionField`,
   `ConditionOp`, and field-to-operator map in the rule-definition module.
2. **Source:** ENUMERATED in that module; parser and matcher import the same
   definitions rather than copying the matrix.
3. **Outside behavior:** unknown fields/operators and unsupported pairs are
   rejected as `invalid_rule`, so novel input cannot become an action.

Every condition object contains exactly the required members `field`, `op`, and
`value`. No aliases, defaults, or extra members are accepted.

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

Each admitted field/operator pair has one operand schema:

| Field and operator | Admitted operand |
|---|---|
| `sender equals` | normalized address, 1..320 characters |
| `sender domain_equals` | lower-case DNS domain, 1..253 characters |
| `sender_name equals/contains` | case-folded non-empty text, at most 320 characters |
| `subject contains/starts_with` | case-folded non-empty text, at most 4,096 characters |
| `category equals` | one of `invoice`, `scheduling`, `customer_request`, `automated_notice`, `informational`, `other` |
| `category in` | 1..6 distinct members of that closed category set |
| `priority equals` | one of `urgent`, `high`, `normal`, `low` |
| `priority in` | 1..4 distinct members of that closed priority set |
| `action_required equals` | strict boolean |
| `attachment.media_type equals` | lower-case media type matching the Connect pattern, at most 127 characters |
| `attachment.filename glob` | case-folded non-empty pattern, at most 512 characters, with no `/` or `\\` |
| `attachment.byte_size lte` | strict integer from 0 through 104,857,600 |
| `attachment.count gte/lte` | strict integer from 0 through 64 |

Booleans are not integers for numeric operands. `in` never accepts a scalar;
`equals` never accepts a list. `attachment.count` is only a rule-threshold
bound: evaluation compares it with the actual persisted attachment count and
makes no 64-attachment workload claim.

`AttachmentDescriptor.byte_size` and its existing persisted column remain
non-null non-negative integers so attachment export, capability discovery, and
interactive Connect invocation keep their current API shape. Schema 20 adds the
strict boolean `byte_size_known` to the descriptor and table. Gmail, Microsoft,
and IMAP set it false when the provider omits the size or supplies a malformed
value; the compatibility integer remains zero in that case. A numeric
`attachment.byte_size lte` condition first requires `byte_size_known`, so an
unknown-size descriptor never matches and a real zero-byte attachment matches
only when the provider explicitly supplied verified integer zero. Other
attachment predicates and `attachment.count` may still use that descriptor.

Filename matching is platform-independent: replace `\\` with `/` in the
persisted filename, take the final slash-delimited component, case-fold both it
and the admitted pattern, then apply Python `fnmatch.fnmatchcase`. The pattern
grammar is exactly `*`, `?`, `[seq]`, and `[!seq]` as implemented by `fnmatch`;
there is no escape syntax. Patterns themselves may contain neither slash kind.

### Open-input parser default

JSON object shape is open input. The top-level engine decoder uses duplicate
member detection at every object depth before `_response`; duplicate members
return `invalid_json` rather than last-value-wins. Definition admission then
uses one strict Pydantic model: only positively recognized members and values
are stored. Malformed, ambiguous, extra, or oversized input is rejected. The
property tests derive both accepted and rejected shapes from the canonical
model and field/operator matrix; they do not grow a denylist from review
examples.

## 5. Rule authority and mutation API

The schema contains:

- singleton `automation_rule_set(revision)`;
- current `automation_rules` identities and flags;
- immutable `automation_rule_versions` with definition/tombstone kind,
  canonical definition bytes and digest, enabled state, optional mailbox
  identity key, global revision, and accepted timestamp.

`MAX_AUTOMATION_RULES` is 100 live, non-deleted rules. Create counts live rules
inside its `BEGIN IMMEDIATE` transaction and returns `rule_limit` instead of
admitting the 101st. List therefore returns at most 100 summaries, and analysis
evaluates at most 100 current definitions. Tombstones and immutable retired
versions do not consume a live-rule slot.

Every mutation holds `BEGIN IMMEDIATE`, increments the global revision once,
inserts one immutable successor, and moves the current-rule pointer. Version
rows have no mutable retirement column: a version is superseded at the next
version's revision, or remains current in `automation_rules`. Database triggers
reject every version-row update and delete. No compaction exists in this slice;
every historical version remains available while any fire can refer to it.

Create omits `rule_id` and `expected_version`. Edit, delete, and enable/disable
require the current live version read by the caller. The comparison occurs
inside the same write transaction before a no-op or new revision. A stale
request returns `stale_rule`; it never silently overwrites an intervening edit
or enablement change. Tombstoned identities cannot be edited, enabled, disabled,
or deleted again and return `not_found`; resurrection is not a core operation.

After a successful expected-version comparison, setting `enabled` to its
current value is a true no-op: it returns the current summary without changing
the rule version, global revision, or timestamps. A stale same-value request
still returns `stale_rule` because compare-and-set precedes the no-op check.

A value-changing enable/disable successor copies the prior immutable version's
`scope_mailbox_identity_key`; toggling a rule never rebinds it to whatever
credentials happen to be installed at mutation time. An explicit definition
edit resolves the current credential-backed key and therefore is the only core
mutation that can intentionally rebind an account-scoped rule.

Every rule mutation runs under the same production mailbox operation lock used
by non-dry-run `watcher.check` and mail-account reconnect/authorization. For an
account-scoped create or definition edit, the lock covers credential verification
through the successful Store commit. `Store.put_rule` still compares the
verified key with the current account key inside `BEGIN IMMEDIATE`; a mismatch
rejects the mutation as `invalid_rule` rather than returning a rule already inert
for the active mailbox. Provider-only and wildcard mutations take the same lock
so mutation behavior does not depend on definition shape.

If native operation locking is unavailable, a rule mutation returns
`unsupported_platform` before credential or Store access. Lock contention with a
poll or account mutation returns retryable `mailbox_busy`; it never falls through
to the generic `runtime_error`. The five rule operations otherwise retain the
closed domain-error set below.

New rules are enabled and non-system by default. Public create cannot override
either flag.

Engine operations:

- `automation.rules.list` returns the global revision and bounded summaries of
  live rules.
- `automation.rules.get` accepts exactly `{rule_id}` and returns one rule and
  its canonical definition.
- `automation.rules.put` creates from `{definition}` or edits from
  `{rule_id, expected_version, definition}`.
- `automation.rules.delete` accepts `{rule_id, expected_version}` and writes a
  tombstone version.
- `automation.rules.set_enabled` accepts
  `{rule_id, expected_version, enabled}`.

Each operation uses an exact strict payload model. `rule_id` is a lower-case
UUIDv4, `expected_version` is a strict integer from 1 through
9,223,372,036,854,775, and `enabled` is a strict boolean. Unknown members,
booleans used as versions, zero/negative/overflowing versions, missing members,
and wrong types return `invalid_request` before Store access. List accepts an
empty payload only; create/edit are the two exact `put` shapes above.

Successful result objects are exact:

- `RuleSummary` is `{rule_id, version, enabled, system, valid, name,
  invalid_reason, created_at, updated_at}`. Version is the current positive
  integer; `name` is the parsed name or null; `invalid_reason` is null for a
  valid row and bounded text otherwise; timestamps are UTC ISO-8601 strings.
- `RuleDetail` is `{summary, definition}` where `definition` is the canonical
  object for a valid current version and null for an invalid row.
- list returns `{revision, rules}` with a non-negative global revision and
  summaries; get, put, and set-enabled return `{rule: RuleDetail}`; delete
  returns `{rule_id, version, deleted: true}` for the tombstone version.

List reads the singleton revision and all summaries inside one explicit SQLite
read transaction, so the returned revision describes the returned rule set.
Get reads the current identity, summary, canonical definition bytes, and digest
from one joined query or one explicit SQLite read transaction, so every member
of `RuleDetail` describes the same immutable version.

Public callers cannot set `system`, revision values, timestamps, digests, or
version numbers. Domain errors are `invalid_rule`, `stale_rule`, `rule_limit`,
`system_rule_protected`, or `not_found`; mutation lock errors are the
`unsupported_platform` and retryable `mailbox_busy` responses above. List is
summary-only so the maximum rule count cannot create a response containing every
maximum-sized definition.

A definition is parsed and canonically serialized inside `Store.put_rule`.
After strict validation, all defaulted members are materialized and the canonical
Pydantic object is dumped in JSON mode. Canonical bytes are UTF-8 encoding of
`json.dumps` with `sort_keys=True`, `separators=(",", ":")`,
`ensure_ascii=False`, and `allow_nan=False`; there is no BOM or trailing newline.
The 16 KiB definition limit applies to the length of these canonical UTF-8 bytes,
not the raw request or Unicode character count. Reads first recompute SHA-256
over the canonical bytes and compare it with the stored lower-case digest, then
parse again. A digest mismatch or parse failure is shown as invalid with a
bounded reason, never evaluated, and never causes another rule to become an
action.

## 6. Atomic analysis-time evaluation

`MAX_AUTOMATION_ATTACHMENTS` and `MAX_AUTOMATION_FIRES_PER_MESSAGE` are both
1,000. Inside `mark_analyzed`, the Store counts persisted descriptors before
loading any into the automation matcher, and the matcher stops before retaining
candidate fire 1,001. Either overflow commits the email analysis, current
rule-set revision, and existing scheduling admission with
`rules_evaluation_error = "automation_fanout_limit"`, creates zero fires or
attempts, and never evaluates or commits a truncated descriptor/fire subset.
Normal evaluation clears the nullable error field. Provider MIME traversal,
attachment-name materialization, and descriptor persistence predate this engine
and remain unchanged; bounding that shared ingestion path is explicitly deferred
rather than smuggled into the Automate core.

`mark_analyzed` remains the single commit boundary:

1. Begin `IMMEDIATE` and require the message to be pending.
2. Read the singleton rule-set revision.
3. Read current, enabled, non-deleted definition versions, ordered by rule
   creation time and `rule_id`.
4. Verify each definition digest, parse it, and exclude invalid rows.
5. Read the message, its captured mailbox identity key, and its persisted
   attachment descriptors ordered by position and `part_id`.
6. Run the pure matcher against that source data and the validated incoming
   analysis `result` passed to `mark_analyzed`. It never reads the still-pending
   message's unset analysis columns and performs no network, filesystem, model,
   Connect, calendar, or notification call.
7. Update the message analysis and set `rules_revision_at_analysis` to the
   exact revision read in step 2.
8. Insert every matched fire and its attempt-one identity.
9. Run the existing scheduling-admission decision with the current message's
   mailbox key; only source identity plumbing changes.
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

Schema-20 migration behavior:

- messages already analyzed when schema 20 is installed receive
  `rules_revision_at_analysis = 0` and never fire retroactively;
- pending messages retain `NULL` until their first successful analysis;
- legacy messages receive `mailbox_identity_key = NULL` because their historical
  mailbox principal cannot be proven; pending legacy rows fail the pre-fetch
  identity check and no account-scoped rule can match them;
- existing accounts remain unverified until the credential-backed identity
  verifier establishes their current key and, exactly once, their immutable
  legacy migration key. Legacy null-key seen/deletion markers apply only when
  the current key still equals that migration key and retain their old expiry;
- a schema-20 `BEFORE INSERT` trigger rejects every new message whose mailbox
  key is null. Rows that already existed when migration began may remain null,
  but an already-running schema-19 process cannot insert another legacy-shaped
  row after migration. Its insert aborts before its later cursor update;
- a schema-20 trigger rejects any `pending` to `analyzed` transition whose
  `rules_revision_at_analysis` remains null. An already-running schema-19
  process that reaches its old completion SQL after migration therefore rolls
  back instead of bypassing evaluation; a transaction that completed before
  migration is assigned revision 0 before rule CRUD becomes available;
- unpublished local draft databases are not a compatibility target.

Schema 20 also adds nullable `source_mailbox_identity_key` to existing
`automation_runs`. New scheduling admission accepts the analyzed message's
non-null mailbox key and derives `source_message_key` from the same four-part
source tuple as messages and fires. Every scheduling join requires the run and
message mailbox keys to be equal in addition to the digest. Migrated runs keep a
null source key and their legacy three-part digest; they may join only legacy
null-key messages and can never collide with or attach to a newly admitted
verified-key message. The existing scheduling states and transitions otherwise
remain unchanged.

## 7. Matching and fire identity

Scope provider/account comparisons are exact. An account-scoped rule also
requires its captured mailbox identity key to equal the message key; null never
equals a scoped key.
Message-level conditions are evaluated once; attachment-level conditions are
evaluated for each descriptor. All conditions are ANDed. A rule that matches
two attachments creates two fires; two matching rules may create independent
fires for the same attachment.

Matching uses only:

- provider and account id;
- normalized sender, optional sender name, and subject;
- model category, priority, and action-required boolean;
- attachment media type, final filename component, byte size, count, position,
  and part id.

It never reads the body, headers, generated summary, suggested action, source
bytes, provider catalog, or entitlement state.

Only messages retained by the existing INBOX, sender-allowlist, and retention
gates can reach matching. A rule does not expand discovery, and a syntactically
valid sender operand outside that retained set may remain inert.

Each fire stores an engine-generated UUID, stable source event digest,
`rule_id`, immutable rule version, required message/part ids,
`connect.invoke`, `pending_dispatch`, state version 1, and timestamps. The event
digest is SHA-256 over the UTF-8 NUL-joined source provider, account id,
mailbox identity key, and provider message id, matching the schema-20
`message_source_key` derivation. A
unique constraint on `(message_id, part_id, rule_id, rule_version)` prevents a
duplicate committed match without relying on generated values. Insert guards
require the referenced definition version and exact message attachment to
exist.

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

- Raw decoder tests prove duplicate members at the request, definition, action,
  and condition levels return `invalid_json`; distinct-member controls pass.
- Parser boundaries cover unknown members, unsupported action kinds, canonical
  Connect identifiers/capability versions/app versions/provider instances/
  parameter scalars, every field/operator operand schema, normalization, all
  size/count boundaries, and opposite controls.
- Store tests prove canonical storage, immutable successor history, live-rule
  cap, system protection, digest verification, mailbox identity binding,
  coherent list/get snapshots, tombstone non-resurrection, and rejection of
  invalid definitions at the Store boundary.
- Canonical-byte tests prove raw key order, insignificant whitespace, explicit
  versus omitted defaults, and equivalent non-ASCII text produce the specified
  bytes/digest; 16,384 canonical UTF-8 bytes pass and 16,385 fail.
- Edit/delete/enable tests prove current versions succeed and stale versions
  fail, including current and stale same-value enablement plus two concurrent
  writers. A current same-value enablement preserves version, revision, and
  timestamps. A value-changing toggle preserves the prior mailbox identity key;
  an explicit definition edit can bind the current key.
- A rule-binding race test pauses after credential verification, attempts a
  concurrent account reconnect, and proves the shared production operation lock
  serializes them. The Store-side expected-key comparison independently rejects
  a mismatched key.
- Rule-mutation lock tests prove unsupported native locking returns
  `unsupported_platform`, contention returns retryable `mailbox_busy`, and
  neither path reaches credential verification or Store mutation.
- Engine API tests exercise all five strict request and response schemas plus
  every error mapping. Create returns an enabled, non-system version 1 rule.
- Schema 19 to 20 migration preserves existing data while installing rule
  authority and atomic evaluation together.

### Atomic evaluation

- Matcher tests cover every canonical field/operator pair, scope, missing
  optional data, case-folding, the exact cross-platform `fnmatchcase` filename
  algorithm, actual attachment counts above 64, mailbox identity mismatch,
  unknown versus verified-zero attachment size, incoming analysis values versus
  null stored columns, mixed conditions, and deterministic order.
- Boundary tests prove 1,000 persisted descriptors and 1,000 matched fires can
  commit. Persisted descriptor 1,001 is detected before automation descriptors
  are loaded; it and matcher candidate 1,001 both commit the analysis, scheduling
  admission, and `automation_fanout_limit` with zero fires/attempts.
- A failure injected between analysis update and fire insertion leaves the
  message pending with no marker, fire, attempt, or scheduling admission.
- Concurrent rule mutation and analysis commit one coherent revision.
- Repeating one `(message_id, part_id, rule_id, rule_version)` fire is rejected;
  a different part or rule version passes. Nonexistent rule-version and
  message-attachment inserts are rejected.
- The schema-20 migration marks historical analyzed messages revision 0 and
  leaves pending messages' revision null and every legacy message's mailbox key
  unverified.
- Credential-backed preflight preserves a matching IMAP or Microsoft identity
  key. After replacement or an injected crash between file install and database
  bookkeeping, it establishes the new key and provider-specific cursor transition
  before fetching; new messages cannot inherit the old key. Existing
  address-mismatch rejection remains unchanged.
- IMAP tests hold credential fields constant while changing `UIDVALIDITY` and
  prove the mailbox key advances, the stale cursor drives recovery from the
  prior `last_success_at`, mail already present in the new epoch is admitted,
  old scoped rules become inert, and newly admitted messages use the new key. An
  unchanged epoch is the opposite control.
- Gmail tests prove the credential key changes when the installed refresh token
  changes even when the normalized account address does not, and that the raw
  token is never persisted or exposed.
- A polling-session race test pauses an old-key gateway before message insert,
  advances the account key and applies its cursor transition, then proves the old
  session can neither insert, advance the cursor, nor commit analysis/fires. The
  opposite same-key control completes each mutation.
- A dry-run with matching identity performs its existing preview under the
  production mailbox operation lock without any write. A race test blocks a
  reconnect until preview completion. A dry-run with an installed/stored
  identity mismatch returns `mailbox_identity_changed` and leaves the account
  key, cursor, messages, rules, analyses, and fires byte-for-byte unchanged.
- A pending row with a null or stale mailbox key is failed before content fetch;
  a reused provider message id cannot replace its descriptors through the new
  gateway.
- Message admission tests prove the same provider message id under a replacement
  mailbox key creates a distinct message, while a true replay under the same key
  remains deduplicated. Legacy null-key rows/suppressions prevent replay only for
  the first verified migration key and do not suppress a later replacement key.
- Scheduling tests admit the same provider message id under two mailbox keys and
  prove distinct source keys/runs with joins confined to the matching key.
  Migrated null-key runs join only legacy null-key messages.
- An old schema-19 completion started after migration is rejected by the null
  revision trigger; the message remains pending for schema-20 evaluation.
- An old schema-19 message insert started after migration is rejected by the
  null-key insert trigger before that process can advance the mailbox cursor;
  rows present before migration remain readable and unverified.
- The real reachability test uses an allowlisted INBOX sender. Its opposite
  control proves a non-allowlisted sender is not stored or evaluated.
- A real engine `watcher.check` request with test mailbox/model adapters creates
  the expected durable fire and attempt through the production dispatcher and
  performs zero provider submissions.
- Existing scheduling, Connect queue, and full pytest suites remain green;
  Ruff reports clean code and formatting.

## 9. Review-thread disposition

- **Pending-row mailbox substitution:** confirmed. `_process_pending` now has a
  pre-fetch key equality gate; null/stale rows fail without fetching or matching.
- **Automation fan-out:** confirmed. Descriptor and committed-fire ceilings have
  an explicit all-or-zero terminal error state.
- **Tombstone resurrection:** confirmed. Every later mutation returns
  `not_found`; only create can introduce a live identity and consume a slot.
- **Get request shape:** confirmed. The exact strict payload is `{rule_id}`.
- **Reauthorization crash window / legacy mailbox identity:** confirmed. Stable
  credential-derived keys replace inferred numeric incarnations; preflight
  reconciles installed credentials before fetch and legacy messages remain
  unbound.
- **Gmail principal identity:** confirmed. Because the current gateway exposes no
  immutable Gmail principal identifier, the mailbox key is a one-way credential
  epoch derived from the installed refresh token; replacement cannot inherit the
  prior rule or message namespace.
- **Enablement rebinding:** confirmed. Value-changing toggles copy the prior
  immutable mailbox key; only an explicit definition edit resolves and binds the
  current credential key.
- **Cross-mailbox message deduplication:** confirmed. Message uniqueness, seen
  checks, suppression keys, and source-event digests include the mailbox key;
  legacy null-key records apply only to the immutable first migration key and
  cannot suppress a later replacement mailbox.
- **In-flight credential replacement:** confirmed. Message admission, cursor
  advancement, and atomic analysis/fire commit compare the polling session's
  expected key with the current account key; a stale session cannot write after
  reconciliation advances the key.
- **Pre-materialization descriptor cap / Gmail duplicate candidates:**
  waived-out-of-scope. Provider MIME traversal, attachment-name materialization,
  and shared descriptor persistence predate this engine. The core bounds only
  its added work by counting persisted descriptors before matcher loading and
  committing zero automation fires on overflow.
- **Rule-detail snapshot:** confirmed. Summary and definition are read from one
  immutable-version snapshot.
- **Schema-19 post-migration insert:** confirmed. A schema-20 insert trigger
  rejects new null-key messages while preserving rows that predate migration.
- **Unknown attachment sizes:** confirmed. Unknown sizes retain the compatibility
  integer but set `byte_size_known` false and fail numeric predicates; only a
  provider-supplied verified integer can satisfy `attachment.byte_size lte`.
- **IMAP mailbox epoch:** confirmed. The binding includes the authenticated
  INBOX `UIDVALIDITY`, so a recreated/reset mailbox cannot inherit scoped rules
  merely by reusing credential fields.
- **Dry-run reconciliation:** confirmed. The mailbox operation lock covers the
  complete read-only preview; dry-run compares identities and fails on mismatch
  without reconciling credentials or mutating production state.
- **IMAP epoch recovery:** confirmed. Reconciliation preserves the stale cursor
  and last-success timestamp so the existing recovery scan admits new-epoch mail
  before committing its cursor.
- **Rule-binding race:** confirmed. The production mailbox operation lock spans
  credential verification through rule commit, with a Store-side expected-key
  comparison as the transaction boundary.
- **Existing attachment-size consumers:** confirmed. The shared byte-size value
  stays a non-null integer; a separate known-size bit gates rule matching without
  changing existing export/discovery/invocation types.
- **Rule-mutation lock failures:** confirmed. Unsupported locking and contention
  have explicit `unsupported_platform` and retryable `mailbox_busy` responses.
- **Canonical definition bytes:** confirmed. Defaults, JSON-mode conversion,
  key ordering, separators, Unicode encoding, finite-number handling, and the
  post-canonicalization byte limit are exact.
- **Scheduling on descriptor overflow:** confirmed. Overflow disables only
  automation fire creation; the existing analysis and scheduling commit remains
  unchanged.
- **Legacy deletion suppressions:** confirmed. Legacy markers remain effective
  through their original expiry for the first verified migration key, but never
  follow a later credential/epoch key.
- **Scheduling source identity:** confirmed. New run identity and every join use
  the mailbox key; migrated null-key runs remain confined to null-key messages.
- **Matcher source-byte caps:** waived-out-of-scope. Persisted mailbox metadata
  is already open-length input; this core bounds rules, descriptors loaded, and
  fires, while per-field/aggregate storage normalization is a separate ingestion
  hardening slice.
- **Internationalized sender-domain equivalence:** waived-out-of-scope. The
  provider-specific Unicode-versus-IDNA sender normalization predates this rule
  engine and does not block the approved ASCII-domain reachability proof; a
  separate normalization slice must align persisted senders and rule operands
  before promising internationalized-domain equivalence.
- **Filename glob semantics:** confirmed. Final-component normalization and the
  exact `fnmatchcase` grammar are now canonical.
- **Retirement immutability:** confirmed. Stored retirement mutation is removed;
  supersession is derived from the next immutable version/current-rule pointer,
  and every version-row update remains forbidden.
- **Create default, strict mutation envelopes, and response schemas:** confirmed.
  Exact request/result models now expose the version required for CAS.
- **Incoming analysis values:** confirmed. The matcher receives the validated
  `mark_analyzed` result rather than reading unset stored columns.
- **Coherent list:** confirmed. Revision and summaries share one read snapshot.
- **Schema-19 in-flight completion:** confirmed. A schema-20 trigger prevents old
  completion SQL from committing a null evaluation marker after migration.
- **Closed action and condition values / duplicate JSON:** confirmed. The
  contract now defines every action and operand type/bound, shares the Connect
  wire constraints, and rejects duplicate members before model validation.
- **Same-value enablement:** confirmed. Expected-version comparison happens
  first; a current same-value request preserves every revision-bearing value.
- **Definition digest:** confirmed against the contract's corruption claim.
  Reads verify the digest before parsing or evaluation.
- **Fire deduplication:** confirmed. The unique key now uses persisted message
  and part identity plus immutable rule identity, with a canonical event digest
  defined separately.
- **IMAP mailbox rebinding:** confirmed by the reconnect path. Account-scoped
  versions bind to the credential-derived mailbox key; wildcard/provider-only
  rules intentionally do not.
- **Microsoft principal rebinding:** confirmed. The binding uses the existing
  immutable Microsoft principal key, not only the presented email address.
- **Connect provider selection:** confirmed. The immutable action carries exact
  app id, app version, and instance id; this core neither discovers nor silently
  substitutes a registration.
- **Sender allowlist:** confirmed as an existing discovery gate. Evaluation and
  reachability cover retained allowlisted INBOX messages only.
- **Source scope definition:** confirmed. Rule definitions now carry a bounded
  provider/account scope with explicit wildcard and invalid-shape behavior.
- **CRUD-before-evaluation deployment gap:** confirmed. Rule authority, CRUD,
  matching, and atomic analysis-time evaluation land together in schema 20.
- **Live-rule bound:** confirmed. The cap is 100 live rules and is enforced
  inside the create transaction before the 101st rule can be admitted.
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
- Per-field or aggregate byte caps for pre-existing persisted mailbox metadata.
- Provider-side queueing or any Invoice Processor change. Invoice Processor
  continues to accept one job at a time by design; any future watcher dispatch
  uses the watcher's existing consumer-side queue.
- Windows/macOS release artifacts, signing, auto-update, icons, and unrelated
  infrastructure issues.

## 11. Landing order

1. Accept and merge this docs-only contract.
2. Land one schema-20 vertical PR containing rule authority, CRUD, atomic
   evaluation, and durable fires.
3. Demonstrate the real create-rule to `watcher.check` to pending-fire path.
4. Choose a separate dispatch/UI slice only after that proof is green.

## 12. Revision history

- **Core revision 1 (2026-09-12):** replaced the unimplemented all-future
  engine/dispatch design with the code-derived Connect-only atomic core;
  classified the current review claims against executable code; added stale
  mutation CAS, Store-boundary validation, closure declarations, and explicit
  non-scope.
- **Core revision 2 (2026-09-12):** defined bounded source scope and the
  100-live-rule cap, and made CRUD plus evaluation one atomic deployment slice
  so no accepted rule can miss messages between schema releases.
- **Core revision 3 (2026-09-12):** closed every action/operand value schema,
  rejected duplicate JSON members, fixed enablement no-op semantics, required
  digest verification and persisted-source dedupe keys, and bound
  account-scoped rules to mailbox identity incarnations.
- **Core revision 4 (2026-09-12):** extended mailbox incarnation checks to
  Microsoft principal changes, pinned actions to exact Connect provider
  instances, and made the existing sender-allowlist discovery gate explicit.
- **Core revision 5 (2026-09-12):** replaced inferred mailbox incarnations with
  credential-derived identity keys and crash preflight; closed request/response,
  condition, glob, retirement, matcher-input, list-snapshot, legacy-message, and
  schema-upgrade completion boundaries.
- **Core revision 6 (2026-09-12):** added the pre-fetch mailbox-key gate,
  deterministic all-or-zero fan-out limits, tombstone non-resurrection, and the
  exact `rules.get` payload.
- **Core revision 7 (2026-09-12):** made Gmail identity a credential epoch,
  preserved mailbox binding across enablement successors, and included mailbox
  identity in message admission, suppression, deduplication, and event identity.
- **Core revision 8 (2026-09-12):** fenced active polls against credential
  replacement, moved descriptor bounding ahead of materialization/persistence,
  made rule-detail reads coherent, and blocked schema-19 inserts after migration;
  internationalized sender-domain equivalence remains deferred.
- **Core revision 9 (2026-09-12):** made unknown attachment sizes non-matching,
  included the IMAP mailbox epoch in identity, and kept credential reconciliation
  out of non-mutating dry runs.
- **Core revision 10 (2026-09-12):** preserved IMAP recovery across epoch
  changes, serialized rule binding with account replacement, and separated
  size-known matching state from the existing non-null attachment-size API.
- **Core revision 11 (2026-09-12):** defined rule-lock errors and canonical
  definition bytes, kept scheduling unchanged on automation fan-out, and
  deferred pre-existing provider descriptor materialization hardening.
- **Core revision 12 (2026-09-12):** serialized read-only previews with account
  replacement, preserved legacy suppression for only the first migration key,
  and extended scheduling source identity/joins with mailbox keys; matcher-source
  byte caps remain deferred ingestion hardening.
