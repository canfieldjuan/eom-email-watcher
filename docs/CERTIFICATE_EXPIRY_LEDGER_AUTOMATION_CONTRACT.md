# Certificate Expiry Ledger automation contract

Status: **CONTRACT, revision 2, accepted 2026-09-20.**

This contract defines one Email Watcher vertical slice: a stored generic rule
invokes a discovered Connect v2 `certificate.extract` capability for a retained
mail attachment, validates the provider's app-specific JSON result, and stores
certificate and policy rows in Email Watcher's local SQLite database. A thin
desktop view lets the operator inspect policy expirations and review uncertain
dates. Existing generic rule evaluation, Connect v2 discovery/dispatch, queue,
authorization, and reconciliation remain the owners of their current behavior.

## Observable behavior

Given an enabled rule whose `connect.invoke` action pins a live provider
instance and `certificate.extract` capability version, and a retained message
attachment matching that rule, Email Watcher must:

1. Admit and run the attachment through the existing generic rule and Connect
   v2 dispatch path, preserving its source identity, immutable attempt identity,
   entitlement, confirmation, queue, and provider reconciliation rules.
2. On provider success, strictly validate the returned certificate JSON against
   the closed application schema below. Do not store a partial ledger result if
   any field, policy row, or cross-field constraint is invalid.
3. In one local database transaction, insert or join exactly one certificate
   record and its complete set of policy rows for the source message, MIME part,
   and Connect job. Reconciliation or replay of the same job must not duplicate
   either the certificate or its policies.
4. Expose the resulting persisted rows through a minimal engine list operation
   and a desktop Expiry Ledger table. The table has one row per policy and
   repeats the parent certificate identity on each row so flattening never
   loses which certificate the policy came from.

The rule stores the exact selected provider identity. There is no hard-coded
sender, insurer, agency, provider, or capability instance. A missing or changed
provider identity follows the generic dispatcher's existing unavailable or
review settlement behavior; it is not silently replaced by another provider.

## Existing dispatcher boundary

- Reuse the current strict rule definition, `connect.invoke` fire, bounded
  dispatcher, Connect v2 manifest discovery, request admission, job polling,
  source locking and digest checks, and immutable dispatch identity. Do not add
  a certificate-specific queue, retry loop, provider-selection path, or direct
  HTTP client.
- `certificate.extract` is an app-specific result contract. The generic
  dispatcher remains responsible only for Connect protocol validity and job
  settlement. It must hand the completed result to the certificate ledger
  validator/persister exactly once per settled job identity.
- Provider-declared effects, confirmation requirements, and entitlement checks
  remain enforced by the generic dispatcher. Certificate ledger persistence is
  a local data write and does not authorize external policy changes, customer
  contact, calendar writes, or insurer actions.
- A retry, restart, repeated poll, or duplicate completion notification for the
  same dispatch job may re-enter result handling. A database uniqueness fence
  and transaction must make that re-entry idempotent.

## Closed `certificate.extract` result JSON

The consumer validates the exact provider-owned `certificate.extract` v1.0 record. It MUST NOT
invent a second date parser, policy classifier, or alternative field aliases. The Connect output
contains exactly one UTF-8 JSON object with no duplicate keys, unknown members, trailing data, or
invalid UTF-8. It is bounded by the existing Connect output limit.

The top-level object is exactly:

```json
{
  "record_version": "1.0",
  "source": "<SourceInfo>",
  "insured": "<TextValue or null>",
  "certificate_holder": "<TextValue or null>",
  "producer": "<TextValue or null>",
  "policies": ["<Policy>"],
  "withheld": ["<Withheld>"],
  "review": {"required": false, "reasons": []},
  "extracted_at": "<UTC RFC 3339 timestamp>"
}
```

`SourceInfo`, `TextValue`, `DateValue`, `Provenance`, and `Withheld` have the strict shapes from
the accepted provider contract. A policy has exactly `coverage`, `insurer`, `policy_number`,
`effective_date`, `expiration_date`, and `review_reasons`. The first three values are nullable
`TextValue`; the dates are nullable `DateValue`; `policies` contains zero to 100 rows. At least
one extracted field is non-null in every policy row. An insurer is policy-scoped.

For every `TextValue`, the consumer validates non-empty text, the boolean `whole_span`, finite
ordered provenance coordinates, one-based page, a non-empty exact source string, and an ordered
token range. For every `DateValue`, it validates the provider-owned invariant: an unambiguous
value has one valid ISO date matching `iso`; an ambiguous value has `iso: null` and at least two
unique valid ISO candidates; provenance is present in both cases. The consumer validates these
relationships but never reparses provenance text into a competing date.

