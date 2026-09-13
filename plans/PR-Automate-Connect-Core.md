# Implement the Automate Connect engine core

## Why this slice exists

Email Watcher has durable Connect queue infrastructure and a separate Microsoft
calendar automation, but current `origin/main` has no generic rule authority,
rule API, matcher, or analysis-time creation of Connect work. The merged
`docs/AUTOMATE_RULE_ENGINE_CONTRACT.md` defines the first executable vertical
slice. The local `claude/pr-automate-rules` branch is a read-only donor: its
strict-model and immutable-row patterns are useful, but its public arbitrary
evaluation API, historical snapshot scan, calendar/notify actions, mutable
retirement state, and schema-21 dispatch state are not accepted behavior.

The diff is expected to exceed the normal 400-line target because schema
migration, authority, matching, API reachability, mailbox identity, and atomic
fire creation cannot ship independently: exposing CRUD before analysis-time
evaluation recreates the split trust boundary this slice exists to remove.

### Problem-derived contract

Root cause: rule admission, selected revision, and fire identity were split
across independently callable boundaries in the donor. That permits stored
definitions the parser never accepted, stale overwrites, evaluated revisions
chosen by callers, and fires referring to rule versions or attachments outside
the analysis transaction.

Exact-head review exposed four additional boundary splits in the first runtime
implementation: stale mailbox recovery could cross an unresolved migrated
identity; missing provider size metadata was collapsed into verified zero;
account-scoped stale edits performed credential reconciliation before their
version could reject; and CLI dry-runs constructed mailbox runtime state before
acquiring the production operation lock. These are reachable violations of the
accepted identity, matching, CAS, and locking contracts rather than optional
hardening.

The correct fix must:

1. make strict Connect-only parsing and canonical serialization part of the
   Store mutation boundary;
2. give every existing-rule mutation compare-and-set semantics and retain
   immutable versions/tombstones;
3. evaluate the current enabled rules inside `mark_analyzed`'s existing
   `BEGIN IMMEDIATE` transaction and create only fires derived from rows read in
   that transaction;
4. bind accounts, messages, account-scoped rules, source identities, scheduling
   joins, and provider fetches to a credential-derived mailbox identity;
5. expose the five strict `automation.rules.*` operations through the real
   engine dispatcher; and
6. prove the path from rule creation through a real `watcher.check` request to a
   durable pending fire and attempt-one identity with zero provider submission;
7. keep unresolved migrated mailbox markers fail-closed through their existing
   retention horizon without extending that horizon for schema-20 data;
8. preserve omitted provider attachment sizes as unknown while retaining an
   explicit numeric zero as known;
9. reject missing, protected, and stale rule edits before any credential or
   mailbox-state side effect while retaining the Store's final transactional
   CAS; and
10. acquire the production mailbox lock before CLI dry-run runtime construction,
    matching production checks.

This change fixes the root boundary split. It must not add provider dispatch,
provider-side queueing, an Invoice Processor change, UI, timers, notification
delivery, calendar rule actions, or change the existing consumer-side Connect
queue.

## Scope (this PR)

Ownership lane: email-watcher-automate-connect-engine-core

Slice phase: vertical slice

1. Add the strict Connect-only rule model, canonical bytes, and pure matcher.
2. Install schema 20 rule authority, mailbox/source identity, immutable fire,
   attempt-one, migration, and cross-version safety fences.
3. Add Store CRUD with live-rule cap, expected-version CAS, coherent reads, and
   account-key binding.
4. Compose matching and all-or-zero fire creation into `mark_analyzed` without
   changing scheduling decisions.
5. Add strict engine API CRUD under the production mailbox lock.
6. Reconcile credential-derived mailbox identity before polling and fence
   pending/scheduling provider fetches against replacement credentials.
7. Add focused parser, Store, service, migration, race, and real-dispatcher
   reachability tests.
8. Carry the migrated unresolved-marker horizon until it expires, preserve
   omitted size metadata as unknown, preflight edits before credential
   reconciliation, and route CLI dry-runs through the production lock.

### Files touched

- `scripts/connect-local-proof.py`
- `scripts/connect-packaged-deb-proof.py`
- `plans/PR-Automate-Connect-Core.md`
- `src/eom_email_watcher/automation/__init__.py`
- `src/eom_email_watcher/automation/rules.py`
- `src/eom_email_watcher/cli.py`
- `src/eom_email_watcher/db.py`
- `src/eom_email_watcher/engine_api.py`
- `src/eom_email_watcher/gmail.py`
- `src/eom_email_watcher/imap.py`
- `src/eom_email_watcher/locking.py`
- `src/eom_email_watcher/mailbox.py`
- `src/eom_email_watcher/microsoft365.py`
- `src/eom_email_watcher/mime.py`
- `src/eom_email_watcher/service.py`
- `tests/test_automation_rules.py`
- `tests/test_cli.py`
- `tests/test_connect_engine_api.py`
- `tests/test_connect_local_proof.py`
- `tests/test_connect_packaged_deb_proof.py`
- `tests/test_connect_v2_engine_api.py`
- `tests/test_db.py`
- `tests/test_engine_api.py`
- `tests/test_gmail.py`
- `tests/test_microsoft365.py`
- `tests/test_mime.py`
- `tests/test_service.py`

