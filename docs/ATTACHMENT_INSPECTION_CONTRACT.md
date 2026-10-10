# Attachment inspection contract

Status: **PROPOSED**, inspection pipeline only (#231), separate from the D5 claims amendment (#234). Contracts only. Before either is accepted, this contract and the document classifier contract in Invoice Processor must state the same result format (I-result). Nothing here is implemented.

## Why

Email Watcher decides what an email is about from its sender, subject, body, and attachment file names (`model.py`, `_email_prompt`). It never reads a document. An automation that relies on the email's category can therefore miss an invoice sent with a blank or generic email, and Invoice Processor's `invoice.extract` reads every input as an invoice. The operator's direction (2026-10-10): after explicit account-scoped selection, inspect supported attachments from the watched senders and accounts, whatever the email says; classify it from the document's own evidence in the document-processing layer; send only invoices to extraction; and keep unreadable or uncertain documents visible instead of dropping them.

This contract owns the order of that work, its states, its queue, and what it stores. The document classifier contract owns how a document is classified. `docs/THREAD_VIEW_CONTRACT.md` (D-claims) owns what an inspection result means for vendor claims. `docs/CONTRACTS.md` owns the Connect queue, which this contract uses through an engine origin it adds there (I-gate).

## Observable behavior

- Every attachment that inspection covers shows its inspection state and reason on its attachment row and in its thread.
- An email's wording, subject, file names, and analysis category never stop, start, or decide inspection.
- A blank email whose PDF is positively classified as an invoice, and passes the remaining checks of D-claims, gives an invoice claim. A file name proves nothing.
- A rule's document-class condition is decided only after the attachment's inspection settles.
- Inspection never goes ahead of a waiting rule fire, and adds at most one job ahead of a fire that arrives later. It makes no promise about time.

## Frozen scope

This proposal owns explicit selection, consumer inspection/extraction lifecycle, bounded scheduling, cache ownership, backfill, and document-class rule integration. D5 claims semantics are reviewed in [PR #234](https://github.com/canfieldjuan/eom-email-watcher/pull/234). The provider classifier/schema belongs to [Invoice Processor issue #139](https://github.com/canfieldjuan/invoice-processor/issues/139). Unknown Gmail attachment sizes and rare legacy mixed-caller job upgrades are deferred to [#232](https://github.com/canfieldjuan/eom-email-watcher/issues/232) and [#233](https://github.com/canfieldjuan/eom-email-watcher/issues/233); no new admission exception for them is defined here.

## Definitions

### I-selection: explicit user selection

- **New mail.** "Check attachments from watched senders" is a per-account setting, off by default. Enabling it records the user's selection of supported, non-embedded attachments from that account's watched senders/accounts received at or after the enable time, for `document.classify` and invoice-only `invoice.extract` by compatible local providers. The selection is bound to the verified mailbox identity and has its own revision; a different mailbox receives no inherited permission. The disclosure names the attachment bytes and bounded metadata that cross the boundary, the receiving capabilities, and both required paid features. Watching an account or sender, or owning a licence, never records this selection.
- **Historical work.** Retained mail before that enable time is selected separately, with a frozen time range displayed to the user. Selection never overrides D-scope's fetch cutoff. Migration records existing descriptors locally but submits none solely because they existed before rollout.
- **Revocation.** Disabling the setting revokes new handoffs from its revision. Unsent attempts remain `deferred` (`selection_inactive`). I-gate controls new preparations, enqueues and POSTs; a persisted queue entry cannot bypass revocation. A re-enabled selection authorizes only its recorded scope. Potentially provider-owned jobs keep the shared queue's reconciliation and tombstone rules, and are never replayed merely because a switch changes. Valid stored evidence is not deleted by disabling selection. Stored claim eligibility follows D-claims, Evidence permission. A scope never selected receives no new checking merely from a cache hit or a pre-existing extraction record. Admission of evidence from a cache or settled job records the attachment's selection provenance, which D-claims consumes.

### I-scope: which attachments are inspected

- **A candidate** is a known-size attachment part whose descriptor today's message processing persists, for a message admitted from a watched sender or account, except an embedded part. Descriptors are persisted only where today's processing persists them, so inspection covers exactly those parts.
  - **A part is embedded** only when the message's HTML body references its Content-ID (`cid:`). `Content-Disposition` and Graph's `isInline` never decide it, because some mail clients mark real PDF attachments inline.
  - A part stored before this contract is a candidate, so a missing Content-ID can only add a candidate.
- **A candidate is inspectable** when all of these hold; otherwise it is `uninspectable` with the first reason that applies:
  - its message was received at or after the cutoff (D-scope): reason `outside_retention`;
  - its message's mailbox identity key equals the current account's verified key: reason `mailbox_identity_unverified`;
  - the selected classifier (I-version) accepts its declared media type and size (`accepts_artifact`): reason `unsupported_media_type` or `too_large`;
  - its bytes fit today's fetch limits (IMAP at most 50 MiB): reason `too_large_to_fetch`;
  - it is among the first `MAX_INSPECTION_CANDIDATES_PER_MESSAGE` (64) candidates of its message in `(position, part_id)` order: reason `too_many_attachments`.
- **Embedded status.** Today's message-content fetch records each part's Content-ID and whether the HTML body references it, before descriptors are persisted and before `mark_analyzed`. A missing recorded value does not exclude the part.
- The declared media type is the mailbox's; file names and email-category output never decide support. Generic media-type compatibility is outside this revision.
- **Existing attachments.** The migration records retained candidate descriptors `deferred` (`selection_inactive`), so rollout preserves a visible state without authorizing a fetch. An explicit historical selection moves its matching candidates to `deferred` (`backfill`); the pump then applies I-scope and I-gate. New-mail selection does not silently authorize old parts.
- **Descriptor replacement.** Analysis retries replace a message's descriptors. A part whose `part_id`, declared media type, and byte size are unchanged keeps its inspection. Any other part's inspection restarts with a new attempt, and a part that is gone loses its inspection, class checks, and extraction state in that transaction. Settlement is bound to the current attempt (I-order), so a job of a replaced attempt settles nothing.

### I-state: an attachment's inspection

Every candidate has exactly one inspection state, kept with its reason, so the thread always shows why processing stopped:

| State | Meaning | Reason |
|---|---|---|
| `uninspectable` | I-scope rejected it | `outside_retention`, `mailbox_identity_unverified`, `unsupported_media_type`, `too_large`, `too_large_to_fetch`, `too_many_attachments` |
| `deferred` | waiting in the queue, no job | `selection_inactive`, `queued`, `backfill`, `sweep`, `awaiting_identical`, `awaiting_extraction`, `yielding_to_rules`, `queue_full`, `entitlement_inactive`, `classifier_unavailable`, `ambiguous_provider`, `classifier_version_unadmitted`, `extractor_version_unadmitted` |
| `pending` | an attempt's Connect job exists | the attempt id and job id |
| `failed` | no valid result was obtained | the code, `retryable` or `permanent`, attempts, and the next retry time |
| `settled` | a valid result under a generation | the result (I-result) and its generation |

- A provider that read the document but could not classify it returns a valid result with class `unknown`: that is `settled`, not `failed`.
- **Connect outcomes map to states one way:**

  | Connect outcome (`docs/CONTRACTS.md`) | Inspection state |
  |---|---|
  | enqueue refused, `connect_queue_full` | `deferred` (`queue_full`) |
  | `waiting`, `dispatching`, `reconciling`, `provider_owned`, including provider busy or absent before a POST and ambiguity after one | stays `pending` |
  | `connect_queue_deadline_exceeded` | `failed`, retryable (`queue_deadline`) |
  | `CONNECT_ENTITLEMENT_REQUIRED` | `deferred` (`entitlement_inactive`) |
  | `connect_source_unavailable`, or a source that is missing, changed, or past the cutoff | `failed`, permanent (`source_unavailable`) |
  | a transient fetch failure while preparing (timeout, temporary mailbox outage, refreshable authorization) | `failed`, retryable (`source_fetch`) |
  | terminal `failed` with a code the provider marks retryable and effect-free (`PROVIDER_RESOURCE_BUSY`) | `failed`, retryable |
  | any other terminal `failed` | `failed`, permanent, with the code |
  | `completed` with an invalid result (I-result) | `failed`, permanent (`classification_result_invalid`); the Connect job stays `completed` |
  | `completed` with a valid result | `settled` |

- A retryable failure starts a new attempt on the analysis retry schedule (5, 15, 60, 360, and 1,440 minutes, then every 1,440 minutes while the message is retained). It is never skipped and never turns into `unknown` by itself.
- Readers read a candidate's current result (I-version), which a newer attempt in any state does not remove; a candidate without one is unsettled. D-claims owns what unsettled means for a claim.

### I-version: the selected classifier and its generations

- **The selected classifier** is a durable record: the `document.classify` capability's identity and version, and the provider app and instance that offer it. Selection follows `docs/CONNECT_V1.md`'s rule (no implicit winner):
  - with nothing recorded, exactly one compatible provider is recorded; more than one leaves candidates `deferred` (`ambiguous_provider`); none leaves them `deferred` (`classifier_unavailable`);
  - while the recorded instance is discovered, a different admitted capability version on it replaces the record;
  - while the recorded instance is absent and exactly one other instance offers a compatible capability, that one is recorded; with more than one, candidates wait `deferred` (`ambiguous_provider`), and with none, `deferred` (`classifier_unavailable`);
  - only an admitted version is selected: a `document.classify` capability of major version 1, whose results are I-result `record_version` `"1.0"`. Any other version is never selected, and candidates wait `deferred` (`classifier_version_unadmitted`);
  - a capability that declares effects is never selected.
- **Absence only defers.** A provider that is not running, or a catalog left empty by an inactive entitlement, changes no record, and only new attempts wait. Discovery checks the entitlement first, so a lapse reads `entitlement_inactive`, not absence.
- **Generations.** Every change of the recorded identity or version, not merely of the instance, starts a new selection generation, numbered in order. Every attempt and result records its generation. The same identity and version discovered again after an absence is not a change.
- **A candidate's current result** is its settled result from the newest generation that has one. A newer generation's result replaces it once it settles; until then the previous result stays current, shown with its version, so an upgrade never removes a claim while re-inspection runs. A result from an older generation, however late it settles, never displaces a newer one. A message past the cutoff, which is never fetched again, keeps its last settled result. A candidate with no current result is unsettled.
- **A new generation re-inspects retained work.** The check records one durable, global sweep with its own cursor. The sweep gives every retained candidate whose newest decision belongs to an older generation (a settled or permanently failed attempt, or an `uninspectable` verdict for media type or size) a new attempt for the new generation, `deferred` (`sweep`), after I-scope's checks. A candidate that fails a check keeps its current result and records why. An older attempt still `pending` is superseded: its Connect job follows `docs/CONTRACTS.md` (it may keep reconciling or stay as a tombstone), its late completion reconciles that job without settling the replaced candidate attempt or writing its cache. The new attempt never joins the older attempt, but admission still obeys I-budget's lane ownership; supersession does not release a nonterminal job.
- **The selected extractor** is recorded the same way for `invoice.extract`, without generations or a sweep, and only an admitted version is selected: `invoice.extract` 1.0, whose records are `record_version` 1.1. Any other version is never selected, extraction waits `deferred` (`extractor_version_unadmitted`), and adopting it needs a contract amendment that defines its record mapping.

### I-cache: one classification per document and version

- A result is cached under `(file sha256, declared media type, classifier id, classifier version)`: the hash of the exact bytes the admission owner prepared, and the type it submitted them as, since the type can select a different parser.
- **Atomic reservation.** After source preparation and hashing, result lookup and reserve-or-join for the full key happen in the same immediate transaction as Connect enqueue. A refusal creates no orphan reservation. No network or model work happens inside that transaction.
- **One job per key.** A candidate whose prepared hash matches a key that has a cached result settles from it, with no job. A candidate whose hash matches a key with an in-flight engine job waits `deferred` (`awaiting_identical`) and settles from that job's result. Only one engine job per key exists at a time.
- **First result wins.** The first valid result to settle for a key fills it; a later different result for the same key never overwrites it and is recorded as a conflict.
- A cache entry is written only from a result settled for a retained candidate's current attempt. It is deleted when no retained message's inspection holds its hash.

### I-order: the order of attachment work

Owned here, so no other component can reorder it:

1. **Descriptors are persisted** (today's step before analysis). In the same transaction, an unselected candidate is recorded `deferred` (`selection_inactive`); a selected candidate is `deferred` (`queued`), or `uninspectable` for a reason its descriptor and message already show. With no classifier selected, it is `deferred` with that reason. Inspection therefore never waits for analysis, and a message whose analysis fails permanently is still inspected.
2. **An attempt is recorded** before any fetch: a UUID that will be its Connect job id, persisted with the candidate, so a restart replays the same attempt.
3. **The admission owner prepares it.** The pump calls `_prepare_or_create_generic_connect_job` without creating a job (`create_job=False`): under the source lock it applies the retention, mailbox-identity, folder, and fetch rules, fetches the bytes, and computes their hash. The cache and coalescing (I-cache) are consulted with that hash.
4. **The admission owner creates the job** with engine origin (I-gate), unless the cache or an identical job settles the candidate. No category, priority, or action-required value is read.
5. **A result settles** in the transaction that settles its Connect job, once, fenced by `(attempt id, job id)`, after strict validation (I-result), and only for the candidate's current attempt.
6. **A part whose current result is `invoice`** is recorded `deferred` for extraction in that same transaction, unless I-records already has its record. Engine extraction runs on no other part (I-records).
7. **Class checks resolve** in that same transaction, and checks on parts that were already settled resolve in the pump's next class-check pass (I-rules).

### I-gate: engine origin and entitlements

- **Engine origin.** Inspection and extraction jobs are created with the engine origin that `docs/CONTRACTS.md` gains with this contract: an immutable origin written with the job row and bound to the message, part, attempt, capability identity and version, provider instance, and input hash. The shared queue owns its immutable identity, isolation from other origins, and checks before every POST. This paragraph does not add a separate queue implementation.
- **Authorization.** Before source preparation and enqueue, the inspection owner consumes I-selection's current scope and verified mailbox identity and D-ops' automation-class entitlements. Before every new POST, the shared queue repeats that decision (Job origin). No preparation or new handoff is authorized while the selection is inactive, out of scope, or either paid feature is inactive. The attempt remains deferred with its distinct reason; local descriptor/state recording grants no authority.
- **While either entitlement is inactive,** no attempt is prepared or created, waiting candidates stay `deferred` (`entitlement_inactive`), and a job the queue fails with `CONNECT_ENTITLEMENT_REQUIRED` returns its candidate to that state. Recording candidates (I-order step 1) is local and not gated.
- **Provider effects.** I-version admits only capabilities without declared effects. This does not waive the user selection required by I-selection.

### I-budget: bounded and fair work

- **Engine jobs yield and stay few.** An engine job is created only while no other job is waiting in its lane and no other engine job in that lane is nonterminal. Otherwise the candidate stays `deferred` (`yielding_to_rules`).
- **What that guarantees, and what it does not.** The queue stays first in, first out (`docs/CONTRACTS.md`). An engine job never goes ahead of a job that was already waiting, and a job that arrives later waits behind at most one engine job. Inspection makes no promise about time: an engine job that never settles holds its lane exactly as any Connect job may (reconciliation in `docs/CONTRACTS.md`), and no second engine job is admitted to that lane meanwhile.
- **Own phase.** Each pump runs inspection after the rule-fire dispatch phase, in its own phase. It stops starting candidates after the `INSPECTION_PHASE_SECONDS` (5) monotonic deadline and advances at most the pump's `limit` candidates; an already-started bounded source preparation follows its existing timeout. This is not a five-second guarantee for provider completion.
- **Fair rotation under eligible capacity.** A durable cursor rotates `queued`, `sweep`, and `backfill` one candidate at a time, starting with `queued` and skipping empty or not-yet-retryable groups. Within each group, oldest message first, then `(position, part_id)`. New mail therefore gets the first opportunity without starving historical work when admission opportunities continue. Provider absence, paused authority or permanent lane saturation promise no progress and never release ownership. Cache hits and identical-job joins consume candidate budget but require no new lane slot.
- Deferred work is never skipped and never times out into another state while its message is retained.
- Page, text, and OCR bounds are the classifier contract's. A document over one of them is read and answered, not rejected: it settles as `unknown` with `unknown_reason` `over_limit` (I-result).

### I-result: the result format, shared with the classifier contract

The provider classifier contract in Invoice Processor issue #139 owns the shared request/result schema. This is its consumer-facing draft proposal, not a competing schema. Before either contract is accepted, both must reference the same exact versioned schema and agree on all types, conditional fields, and compatibility rules. Email Watcher validates that accepted schema and job/attempt/source binding strictly; until agreement, implementation is blocked. Proposed fields:

- `record_version`: exactly the string `"1.0"`. Any other value is invalid;
- `file`: `sha256`, `byte_size`, `media_type`. A `sha256`, size, or media type that differs from the prepared bytes and the declared type they were submitted as is invalid (`classification_file_mismatch`);
- `classifier`: `id`, `version`. A version other than the one the job was created for is invalid (`classification_version_mismatch`);
- `class`: one of `invoice`, `quote`, `receipt`, `credit_note`, `statement`, `other`, `mixed`, `unknown`. `other` is a recognized document that is none of the financial classes; `mixed` is a file holding several documents; `unknown` is insufficient or conflicting evidence;
- `unknown_reason`, present exactly when `class` is `unknown`: one of `no_text` (no native text, and OCR found none), `unreadable` (the file cannot be opened, for example encrypted or corrupt), `conflicting_titles`, `insufficient_evidence`, or `over_limit`;
- `decided_by`: `rule` or `model`;
- `evidence`: the cited spans the decision rests on, each with page, exact text, span id, and role (`title`, `heading`, `label`, or `body`), verified by the classifier to be in the document; at least one unless `class` is `unknown`;
- `text_source`: `native`, `ocr`, or `none`, and `none` exactly when `class` is `unknown` with `unknown_reason` `no_text` or `unreadable`;
- `coverage`, present exactly when `text_source` is not `none`: the pages the title and heading scan read, out of the document's total, and the pages the model check sampled. The title scan reads every page within the bounds, so a second document deep in the file is found; only the model check samples;
- `payment_status`: an object with `state` (`paid_evidence`, `no_paid_evidence`, or `unknown`) and its own `evidence` spans. It is separate from the class and never decides it.

Any other shape is invalid (`classification_result_invalid`). An invalid result fails the inspection attempt permanently; the Connect job stays `completed`, as provider-completed evidence. Result text is untrusted provider output and renders as text only.

### I-records: invoice records

- **One extraction per attachment.** Engine extraction runs only on parts whose current result is `invoice`, through the same admission owner with engine origin, against the selected extractor. It is not created while a valid record of the selected extractor already exists for that part, and while a job of another origin for that part, input hash and admitted extractor is nonterminal, the part waits `deferred` (`awaiting_extraction`) for its result. It has its own state, with I-state's names, mapping, gate, budget, retry, and reasons, shown on the attachment row after the inspection's.
- **Every origin's result is a record.** An `invoice.extract` completion of any origin (engine, a user's rule, or an interactive invocation) is handed to the invoice-record validator exactly once per settled job, as `certificate.extract` completions are handed to the certificate ledger. Whatever ran it, a record counts only through the binding below.
- The provider-owned `invoice.extract` 1.0 record (`record_version` 1.1) is validated strictly and fails closed. An invalid record fails the extraction attempt permanently; the Connect job stays `completed`.
- One record is stored per `(message, part, Connect job)`, with its settlement time, canonical JSON, and digest, and no attachment bytes, exactly once per settled job, inside the transaction that settles it, fenced by the job id (and, for an engine job, its attempt id).
- **The binding.** A record states the hash of the bytes it read (`source.sha256`). It is bound to its part's inspection only when that hash equals the current result's `file.sha256`, the current result's class is `invoice`, and the record comes from the selected extractor's version. Both providers' outputs are then provably about the same bytes, and a record of a quote or of another file is never bound. A record the document layer reads through OCR must still state the hash of the submitted file, not of its OCR copy.
- A scanned invoice is classified through OCR, but `invoice.extract` 1.0 reads only native text. Until the document layer reads scanned PDFs for extraction too, its extraction fails with that reason, visibly, and it gives no claim.

### I-rules: the rule engine's document-class condition

- `docs/AUTOMATE_RULE_ENGINE_CONTRACT.md` gains the per-attachment condition `attachment.document_class`, with `equals` and `in` (at most six values, the engine's bound) over the class set of I-result. Every rule still needs its `attachment.media_type` condition, which selects the persisted attachment.
- **At analysis time**, inside `mark_analyzed`, a rule that has a document-class condition, and whose other conditions match a candidate, commits a class check `(message, part, rule_id, rule_version)` instead of a fire. Checks count with fires toward `MAX_AUTOMATION_FIRES_PER_MESSAGE`; on overflow, the message commits neither fires nor checks (`automation_fanout_limit`). A part that is embedded, or beyond its message's first 64 candidates, gets no check, so a class condition sees only candidates. The check is never resolved inside `mark_analyzed`, which still reads no provider output.
- **A check resolves in the pump:** in the transaction that settles its part under the selected classifier, or, for a part settled earlier, in the pump's next class-check pass. That transaction reads only the check's recorded rule version, the part's descriptor, and its settled inspection, and it first compares the message's mailbox identity key with the current account's; a mismatch closes the check (`mailbox_identity_changed`) with no fire. A class that satisfies the condition creates the fire with the check's rule version, under the existing identity fence; any other class closes the check with that class.
- A part that ends `uninspectable` or permanently `failed` closes its checks with that state as the reason, and no fire is created.
- **A check matches the rule version recorded at analysis.** Editing the rule after analysis does not change what an open check matches, so a new definition never applies retroactively.
- **Disabling or deleting the rule cancels its open checks**, closing them with reason `rule_disabled` or `rule_deleted` in the transaction that disables or deletes it, because disabling means stop. Fires already created are unchanged. This is the default; firing anyway would need an operator decision.
- A check resolves once; a later classifier version does not reopen it.
- Class checks exist only for messages the rule engine evaluates. Inspection covers every candidate; a message outside the rule engine's scope gets inspections but no checks.

### I-review: visibility

- Each candidate's state and reason render on its attachment row in the inbox and in the thread view, in Email Watcher's own words, as automation outcomes do today. Provider codes render as text only. `settled` `unknown` or `mixed`, permanent `failed`, and `uninspectable` read as needing review, with the reason. This is not a new notification channel.

### I-purge

- Message-delete triggers remove a message's inspections, attempts, class checks, extraction state, and invoice records on every deletion path (manual deletion, clearing, and the retention purge), under D-scope's purge rule, with secure delete.
- A Connect job identity follows `docs/CONTRACTS.md`: a job the provider may own stays as a non-content tombstone until its terminal outcome, and a tombstone's late result settles nothing and never writes the cache.
- A cache entry goes when no retained message's inspection holds its hash. The sweep's cursor is global and is not message data.
- A dry run inspects nothing and records nothing.

## Acceptance evidence

Each item needs a behavioral fail-first test during implementation. Current contract seam checks prove structure only. Scenarios below assume I-selection unless they test it.

- **Explicit selection:** a watched account with the setting off makes no fetch or handoff. Enabling new-mail selection does not send pre-enable attachments; explicit historical selection can queue only its permitted retained range. Disable before prepare, enqueue or POST prevents that handoff; no unrelated mailbox inherits selection. A provider-owned job still reconciles without duplicate POSTs. Previously authorized verified claims remain visible after disabling; a never-selected attachment gets no new claim from a cached classification or pre-existing extraction record.
- **Blank email:** a blank-body email whose only attachment, `scan_0012.pdf`, is positively classified `invoice` is inspected, extracted, and gives the claim D-claims defines. The same email whose PDF settles as any other class gives no claim, whatever the file name.
- **Category never gates:** an email analyzed as `informational`, and one whose analysis failed permanently, still have their PDFs inspected. A rule with `attachment.media_type equals application/pdf` and `attachment.document_class equals invoice` fires for an attachment-only invoice whose email category is `other`.
- **Class checks:** a document-class condition alone never enables account inspection or authorizes a background handoff; a rule edited between analysis and settlement fires with the version matched at analysis; a rule disabled or deleted in between cancels its open check, with no fire. A part settled before analysis finishes resolves its check in the next pump, never inside `mark_analyzed`. A check never resolves from another version's result, and a mailbox identity change closes it. 100 class rules on 64 candidates overflow the fan-out limit and commit no checks.
- **States and mapping:** each row of I-state's Connect mapping is reached: a full lane (`queue_full`), a queue deadline (`queue_deadline`, retried), an entitlement lapse (`entitlement_inactive`, with nothing prepared or created, resuming on reactivation), a transient mailbox outage while preparing (`source_fetch`, retried), a source deleted at the mailbox (`source_unavailable`), an invalid result (permanent, Connect job still `completed`), and post-POST ambiguity (stays `pending`).
- **Result shape:** an encrypted PDF settles `unknown` (`unreadable`) with `text_source` `none` and no coverage; a result with another `record_version` fails the attempt permanently.
- **Uninspectable:** an over-size PDF, a JPG the classifier does not accept, a non-PDF declared `application/octet-stream`, a message past the cutoff, and a legacy message with no verified mailbox key are `uninspectable` with their reasons.
- **Embedded parts:** a logo the HTML body references by `cid:` is not a candidate; a PDF marked `Content-Disposition: inline` that the body does not reference is a candidate.
- **Coverage:** a PDF whose page 12 is a quote, behind 11 pages of invoice, settles `mixed` or `unknown`, never `invoice`, and reports the pages its title scan read. A document over the page bound settles `unknown` (`over_limit`).
- **File identity:** an `invoice.extract` record whose `source.sha256` differs from the inspection's `file.sha256` is not used.
- **Engine origin:** an inspection job and an interactive Summarize of the same attachment never join each other. An engine job is refused at handoff without the Automations entitlement.
- **Starvation:** while any other job waits in a lane, no engine job is created there; a fire that arrives during a sweep waits behind at most one engine job, the lane never holds a second, and an engine job stuck in `reconciling` keeps any second one out.
- **Fairness:** with new mail arriving on every pump, `sweep` and `backfill` candidates still advance, oldest first.
- **Selection:** with two providers offering `document.classify`, candidates wait `ambiguous_provider` and no record changes. With Invoice Processor closed overnight and reopened at the same classifier version, no settled result becomes unsettled, no claim or discrepancy is deleted or recreated, and no sweep runs. A capability that declares effects is never selected.
- **Generations:** a new classifier version starts a generation and gives every retained candidate a new attempt; until each settles, its previous result stays current and no claim disappears. A late result of the old generation never displaces the new one. An old-generation job stuck in `reconciling` keeps its lane; supersession creates a distinct candidate attempt but no duplicate provider job or premature lane release. New work runs only when I-budget permits admission. A 45-day-old invoice in a followed thread, past a 30-day cutoff, keeps its result, with its version shown, and its claim and discrepancy stay.
- **Admitted versions:** a classifier of major version 2, and `invoice.extract` 2.0, are never selected; work waits with its reason, and no result of either is ever read.
- **Cache:** the same PDF in two messages runs one classification, including when both are queued before either settles; the same bytes submitted as two declared types are classified separately. A different later result for the same key never overwrites the first. Purging one message keeps the cache entry; purging both deletes it, with secure delete.
- **Purge:** deleting a message whose inspection job is `provider_owned` keeps only the job's non-content tombstone; its late result is discarded and writes no cache entry.
- **Restart:** a crash after an attempt is recorded and before its job settles replays the same attempt and job id, and settles once.
- **Existing attachments:** migration records retained candidate descriptors `deferred` (`selection_inactive`) and makes no fetch or handoff. Explicit historical selection queues only its selected retained range as `backfill`; eligible new mail receives the first opportunity under I-budget, and a selected pre-contract invoice can then supply the evidence D-claims requires.
- **Descriptor replacement:** an analysis retry keeps an unchanged part's inspection, restarts a changed part's, and a job of the replaced attempt settles nothing.
- **Order:** engine extraction runs only on a part whose current result is `invoice`, never on a part of another class.
- **One extraction per attachment:** a user rule that already extracted a part with the selected extractor supplies its record, and no engine extraction runs; a rule's extraction of a part classified `quote`, or of different bytes, is never bound.
- **Extraction state:** an extraction that is waiting, running, or failed is shown with its own state and reason, and the message has no invoice until its record settles.
- **Dry run:** inspects nothing and records nothing.

## Explicit non-scope

- Whether a document is paid or payable by the mailbox. I-result carries payment-status evidence; deciding payability needs the bill-to party and a later contract.
- Matching an invoice to a quote or agreement (D-claims).
- Any rule based on file names, or generic media-type sniffing and compatibility.
- **Older followed-thread mail before this contract.** A message already past the cutoff when this contract ships is never fetched again (D-scope), so its pre-contract parts are `uninspectable` (`outside_retention`), give no claim, and keep their message without an invoice. Inspecting them would change D-scope's fetch rule, which is the operator's decision.
- A new notification channel, and priorities between applications.

## Dependencies

- **The document classifier contract** in Invoice Processor (`document.classify`), drafted by Codex. It must state I-result identically, and its page, text, and OCR bounds. It should adopt Document Summarizer's accepted dominant-purpose classifier: typed values with `other` apart from `unknown`, `mixed` as an admitted result, bounded sampling with one expanded inspection for the model check, fail-closed on invalid output, and a source content hash binding. Its title and heading conflict scan must read every page within its bounds, since sampling alone cannot find a second document. It declares no effects. Its results are `record_version` `"1.0"`. It cannot accept itself: it waits for format agreement with this contract and a fresh review.
- **Extraction of scanned invoices** needs the document layer to read scanned PDFs for `invoice.extract` (I-records).
- **`docs/CONTRACTS.md`** gains the engine origin (I-gate). **`docs/CONNECT_V1.md`** references I-selection while preserving shared ADR-0003. **`docs/AUTOMATE_RULE_ENGINE_CONTRACT.md`** gains the `attachment.document_class` condition and class-check resolution as a second place fires are created (I-rules).

## Revision log

- 2026-10-10: proposed with the second D5 revision (#231), after Codex's review of `a61d76b` found that #231 required attachment inspection while no contract owned its gate, bounds, retries, versions, cache, or rule timing.
- 2026-10-10: the reviews of `d594f49` found the selected classifier following live discovery, inspection able to starve rule fires, pre-contract parts without a state, and a loose result format. A sweep of every rule here against `docs/CONTRACTS.md`, `docs/CONNECT_V1.md`, `docs/AUTOMATE_RULE_ENGINE_CONTRACT.md`, and the certificate ledger contract found the same root behind most of the rest: engine-owned jobs had no origin in the Connect queue. That historical draft added one and proposed two jobs per lane; the current I-budget replaces its unproved timing claim with admission ordering and one-job occupancy, Connect outcomes map to inspection states one way, class checks resolve only in the pump, selection follows Connect v1's no-implicit-winner rule, and the octet-stream header check is dropped in favor of a stated gap.
- 2026-10-10: the operator froze the inspection scope, required default-off explicit account selection and separate historical selection, and split D5 into PR #234. Rare unknown-size and mixed legacy caller cases moved to issues #232/#233; this revision changes neither the shared ADR nor existing callers' authorization.