Policy review reasons are the closed provider set `EFFECTIVE_DATE_MISSING`,
`EFFECTIVE_DATE_AMBIGUOUS`, `EXPIRATION_DATE_MISSING`, `EXPIRATION_DATE_AMBIGUOUS`,
`DATE_RANGE_INVALID`, and `ASSOCIATION_UNCLEAR`. Top-level review reasons are the closed set
`INSURED_MISSING`, `NO_POLICY_ROWS`, `POLICY_DATE_REVIEW`,
`POLICY_ASSOCIATION_UNCLEAR`, and `CONFLICTING_VALUES`. Reasons are unique and in provider-defined
order. `review.required` is true exactly when top-level or policy reasons are non-empty.
`POLICY_DATE_REVIEW` is present exactly when a policy carries a date review reason. When both
policy dates are unambiguous, `DATE_RANGE_INVALID` is present exactly when expiration precedes
effective date.

Malformed JSON, extra fields, wrong types, invalid relationships, more than 100 policies,
duplicate policy rows with the same canonical content, and review flags that contradict their
reason lists fail closed as local application error `CERTIFICATE_RESULT_INVALID`. The provider
job remains recorded as provider-completed evidence, while the local automation fire settles
failed with that application reason and no certificate or policy row is inserted. A structurally
valid empty/partial record is persisted as a review result exactly as the provider contract
specifies.

## Identity, storage, and idempotency

Persist one parent certificate row and zero to 100 child policy rows. Neither table stores source
attachment bytes or full message content. The parent retains the canonical validated provider
record and its SHA-256 so provenance and replay evidence survive source cleanup; that bounded
record is not a second PDF or message-body store.

The certificate identity is its stable database id plus the immutable source tuple `(provider,
account_id, mailbox_identity_key, message_id, part_id, connect_job_id)`. The tuple is unique. The
parent stores that tuple, result digest and canonical JSON, source display reference, nullable
party text and provenance, top-level review reasons, and created/updated timestamps. Each policy
row is identified by `(certificate_id, ordinal)` and stores the exact coverage, insurer, policy
number, date values and provenance, policy review reasons, and timestamps. Source order is the
policy order; equal field values in different rows are not silently collapsed.

The completed-result projection runs inside the same `BEGIN IMMEDIATE` transaction that settles
the local Connect job and its automation fire. It first verifies the immutable job/fire/source
binding and validates the entire certificate record, then inserts or joins the parent and
replaces no prior evidence. A replay for the same source/job tuple joins only when the canonical
result digest matches. A different digest for that tuple is
`CERTIFICATE_RESULT_CONFLICT`, settles the local fire failed, and never overwrites the first
record. Any schema, relationship, or insert failure rolls back the full parent/child projection
and terminal fire update.

Repeated messages or attachments are not merged because names, insurer, coverage, or policy
number happen to match. A different source message, MIME part, or Connect job remains separately
attributable. Cross-source duplicate detection, merge, and replacement are outside this slice.

## Date status and review behavior

Expiry status is a query-time projection using the caller-supplied local calendar date, which the
engine validates as `YYYY-MM-DD`; the desktop supplies today from the user's local calendar. For
a policy whose expiration `DateValue.iso` is present:

- `expired` when expiration is earlier than today;
- `expires_today` when expiration equals today;
- `upcoming` when expiration is later than today.

A missing or ambiguous expiration has expiry status `review`. Effective-date uncertainty and any
other policy review reason set `review_state: needs_review` but do not erase a certain expiration
classification. This keeps a known expiry visible while honestly marking the row for review. A
row with no review reasons has `review_state: extracted`; neither value claims human review.

A certificate with zero policy rows is still visible. The list operation emits one
certificate-level placeholder row with null policy identity and fields, expiry status `review`,
review state `needs_review`, and reason `NO_POLICY_ROWS`.

Human correction is a separate contract. This slice adds no edit, approve, waive, renew,
reminder, mark-covered, or delete action.

## Minimal engine and desktop surface

Add one read-only engine operation, `certificate.expiry_ledger.list`, with payload
`{"today": "YYYY-MM-DD", "limit": N}`. `today` is required and `limit` defaults to 100 with an
accepted range of 1 through 500; booleans, zero, negatives, and 501 are rejected. The operation
returns a deterministic bounded list ordered by certain expiration ISO ascending, unknown dates
last, then certificate id and policy ordinal.

Each flattened policy row contains certificate id, nullable certificate-holder, insured, and
producer text; nullable policy id/ordinal, exact coverage label, insurer and policy number;
effective and expiration ISO/ambiguity/candidates; expiry status, review state/reasons; source
message id, MIME part id, Connect job id, and whether the retained source link is available. It
does not return source body, attachment bytes, credentials, prompts, unrelated message content,
or raw model responses. An empty ledger returns an empty list; malformed persisted state fails
closed rather than manufacturing defaults.

The desktop adds a single thin `Expiry Ledger` view backed only by this operation. It renders one
row per policy plus the zero-policy placeholder. Columns show holder, insured, producer,
coverage, insurer, policy number, effective date, expiration date, expiry status, review state,
and source availability. All provider strings render through text nodes. The view visibly
distinguishes expired, expires today, upcoming, and review, and displays missing/ambiguous dates
as `Needs review`. It adds no edit flow, reminder, bulk action, filtering promise, sort controls,
or separate detail screen.