### Review Contract

Acceptance criteria:

1. `tests/test_automation_rules.py` proves exact Connect-only model admission,
   canonical serialization, every field/operator boundary and opposite control,
   mailbox scope, verified/unknown attachment sizes, and deterministic matching.
2. Schema migration tests in `tests/test_db.py` prove schema 19 upgrades to 20
   atomically, historical analyzed rows receive revision zero, pending rows keep
   a null revision, old null-key inserts/completions are rejected, the scheduling
   table row shape is unchanged, and legacy identities remain unbound without a
   provider-specific witness.
3. Store tests prove canonical write-boundary validation, immutable successors,
   digest verification, coherent list/get snapshots, the live-rule cap,
   system/tombstone protection, current/stale CAS—including same-value enable—and
   account-key comparison inside the mutation transaction. A concurrent rule
   edit and analysis settle on one complete rule-set revision.
4. `tests/test_db.py` proves `mark_analyzed` commits one coherent rule revision,
   analysis, all matching fires, attempt-one identities, and unchanged scheduling
   admission or rolls the whole unit back. Descriptor/fire overflow records
   `automation_fanout_limit` with zero truncated fires.
5. Service tests prove identity reconciliation precedes message admission,
   pending content fetch rejects null/stale keys, IMAP continuity can rebind only
   retained pending rows, replacement credentials cannot advance an old cursor
   or source fetch, and scheduling fetch uses a captured or proven legacy key.
6. Engine API tests prove the five exact request/result envelopes, duplicate-key
   raw JSON rejection, domain error mapping, production-lock failure mapping,
   and no credential/Store access when locking is unavailable or contended.
7. A real dispatcher test creates a rule, sends `watcher.check` through the
   production request handler with fake mailbox/model adapters, observes a
   durable pending fire plus attempt one, and observes zero provider submission;
   a non-allowlisted sender is the opposite control.
8. Existing scheduling, Connect queue, Ruff, formatting, and the full pytest
   suite remain green.
9. Opposite-side tests prove unresolved migrated markers block stale recovery
   only through their inherited horizon, omitted size differs from explicit
   zero, stale edits fail before account reconciliation, and CLI dry-runs load
   runtime only after acquiring the mailbox operation lock.

Affected surfaces: SQLite schema/migration; mailbox polling identity; pending
message fetch; scheduling source joins/fetch; `mark_analyzed`; engine request
decoding/dispatch; rule parsing/matching; durable fire storage.

Risk areas: cross-version migration, credential replacement, transaction
atomicity, CAS concurrency, open JSON input, source deduplication, existing
scheduling compatibility, and accidental provider effects.

Triggered reviewer rules: R1, R2, R3, R4, R5, R6, R7, R8, R10, R11, R12, R13,
and R14. R9 is not triggered because no frontend path changes.

Reachability proof: `engine_api._response` accepts `automation.rules.put`; a
subsequent real `watcher.check` dispatcher call reaches `run_watcher_check`,
`Watcher._process_pending`, and `Store.mark_analyzed`; Store inspection returns
the committed fire and attempt while the fake provider records no submissions.

Closure declaration:

1. Membership is closed over the five rule operations, one `connect.invoke`
   action, the canonical condition field/operator matrix, and the provider
   identities named by the strict models.
2. The source of truth is `automation.rules` for definition/matcher vocabulary,
   the request models for API shapes, and Store schema/helpers for persisted
   identity. Tests import those sources instead of maintaining a second list.
3. Unknown operations/members/actions/operators and ambiguous or malformed input
   fail closed before storage; unverified mailbox identity fails before source
   fetch or durable action creation.

## Mechanism

`automation.rules` owns one strict Pydantic definition model and the pure
metadata matcher. Store parses, materializes defaults, emits canonical UTF-8
JSON, checks the post-canonicalization byte limit, and hashes exactly those bytes
inside `put_rule`. Rule identities point to immutable version rows; mutation
transactions perform expected-version comparison before no-op handling or
successor creation.

Schema 20 adds credential-backed identity to accounts/messages, identity-aware
source uniqueness, rule authority, immutable versions, durable pending fires,
and immutable attempt-one request IDs. New scheduling source identity uses a
one-to-one companion table so existing `automation_runs` row shape is unchanged.
Triggers fence schema-19 null-key message inserts and completions without rule
revision capture.

