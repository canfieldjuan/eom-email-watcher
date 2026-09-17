# Contract Watch automation contract

Status: **PROPOSED — behavioral contract only; no implementation is authorized by this commit.**

Baseline inspected on 2026-09-17:

- Email Watcher `origin/main`: `b34089f11ca6ea6b422d160a94610472a62137ed`
- Document Summarizer `origin/main`: `e42d5bf0a2b06eb2efb039511f5d93bc37a494ee`
- Connect contracts `origin/main`: `3e5228361ae5e0cc45c0273d1d05bd9bab792894`

The code at those revisions is the authority for this contract. Product briefs,
fixtures, plans, and older contracts are supporting context only.

## Contract

### Root cause

What is wrong:

- Email Watcher can store strict mail rules, match analyzed messages, and create
  durable `connect.invoke` fires, but every fire remains `pending_dispatch`.
  Nothing reads its stable `dispatch_request_id` and creates or joins the
  existing Connect job queue.
- Document Summarizer implements a Contract summary profile, but its Connect v2
  manifest declares no parameters, v2 validation rejects every supplied
  parameter, and Connect ingestion always persists `SummaryProfile::General`.
- The canonical `document.translate` examples are protocol fixtures. No
  production provider implements that capability at the inspected revisions.

Where the wrong behavior originates:

- Email Watcher schema 20 permits only the fire state `pending_dispatch` and
  attempt number 1. Rule evaluation writes the fire and stable request identity
  atomically with message analysis, then stops.
- Document Summarizer constructs `document.summarize` with an empty parameter
  declaration, rejects nonempty `JobRequest.parameters`, converts the request to
  the v1 internal shape, and hard-codes General during ingestion.

Why the current behavior fails:

- A rule such as "PDF from this sender -> summarize as a contract" records an
  intent but never invokes a provider.
- A manual or future automated request with `{"mode":"contract"}` is rejected
  before the existing Contract pipeline can run.

Evidence:

- `src/eom_email_watcher/automation/rules.py` accepts bounded primitive action
  parameters and requires an exact provider and capability identity.
- `src/eom_email_watcher/db.py` creates `automation_fires` and
  `automation_fire_attempts`, while no production caller consumes those rows.
- Document Summarizer `src-tauri/src/connect/v2.rs` declares
  `parameters: vec![]` and returns `PARAMETERS_INVALID` for a nonempty map.
- Document Summarizer `src-tauri/src/connect/store.rs` persists every Connect run
  with `SummaryProfile::General` even though
  `src-tauri/src/pipeline/contracts.rs` also defines `Contract`.

### Required change surface

#### Observable Contract Watch behavior

Given all of the following:

1. Email Watcher has active `connect.capability_exchange` and
   `connect.automations` entitlement features;
2. a retained allowlisted mail message matches an enabled rule whose exact
   sender or sender domain and attachment media type select a PDF;
3. the rule action pins the live Document Summarizer provider and
   `document.summarize` version `1.0`, supplies `{"mode":"contract"}`, and has
   `confirm_each: false`; and
4. the selected provider advertises that exact parameter and declares no
   external or confirmation-required effect;

Email Watcher must use the fire's stable dispatch identity to admit that source
attachment to its existing Connect queue exactly once. Document Summarizer must
run the existing Contract profile and return the existing cited summary
artifact. Email Watcher's existing Connect result surface must show the real
waiting, completed, or failed job state for that message and attachment.

The rule contains the chosen sender. Neither app hard-codes a landlord,
insurer, lawyer, domain, mailbox, or provider instance.

#### Document Summarizer provider contract

- Keep Connect v1 frozen and General-profile behavior unchanged.
- In Connect v2, add one optional string parameter named `mode` to
  `document.summarize` version `1.0`.
- Accept exactly `general`, `story`, and `contract`. Omission means `general`.
  Reject wrong types, unknown values, extra parameters, and case variants with
  the existing non-retryable parameter error boundary.
- Derive one `SummaryProfile` from the validated request and pass that value to
  the single ingestion persistence owner. Do not validate one value and use the
  raw parameter later.
- Keep the capability identifier, capability version, accepted media, effects,
  output media type, output shape, citation behavior, model selection, and
  queue capacity unchanged.
- Preserve request parameters in the existing canonical request hash so replay
  with another mode is a conflicting identity rather than an accidental reuse.

Expected provider boundary probes:

- omitted, `general`, `story`, and `contract` are accepted and persist the
  corresponding profile;
- `Contract`, an empty string, an unknown string, integer `0`, boolean `false`,
  null, and an undeclared second parameter are rejected;
- a v1 request still persists General;
- the pipeline consumes the persisted Contract profile and produces the same
  artifact contract as an interactive Contract run.

#### Email Watcher fire-to-queue contract

- Add one bounded dispatcher that materializes existing `connect.invoke` fires
  into the existing `connect_attachment_jobs` and `connect_job_dispatch` queue.
  Do not add a second provider queue and do not synchronously wait for provider
  completion.
- Refactor the current interactive admission path only as needed so interactive
  and automation calls share one owner for discovery, parameter validation,
  source locking, byte fetch, size and digest checks, capability preparation,
  invocation fingerprinting, queue-cap admission, and authorization metadata.
- The immutable attempt's `dispatch_request_id` is the job identity when new
  work is created. Re-entry after a crash must replay the same attempt or its
  stored job binding. Concurrent click and automation admission for the same
  complete invocation fingerprint may join one active or completed job, but
  must never create duplicate provider work.
- Provider app version, instance, capability version, canonical parameters,
  input identity, effect flags, and required confirmation form the authority
  checked at admission. The dispatcher must not silently select another
  provider or capability when the rule's pinned identity is unavailable.
- Fetch and verify source bytes under the existing per-message source lock
  immediately before confirmation preparation or queue admission. Do not store
  attachment bytes in the automation tables. Definitive source loss or drift
  settles as `source_unavailable`; transient mailbox or lock failure remains
  retryable.
- Recheck both entitlement features for every new automation admission and
  every proven-new provider POST. GET-only reconciliation of work the provider
  may own remains permitted. Time while entitlement is inactive must be a
  distinct paused state and must not consume the active pending-stall interval.
- Snapshot the admitted provider effect flags and require an authorization
  receipt bound to the full invocation fingerprint. An automation receipt is
  valid only for its exact fire and prepared identity. An interactive click
  creates interactive authority only through the real click path. Joining work
  does not transfer one fire's confirmation or one origin's authority to
  another.
- When `confirm_each` is true or the live manifest declares an external or
  confirmation-required effect, persist a content-free prepared identity and
  hash, then settle at `awaiting_confirmation` before queue admission. A later
  confirmation is usable only if a fresh source and manifest preparation
  matches that receipt exactly. Drift or a stale notice halts for review.
- Extend fire settlement with versioned states sufficient to distinguish
  `pending_dispatch`, entitlement pause, `awaiting_confirmation`, submitted
  work, completed work, declined work, manual review, and unavailable source.
  State changes use compare-and-set transactions. Rule definitions and prior
  attempt identities remain immutable.
- A Connect admission deadline that proves no provider acceptance may open one
  fresh attempt atomically. A second such deadline settles for manual review.
  No other failure creates a new attempt. Provider-owned or reconciling jobs are
  never duplicated.
- Dispatch at most the existing queue batch per pump and honor one bounded
  phase deadline. Lock contention, provider absence, queue capacity, transient
  fetch failure, or entitlement pause leaves durable resumable work; repeated
  passes must not reset the active pending timer. An active pending interval
  that exceeds its contract deadline settles for review instead of retrying
  forever.
- Deleting a retained message deletes unsubmitted and confirmation-waiting fire
  intent, receipts, and notices in the same cleanup unit. Once a Connect job may
  exist, the existing source-retention and reconciliation rules govern cleanup.
  No notification or automation audit row stores source bytes or summary text.
- The desktop queue scheduler and the normal watcher-check path must both reach
  the same bounded dispatch and settlement owner so desktop and headless checks
  do not implement different automation semantics.

#### Acceptance rule definition

The deterministic acceptance rule uses the existing strict schema:

```json
{
  "name": "Contract watch",
  "scope": {},
  "trigger": {"source_kind": "mail.message"},
  "conditions": [
    {"field": "sender", "op": "domain_equals", "value": "example.test"},
    {"field": "attachment.media_type", "op": "equals", "value": "application/pdf"}
  ],
  "action": {
    "kind": "connect.invoke",
    "capability": {"id": "document.summarize", "version": "1.0"},
    "provider": {
      "app_id": "document-summarizer",
      "version": "0.1.0",
      "instance_id": "11111111-1111-4111-8111-111111111111"
    },
    "parameters": {"mode": "contract"}
  },
  "confirm_each": false
}
```