## Restart, reconciliation, and concurrency

- On restart, the generic dispatcher reconciles submitted or provider-owned
  Connect jobs using its existing job id and source identity. It must not create
  a second job to recover a result.
- After the provider reports success, result validation and local ledger commit
  are replay-safe by the unique source/job key and validated-result digest. A
  crash before commit causes the same result to be fetched/reconciled and
  committed once. A crash after commit returns the existing rows on replay.
- Concurrent dispatch pumps cannot submit duplicate provider work; this remains
  guaranteed by the existing generic dispatcher. Concurrent result handlers
  for one job serialize at the database transaction/unique-key fence. One
  transaction inserts the full parent/child set; all others join that result
  or fail on digest mismatch.
- Provider completion remains recorded as immutable provider evidence. The
  local automation fire is not settled `completed` until the provider output
  has passed application validation and the ledger transaction has committed.
  Invalid provider output settles the local fire failed with its application
  reason, not as an endless retry. Database busy/temporary I/O failure remains
  retryable through reconciliation without resubmitting the provider job.
- If the source is deleted before provider submission, generic source-retention
  cleanup removes unsubmitted intent under existing rules. Once a provider job
  may exist, existing reconciliation and source tombstone rules apply. The
  ledger retains content-free source identity and extracted fields after source
  message deletion so the operator can still identify the record; the source
  link becomes unavailable. Deleting the source must never cascade-delete an
  already committed certificate or policy record.
- No ledger row contains attachment bytes, extracted full-document text, full
  message content, or secrets. The parent retains only the bounded canonical
  provider result defined above so its span provenance and digest remain
  auditable after source cleanup. Existing retention controls for generic
  automation audit and source records continue to apply. Ledger deletion,
  certificate correction, and retention policy changes are outside scope.

## Acceptance evidence

The vertical proof uses a deterministic retained message and PDF, the real
Email Watcher engine API, the existing generic dispatcher, the real local
Connect v2 provider advertising `certificate.extract`, and a valid fixture
result. It must demonstrate:

1. rule creation and matching create one immutable fire/attempt and one Connect
   job;
2. a completed provider result validates and commits one certificate with all
   child policies in one transaction;
3. repeated polling, pump, process restart, and concurrent result settlement
   leave one parent and the expected complete child set;
4. `certificate.expiry_ledger.list` returns flattened policy rows preserving
   certificate identity and stable ordering;
5. the desktop renders expired, expires-today, upcoming, and needs-review rows
   from real engine data;
6. malformed JSON, duplicate JSON keys, extra fields, wrong types, invalid ISO
   dates or date relationships, duplicate canonical policy rows, more than 100
   policies, and a conflicting replay digest create no partial or silently
   replaced ledger data; a provider-valid reversed range is retained with
   `DATE_RANGE_INVALID` review evidence;
7. missing and ambiguous effective/expiration dates are visible for review;
   an uncertain expiration is never classified as expired or upcoming, while
   effective-date uncertainty does not hide a certain expiration status;
8. provider unavailability, entitlement pause, source loss, queue admission
   failure, and restart at each dispatcher boundary retain the generic
   dispatcher's current bounded and resumable semantics.

This proof establishes local behavior only. It does not establish that a
provider's model output is factually correct, that an insurer recognizes a
policy, that coverage is active, or that any reminder or renewal occurred.

## Explicit non-scope

- No changes to generic rule definition, evaluator, Connect v2 dispatcher,
  authorization, queue, retry limits, or provider-selection policy except a
  defect that blocks this contract and is separately documented.
- No changes to Connect v1, the shared Connect schema, provider queue behavior,
  Invoice Processor business ledger, or another provider capability.
- No automatic reminders, email/calendar writes, renewal workflow, broker
  contact, coverage advice, claims, compliance assessment, premium handling,
  certificate generation, policy verification, or cross-source record merge.
- No rule builder, new sender allowlist, new mailbox permissions, general
  automation UI, ledger edit/review action, or customer-facing product claims.
- No source-byte or full-document-text duplication into Email Watcher's ledger.
- No release, installation, or cross-platform readiness claim without separate
  installed runtime evidence for each claimed platform.

## Accepted dependencies and decisions

- The accepted provider contract is `CERTIFICATE-EXTRACT.md`, revision 3. The
  two contracts share the exact app-specific result schema and 100-policy cap.
- Only policy expiration visibility is in this slice. Certificate holder,
  insured, and producer provide visible certificate identity; no universal
  certificate id is inferred from model output.
- An uncertain date is preserved as uncertainty and routed to operator review;
  neither provider nor consumer selects a guessed date.
- Date corrections, reminders, renewal actions, and other behavior outside the
  read-only ledger require a later product contract.

## Revision log

- 2026-09-20: Revision 1 proposed.
- 2026-09-20: Revision 2 accepted after synchronization with provider contract
  revision 3, including provider-owned date parsing, transaction-coupled local
  settlement, zero-policy visibility, and the 100-policy cap.
