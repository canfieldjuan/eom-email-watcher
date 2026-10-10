# Attachment inspection contract

Status: **PROPOSED** with the D5 revision of `docs/THREAD_VIEW_CONTRACT.md` (#231). Contracts only. Before either is accepted, this contract and the document classifier contract in Invoice Processor must state the same result format (I-result). Nothing here is implemented.

## Why

Email Watcher decides what an email is about from its sender, subject, body, and attachment file names (`model.py`, `_email_prompt`). It never reads a document. An automation that relies on the email's category can therefore miss an invoice sent with a blank or generic email, and Invoice Processor's `invoice.extract` reads every input as an invoice. The operator's direction (2026-10-10): inspect every supported attachment from the senders and accounts already watched, whatever the email says; classify it from the document's own evidence in the document-processing layer; send only invoices to extraction; and keep unreadable or uncertain documents visible instead of dropping them.

This contract owns the order of that work, its states, its queue, and what it stores. The document classifier contract owns how a document is classified. `docs/THREAD_VIEW_CONTRACT.md` (D-claims) owns what an inspection result means for vendor claims. `docs/CONTRACTS.md` owns the Connect queue, which this contract uses through an engine origin it adds there (I-gate).

## Observable behavior

- Every attachment that inspection covers shows its inspection state and reason on its attachment row and in its thread.
- An email's wording, subject, file names, and analysis category never stop, start, or decide inspection.
- A blank email whose PDF is positively classified as an invoice, and passes the remaining checks of D-claims, gives an invoice claim. A file name proves nothing.
- A rule's document-class condition is decided only after the attachment's inspection settles.
- Inspection never goes ahead of a waiting rule fire, and adds at most one job ahead of a fire that arrives later. It makes no promise about time.

## Operator decisions recorded here (2026-10-10)

- **Inspection crosses the Connect boundary without a per-attachment selection.** `docs/CONNECT_V1.md` lets only an explicitly selected attachment's bytes cross the boundary. The operator's direction amends that for engine-origin inspection and extraction only: a candidate's bytes go to the selected `document.classify` and `invoice.extract` providers, under both entitlements (I-gate), and to nothing else.

## Definitions

### I-scope: which attachments are inspected

- **A candidate** is every attachment part whose descriptor today's message processing persists, for a message admitted from a watched sender or account, except an embedded part. Descriptors are persisted only where today's processing persists them, so inspection covers exactly those parts.
  - **A part is embedded** only when the message's HTML body references its Content-ID (`cid:`). `Content-Disposition` and Graph's `isInline` never decide it, because some mail clients mark real PDF attachments inline.
  - A part stored before this contract is a candidate, so a missing Content-ID can only add a candidate.
- **A candidate is inspectable** when all of these hold; otherwise it is `uninspectable` with the first reason that applies:
  - its message was received at or after the cutoff (D-scope): reason `outside_retention`;
  - its message's mailbox identity key equals the current account's verified key: reason `mailbox_identity_unverified`;
  - the selected classifier (I-version) accepts its effective media type and size (`accepts_artifact`): reason `unsupported_media_type` or `too_large`;
  - its size is known (the descriptor's `byte_size_known`): reason `size_unknown`;
  - its bytes fit today's fetch limits (IMAP at most 50 MiB): reason `too_large_to_fetch`;
  - it is among the first `MAX_INSPECTION_CANDIDATES_PER_MESSAGE` (64) candidates of its message in `(position, part_id)` order: reason `too_many_attachments`.
- **Decided once, when descriptors are stored.** When today's processing fetches a message's content, before it persists the descriptors, each part records:
  - its Content-ID, and whether the HTML body references it;
  - its **effective media type**: the mailbox's declared type, except that a part declared `application/octet-stream`, within today's fetch limits, whose first bytes are `%PDF-`, is `application/pdf`. Reading those bytes is part of that content fetch, so a failure follows its existing retry.

  The declared type is kept beside it for display. Every consumer reads only the effective type: the rule engine's `attachment.media_type` condition, Connect's job admission and the artifact it submits, and this contract's support check. No file-name rule decides anything.
- **Existing attachments.** The schema migration that installs this contract records every retained message's existing parts `deferred` with reason `backfill`, so no attachment stored before it is left without a state. The pump applies I-scope's checks when it reaches each one.
- **Descriptor replacement.** Analysis retries replace a message's descriptors. A part whose `part_id`, effective media type, and byte size are unchanged keeps its inspection. Any other part's inspection restarts with a new attempt, and a part that is gone loses its inspection, class checks, and extraction state in that transaction. Settlement is bound to the current attempt (I-order), so a job of a replaced attempt settles nothing.

### I-state: an attachment's inspection

Every candidate has exactly one inspection state, kept with its reason, so the thread always shows why processing stopped:

| State | Meaning | Reason |
|---|---|---|
| `uninspectable` | I-scope rejected it | `outside_retention`, `mailbox_identity_unverified`, `unsupported_media_type`, `too_large`, `size_unknown`, `too_large_to_fetch`, `too_many_attachments` |
| `deferred` | waiting in the queue, no job | `queued`, `backfill`, `sweep`, `awaiting_identical`, `awaiting_extraction`, `yielding_to_rules`, `queue_full`, `entitlement_inactive`, `classifier_unavailable`, `ambiguous_provider`, `classifier_version_unadmitted`, `extractor_version_unadmitted` |
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
- **A new generation re-inspects retained work.** The check records one durable, global sweep with its own cursor. The sweep gives every retained candidate whose newest decision belongs to an older generation (a settled or permanently failed attempt, or an `uninspectable` verdict for media type or size) a new attempt for the new generation, `deferred` (`sweep`), after I-scope's checks. A candidate that fails a check keeps its current result and records why. An older attempt still `pending` is superseded: its Connect job follows `docs/CONTRACTS.md` (it may keep reconciling or stay as a tombstone), its result, if it ever settles, is stored under its own generation, and the new attempt never waits for it.
- **The selected extractor** is recorded the same way for `invoice.extract`, without generations or a sweep, and only an admitted version is selected: `invoice.extract` 1.0, whose records are `record_version` 1.1. Any other version is never selected, extraction waits `deferred` (`extractor_version_unadmitted`), and adopting it needs a contract amendment that defines its record mapping.

### I-cache: one classification per document and version

- A result is cached under `(file sha256, effective media type, classifier id, classifier version)`: the hash of the exact bytes the admission owner prepared, and the type it submitted them as, since the type can select a different parser.
- **One job per key.** A candidate whose prepared hash matches a key that has a cached result settles from it, with no job. A candidate whose hash matches a key with an in-flight engine job waits `deferred` (`awaiting_identical`) and settles from that job's result. Only one engine job per key exists at a time.
- **First result wins.** The first valid result to settle for a key fills it; a later different result for the same key never overwrites it and is recorded as a conflict.
- A cache entry is written only from a result settled for a retained candidate's current attempt. It is deleted when no retained message's inspection holds its hash.

### I-order: the order of attachment work

Owned here, so no other component can reorder it:

1. **Descriptors are persisted** (today's step before analysis). In the same transaction, every new candidate is recorded `deferred` (`queued`), or `uninspectable` for a reason its descriptor and message already show. With no classifier selected, it is `deferred` with that reason. Inspection therefore never waits for analysis, and a message whose analysis fails permanently is still inspected.
2. **An attempt is recorded** before any fetch: a UUID that will be its Connect job id, persisted with the candidate, so a restart replays the same attempt.
3. **The admission owner prepares it.** The pump calls `_prepare_or_create_generic_connect_job` without creating a job (`create_job=False`): under the source lock it applies the retention, mailbox-identity, folder, and fetch rules, fetches the bytes, and computes their hash. The cache and coalescing (I-cache) are consulted with that hash.
4. **The admission owner creates the job** with engine origin (I-gate), unless the cache or an identical job settles the candidate. No category, priority, or action-required value is read.
5. **A result settles** in the transaction that settles its Connect job, once, fenced by `(attempt id, job id)`, after strict validation (I-result), and only for the candidate's current attempt.
6. **A part whose current result is `invoice`** is recorded `deferred` for extraction in that same transaction, unless I-records already has its record. Engine extraction runs on no other part (I-records).
7. **Class checks resolve** in that same transaction, and checks on parts that were already settled resolve in the pump's next class-check pass (I-rules).

### I-gate: engine origin and entitlements

- **Engine origin.** Inspection and extraction jobs are created with the engine origin that `docs/CONTRACTS.md` gains with this contract: an immutable origin written with the job row and bound to the message, part, attempt, capability identity and version, provider instance, and input hash. A job of engine origin needs both `connect.capability_exchange` and `connect.automations` at handoff, never joins a job of another origin, and is never joined by one.
- **Operation class.** They belong to D-ops' automation class (`docs/THREAD_VIEW_CONTRACT.md`).
- **While either entitlement is inactive,** no attempt is prepared or created, waiting candidates stay `deferred` (`entitlement_inactive`), and a job the queue fails with `CONNECT_ENTITLEMENT_REQUIRED` returns its candidate to that state. Recording candidates (I-order step 1) is local and not gated.
- **No effects.** A capability that declares effects is never selected (I-version), so inspection and extraction never need confirmation.

### I-budget: bounded and fair work

- **Engine jobs yield and stay few.** An engine job is created only while no other job is waiting in its lane and no other engine job in that lane is nonterminal. Otherwise the candidate stays `deferred` (`yielding_to_rules`).
- **What that guarantees, and what it does not.** The queue stays first in, first out (`docs/CONTRACTS.md`). An engine job never goes ahead of a job that was already waiting, and a job that arrives later waits behind at most one engine job. Inspection makes no promise about time: an engine job that never settles holds its lane exactly as any Connect job may (reconciliation in `docs/CONTRACTS.md`), and no second engine job is admitted to that lane meanwhile.
- **Own phase.** Each pump runs inspection after the rule-fire dispatch phase, in its own phase with its own `INSPECTION_PHASE_SECONDS` (5) deadline, advancing at most the pump's `limit` candidates.
- **Every candidate advances.** Inspection rotates among the waiting `queued`, `sweep`, and `backfill` candidates by a durable rotation, one at a time, so new mail cannot hold back older work forever; within each, the oldest message first, then `(position, part_id)`. Settling from the cache or from an identical job needs no job and is not rotated.
- Deferred work is never skipped and never times out into another state while its message is retained.
- Page, text, and OCR bounds are the classifier contract's. A document over one of them is read and answered, not rejected: it settles as `unknown` with `unknown_reason` `over_limit` (I-result).

### I-result: the result format, shared with the classifier contract

The classifier contract owns this format. Email Watcher validates it strictly and fails closed. Both contracts must state these fields identically before either is accepted:

- `record_version`: exactly the string `"1.0"`. Any other value is invalid;
- `file`: `sha256`, `byte_size`, `media_type`. A `sha256`, size, or media type that differs from the prepared bytes and the effective type they were submitted as is invalid (`classification_file_mismatch`);
- `classifier`: `id`, `version`. A version other than the one the job was created for is invalid (`classification_version_mismatch`);
- `class`: one of `invoice`, `quote`, `receipt`, `credit_note`, `statement`, `other`, `mixed`, `unknown`. `other` is a recognized document that is none of the financial classes; `mixed` is a file holding several documents; `unknown` is insufficient or conflicting evidence;
- `unknown_reason`, present exactly when `class` is `unknown`: one of `no_text` (no native text, and OCR found none), `unreadable` (the file cannot be opened, for example encrypted or corrupt), `conflicting_titles`, `insufficient_evidence`, or `over_limit`;
- `decided_by`: `rule` or `model`;
- `evidence`: the cited spans the decision rests on, each with page, exact text, span id, and role (`title`, `heading`, `label`, or `body`), verified by the classifier to be in the document; at least one unless `class` is `unknown`;
- `text_source`: `native`, `ocr`, or `none`, and `none` exactly when `class` is `unknown` with `unknown_reason` `no_text` or `unreadable`;
- `coverage`, present exactly when `text_source` is not `none`: the pages the title and heading scan read, out of the document's total, and the pages the model check sampled. The title scan reads every page within the bounds, so a second document deep in the file is found; only the model check samples;
- `payment_status`: `paid_evidence`, `no_paid_evidence`, or `unknown`, with its own evidence. It is separate from the class and never decides it.

Any other shape is invalid (`classification_result_invalid`). An invalid result fails the inspection attempt permanently; the Connect job stays `completed`, as provider-completed evidence. Result text is untrusted provider output and renders as text only.

### I-records: invoice records

- **One extraction per attachment.** Engine extraction runs only on parts whose current result is `invoice`, through the same admission owner with engine origin, against the selected extractor. It is not created while a valid record of the selected extractor already exists for that part, and while a job of another origin for that part and extractor is nonterminal, the part waits `deferred` (`awaiting_extraction`) for its result. It has its own state, with I-state's names, mapping, gate, budget, retry, and reasons, shown on the attachment row after the inspection's.
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

Each item needs a fail-first test.

- **Blank email:** a blank-body email whose only attachment, `scan_0012.pdf`, is positively classified `invoice` is inspected, extracted, and gives the claim D-claims defines. The same email whose PDF settles as any other class gives no claim, whatever the file name.
- **Category never gates:** an email analyzed as `informational`, and one whose analysis failed permanently, still have their PDFs inspected. A rule with `attachment.media_type equals application/pdf` and `attachment.document_class equals invoice` fires for an attachment-only invoice whose email category is `other`.
- **Class checks:** a rule edited between analysis and settlement fires with the version matched at analysis; a rule disabled or deleted in between cancels its open check, with no fire. A part settled before analysis finishes resolves its check in the next pump, never inside `mark_analyzed`. A check never resolves from another version's result, and a mailbox identity change closes it. 100 class rules on 64 candidates overflow the fan-out limit and commit no checks.
- **States and mapping:** each row of I-state's Connect mapping is reached: a full lane (`queue_full`), a queue deadline (`queue_deadline`, retried), an entitlement lapse (`entitlement_inactive`, with nothing prepared or created, resuming on reactivation), a transient mailbox outage while preparing (`source_fetch`, retried), a source deleted at the mailbox (`source_unavailable`), an invalid result (permanent, Connect job still `completed`), and post-POST ambiguity (stays `pending`).
- **Result shape:** an encrypted PDF settles `unknown` (`unreadable`) with `text_source` `none` and no coverage; a result with another `record_version` fails the attempt permanently.
- **Uninspectable:** a part whose provider omitted its size is `uninspectable` (`size_unknown`); an over-size PDF, a JPG the classifier does not accept, a non-PDF declared `application/octet-stream`, a message past the cutoff, and a legacy message with no verified mailbox key are `uninspectable` with their reasons.
- **Effective type:** a PDF declared `application/octet-stream` whose bytes begin `%PDF-` is stored with effective type `application/pdf`; a rule's `attachment.media_type equals application/pdf` matches it, Connect admits and submits it as a PDF, and it is inspected. A failure reading those bytes retries the message's content fetch. A file named `invoice.pdf` declared as an image stays an image. The 65th candidate of one message is `uninspectable` (`too_many_attachments`).
- **Embedded parts:** a logo the HTML body references by `cid:` is not a candidate; a PDF marked `Content-Disposition: inline` that the body does not reference is a candidate.
- **Coverage:** a PDF whose page 12 is a quote, behind 11 pages of invoice, settles `mixed` or `unknown`, never `invoice`, and reports the pages its title scan read. A document over the page bound settles `unknown` (`over_limit`).
- **File identity:** an `invoice.extract` record whose `source.sha256` differs from the inspection's `file.sha256` is not used.
- **Engine origin:** an inspection job and an interactive Summarize of the same attachment never join each other. An engine job is refused at handoff without the Automations entitlement. A job that existed before the amendment, joined by both an interactive invocation and a fire, keeps both callers and today's rules.
- **Starvation:** while any other job waits in a lane, no engine job is created there; a fire that arrives during a sweep waits behind at most one engine job, the lane never holds a second, and an engine job stuck in `reconciling` keeps any second one out.
- **Fairness:** with new mail arriving on every pump, `sweep` and `backfill` candidates still advance, oldest first.
- **Selection:** with two providers offering `document.classify`, candidates wait `ambiguous_provider` and no record changes. With Invoice Processor closed overnight and reopened at the same classifier version, no settled result becomes unsettled, no claim or discrepancy is deleted or recreated, and no sweep runs. A capability that declares effects is never selected.
- **Generations:** a new classifier version starts a generation and gives every retained candidate a new attempt; until each settles, its previous result stays current and no claim disappears. A late result of the old generation never displaces the new one. An old-generation job stuck in `reconciling` is superseded, and the new attempt runs on the new provider without waiting for it. A 45-day-old invoice in a followed thread, past a 30-day cutoff, keeps its result, with its version shown, and its claim and discrepancy stay.
- **Admitted versions:** a classifier of major version 2, and `invoice.extract` 2.0, are never selected; work waits with its reason, and no result of either is ever read.
- **Cache:** the same PDF in two messages runs one classification, including when both are queued before either settles; the same bytes submitted as two effective types are classified separately. A different later result for the same key never overwrites the first. Purging one message keeps the cache entry; purging both deletes it, with secure delete.
- **Purge:** deleting a message whose inspection job is `provider_owned` keeps only the job's non-content tombstone; its late result is discarded and writes no cache entry.
- **Restart:** a crash after an attempt is recorded and before its job settles replays the same attempt and job id, and settles once.
- **Existing attachments:** after the migration, every retained message's existing parts are `deferred` (`backfill`), are inspected after new mail, and a retained pre-contract email with an invoice PDF gets its claim.
- **Descriptor replacement:** an analysis retry keeps an unchanged part's inspection, restarts a changed part's, and a job of the replaced attempt settles nothing.
- **Order:** engine extraction runs only on a part whose current result is `invoice`, never on a part of another class.
- **One extraction per attachment:** a user rule that already extracted a part with the selected extractor supplies its record, and no engine extraction runs; a rule's extraction of a part classified `quote`, or of different bytes, is never bound.
- **Extraction state:** an extraction that is waiting, running, or failed is shown with its own state and reason, and the message has no invoice until its record settles.
- **Dry run:** inspects nothing and records nothing.

## Explicit non-scope

- Whether a document is paid or payable by the mailbox. I-result carries payment-status evidence; deciding payability needs the bill-to party and a later contract.
- Matching an invoice to a quote or agreement (D-claims).
- Any rule based on file names, and any byte check beyond the `%PDF-` check of a part declared `application/octet-stream`.
- **Older followed-thread mail before this contract.** A message already past the cutoff when this contract ships is never fetched again (D-scope), so its pre-contract parts are `uninspectable` (`outside_retention`), give no claim, and keep their message without an invoice. Inspecting them would change D-scope's fetch rule, which is the operator's decision.
- A new notification channel, and priorities between applications.

## Dependencies

- **The document classifier contract** in Invoice Processor (`document.classify`), drafted by Codex. It must state I-result identically, and its page, text, and OCR bounds. It should adopt Document Summarizer's accepted dominant-purpose classifier: typed values with `other` apart from `unknown`, `mixed` as an admitted result, bounded sampling with one expanded inspection for the model check, fail-closed on invalid output, and a source content hash binding. Its title and heading conflict scan must read every page within its bounds, since sampling alone cannot find a second document. It declares no effects. Its results are `record_version` `"1.0"`. It cannot accept itself: it waits for format agreement with this contract and a fresh review.
- **Extraction of scanned invoices** needs the document layer to read scanned PDFs for `invoice.extract` (I-records).
- **`docs/CONTRACTS.md`** gains the engine origin (I-gate). **`docs/CONNECT_V1.md`** records the boundary decision above. **`docs/AUTOMATE_RULE_ENGINE_CONTRACT.md`** gains the `attachment.document_class` condition and class-check resolution as a second place fires are created (I-rules).

## Revision log

- 2026-10-10: proposed with the second D5 revision (#231), after Codex's review of `a61d76b` found that #231 required attachment inspection while no contract owned its gate, bounds, retries, versions, cache, or rule timing.
- 2026-10-10: the reviews of `d594f49` found the selected classifier following live discovery, inspection able to starve rule fires, pre-contract parts without a state, and a loose result format. A sweep of every rule here against `docs/CONTRACTS.md`, `docs/CONNECT_V1.md`, `docs/AUTOMATE_RULE_ENGINE_CONTRACT.md`, and the certificate ledger contract found the same root behind most of the rest: engine-owned jobs had no origin in the Connect queue. The queue gains one, inspection holds at most two jobs per lane so first-in-first-out order still protects rule fires, Connect outcomes map to inspection states one way, class checks resolve only in the pump, selection follows Connect v1's no-implicit-winner rule, and the octet-stream header check is dropped in favor of a stated gap.