The fixture identities above are test data only. Production rule creation must
store the provider identity selected from live discovery.

#### Tests and evidence

Email Watcher must add focused tests for:

- rule creation -> real `watcher.check` -> one durable fire -> one queue job ->
  one completed result linked to the source message and part;
- opposite controls: nonmatching sender, non-PDF attachment, missing Automate
  feature, and unsupported parameter create no provider submission;
- provider unavailable, queue full, transient source failure, restart after
  each state boundary, repeated pump, and concurrent interactive invocation;
- exact-once job binding, active/completed join order, two-attempt deadline
  boundary, source deletion, entitlement pause/resume, confirmation hash drift,
  provider effect drift, and stale confirmation cleanup;
- bounded batch and deadline behavior without synchronous terminal waits.

Document Summarizer must add focused tests for:

- manifest declaration and the full mode boundary matrix above;
- validated mode reaching persisted `PipelineRun` state;
- Connect v1 compatibility and Connect v2 replay/conflict behavior;
- a deterministic Contract Connect job producing the existing cited artifact.

Cross-repository acceptance must exercise:

1. a deterministic PDF and model fixture through the production Email Watcher
   request dispatcher and real Document Summarizer Connect server;
2. one real local model endpoint with a contract PDF, asserting the selected
   profile, successful terminal state, output integrity, and citation/source
   linkage; and
3. Linux and Windows installed evidence independently before any joint release
   readiness claim.

### Explicit non-scope

- No `certificate.extract`, `receipt.extract`, `statement.extract`,
  `order.extract`, or production `document.translate` capability.
- No Invoice Processor code, schema, ledger, or provider behavior change.
- No automation builder UI, new user-facing copy, sender allowlist expansion,
  mailbox permission expansion, notification channel, or calendar behavior.
- No provider-side queue, broker, remote Connect transport, launch-on-demand,
  stored attachment bytes, model download, or model-selection change.
- No Connect v1 schema change, Connect v2 shared-schema change, capability
  version bump, output artifact change, broad refactor, dependency bump, rename,
  formatting sweep, or generated-file churn.
- No claim that a merged implementation is installed, release-ready, or
  publicly released without separate runtime and platform evidence.

### Assumptions and blockers

Assumptions:

- Contract Watch is the first slice because it reuses an implemented summary
  profile and exercises the generic automation path with the smallest new
  provider surface.
- "Chosen sender" means a strict user-stored sender or domain condition, not a
  new hard-coded sender class.
- The first slice reuses Email Watcher's existing Connect result presentation.
  A rule-management interface is a separate product and copy contract.
- Contract obligations and dates remain part of the existing Contract summary;
  this slice does not create a separate obligations ledger or calendar event.

Blockers:

- Operator acceptance of this behavioral contract is required before code or
  schema implementation begins.
- Product decisions for a rule-management UI and any obligations ledger remain
  open and do not block the engine/provider acceptance path above.

### Verification plan

Fail-first probes, named before implementation:

- Email Watcher: a matching rule followed by `connect.queue.pump` leaves its
  fire unconsumed and produces no automation-owned Connect job. Expected failure
  class: missing fire-to-queue dispatch.
- Document Summarizer: a v2 request with `{"mode":"contract"}` returns
  `PARAMETERS_INVALID`, and an admitted parameter-free request persists General.
  Expected failure class: declared parameter absent and profile hard-coded.

After implementation:

- Run the new targeted Email Watcher dispatcher, Store, rule, source-retention,
  queue, entitlement, confirmation, concurrency, and engine API tests.
- Run the new targeted Document Summarizer v2 contract, provider, Store, and
  pipeline tests plus applicable Rust formatting and lint checks.
- Run the deterministic cross-repository acceptance, then the real local-model
  endpoint acceptance.
- Run required repository-specific gates without duplicating broad suites that
  exact-head CI already owns.
- Inspect the final diffs cold against this contract and record a `DONE` or
  `NOT DONE` gap audit for each repository.

## Landing order

1. Commit and accept this behavioral contract by itself.
2. Implement and verify Email Watcher's bounded fire-to-queue dispatcher against
   an existing parameterized reference provider.
3. Implement and verify Document Summarizer's `mode` parameter and profile
   propagation without changing Connect v1.
4. Run the deterministic and real-model Contract Watch acceptance paths.
5. Add rule-management UI only under a separately accepted product/copy
   contract.