Migration records one provider/account horizon derived only from pre-schema-20
suppression markers. Stale recovery for an unresolved legacy identity remains
retryably blocked while retained null-key messages or that fixed horizon remain;
new schema-20 suppression activity cannot extend the legacy fence.

Production checks derive a gateway key under the mailbox operation lock,
reconcile it with the account using provider-specific cursor behavior, and pass
that captured key through message admission, cursor updates, pre-fetch checks,
`mark_analyzed`, and scheduling source fetch. Store repeats account-key CAS at
each mutation boundary. IMAP legacy pending rows bind only after stored cursor
credential/UIDVALIDITY continuity proof; migrated Microsoft scheduling runs may
use their existing immutable principal as a run-specific witness.

`mark_analyzed` loads the current valid enabled definitions, incoming validated
analysis, message identity, and persisted descriptors in one write transaction.
It runs the pure matcher, records the exact singleton revision, inserts every
fire and attempt-one identity, performs the existing scheduling admission, and
commits once. Fanout overflow records an evaluation error and creates no partial
automation subset while preserving analysis and scheduling admission.

The engine API uses exact strict payload models. Rule mutations acquire the same
production mailbox lock as production checks; scoped create/edit resolves the
credential identity under that lock and Store verifies it again before commit.
Existing-rule edits first perform a side-effect-free current-version/protection
preflight inside that lock, then retain the Store CAS as the final authority.
CLI dry-run and production checks both acquire the lock before constructing the
runtime or reading mailbox identity.

## Intentional

- The action set contains only `connect.invoke`; existing calendar automation is
  preserved but is not converted into a generic rule action.
- Provider app/version/instance are stored exactly as selected. This slice does
  not discover, substitute, dispatch to, or retry a provider.
- Rules evaluate only messages already retained by the existing INBOX,
  sender-allowlist, and retention gates.
- Existing non-null attachment byte size remains API-compatible; a separate
  known-size bit controls numeric matching.
- An automation overflow does not roll back email analysis or existing
  scheduling admission; it creates zero fires and records the bounded error.
- Historical rule versions remain while fires can refer to them; no compaction
  is introduced.
- The donor branch is never merged or mutated; accepted patterns are rewritten
  against current schema-19 code and this contract.
- Ruff's repository-wide format check currently names 27 untouched baseline
  files. This slice formats and checks all 26 changed Python paths rather than
  widening into an unrelated repository reformat.

## Deferred

Parking predicate: dispatch/retry/timer/confirmation/notification/UI behavior,
provider ingestion hardening, and maintenance-only cleanup remain parked unless
they demonstrate a correctness or safety failure in create-rule-to-durable-fire.

- Provider dispatch, retry transitions, confirmation, timers, notifications,
  generic calendar actions, and UI are future product slices.
- Companion identity retention cleanup is maintenance hardening; the current
  Store does not enable foreign-key cascades, so a later retention slice must add
  an explicit deletion trigger or manual cleanup plus retention tests.
- Lone-surrogate rejection/mapping and internationalized sender-domain
  normalization are parser hardening beyond this vertical proof.
- Provider MIME traversal/materialization byte caps and duplicate Gmail
  descriptor hardening predate this engine.
- Windows/macOS release artifacts, signing, auto-update, icon work, and GPU smoke
  remain outside this runtime slice.
- Provider-side queueing and Invoice Processor changes are explicitly excluded;
  Invoice Processor continues accepting one job at a time.

## Verification

- `uv run ruff check .`
- `uv run ruff format --check <12 review-fix Python paths>` — `12 files already formatted`
- review-fix boundary probes across retained/expired legacy markers,
  omitted/explicit-zero sizes, missing/stale/system edits, and CLI lock ordering
  — `13 passed`
- affected DB/service/provider/API/CLI suites — `443 passed in 48.75s`
- focused parser/matcher tests in `tests/test_automation_rules.py`
- focused Store/migration/atomicity tests in `tests/test_db.py`
- focused polling/scheduling identity tests in `tests/test_service.py`
- focused strict API/reachability tests in `tests/test_engine_api.py`
- focused concurrency probes for rule-write/analysis serialization, stale
  mailbox-session CAS rejection, and scoped verification/commit lock coverage —
  `3 passed`
- `uv run pytest -o addopts='' --tb=short` — `1254 passed, 16 skipped in 66.59s`
- `git diff --check` — clean
- cold diff reconstruction against this Problem-derived contract before push

Observed baseline exception: `uv run ruff format --check .` reports 27
untouched files that would be reformatted. None is in this diff; the changed-path
format gate reports `25 files already formatted`.

## Estimated diff size

| Surface | LOC |
|---|---:|
| Tracked runtime, scripts, and tests | 5,084 |
| Plan | 301 |
| **Total** | **5,385** |

The overage is justified by the indivisible schema-20 vertical boundary stated
above; dispatch and every UI/product surface remain excluded.
