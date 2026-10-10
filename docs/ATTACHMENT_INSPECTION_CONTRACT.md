# Attachment inspection contract

Status: **PROPOSED** with the D5 revision of `docs/THREAD_VIEW_CONTRACT.md` (#231). Contracts only. Before either is accepted, this contract and the document classifier contract in Invoice Processor must state the same result format (I-result). Nothing here is implemented.

## Why

Email Watcher decides what an email is about from its sender, subject, body, and attachment file names (`model.py`, `_email_prompt`). It never reads a document. An automation that relies on the email's category can therefore miss an invoice sent with a blank or generic email, and Invoice Processor's `invoice.extract` reads every input as an invoice. The operator's direction (2026-10-10): inspect every supported attachment from the senders and accounts already watched, whatever the email says; classify it from the document's own evidence in the document-processing layer; send only invoices to extraction; and keep unreadable or uncertain documents visible instead of dropping them.

This contract owns the order of that work, its states, its queue, and what it stores. The document classifier contract owns how a document is classified. `docs/THREAD_VIEW_CONTRACT.md` (D-claims) owns what an inspection result means for vendor claims.

## Observable behavior

- Every attachment that inspection covers shows its inspection state and reason on its attachment row and in its thread.
- An email's wording, subject, file names, and analysis category never stop, start, or decide inspection.
- A blank email whose PDF is positively classified as an invoice, and passes the remaining checks of D-claims, gives an invoice claim. A file name proves nothing.
- A rule's document-class condition is decided only after the attachment's inspection settles.

## Definitions

### I-scope: which attachments are inspected

- **A candidate** is every attachment part persisted for a message admitted from a watched sender or account, under today's admission or D-capture's, in any admitted folder, while the message is retained, except an embedded part.
  - **A part is embedded** only when the message's HTML body references its Content-ID (`cid:`). Each persisted part records its Content-ID, and whether the body references it, when its content is fetched. `Content-Disposition` and Graph's `isInline` never decide it, because some mail clients mark real PDF attachments inline.
  - A part stored before this contract is a candidate, so a missing Content-ID can only add a candidate.
- **A candidate is inspectable** when all of these hold; otherwise it is `uninspectable` with the first reason that applies:
  - the active classifier (I-version) accepts its declared media type and size (`accepts_artifact`): reason `unsupported_media_type` or `too_large`;
  - its bytes can be fetched under today's fetch rules (IMAP at most 50 MiB): reason `unfetchable`;
  - it is among the first `MAX_INSPECTION_CANDIDATES_PER_MESSAGE` (64) candidates of its message in `(position, part_id)` order: reason `too_many_attachments`.
- The declared media type is the mailbox's. No byte sniffing and no file-name rule decides support.
- **Descriptor replacement.** Analysis retries replace a message's descriptors. A part whose `part_id`, media type, and byte size are unchanged keeps its inspection; any other part's inspection restarts, and a part that is gone loses its inspection, class checks, and queue entry in that transaction.

### I-state: an attachment's inspection

Every candidate has exactly one inspection state, kept with its reason, so the thread always shows why processing stopped:

| State | Meaning | Reason |
|---|---|---|
| `uninspectable` | I-scope rejected it | `unsupported_media_type`, `too_large`, `unfetchable`, `too_many_attachments` |
| `deferred` | waiting in the queue, nothing submitted | `queued`, `queue_full`, `budget`, `entitlement_inactive`, `classifier_unavailable` |
| `pending` | submitted as a Connect job, no result yet | the job id |
| `failed` | no valid result was obtained | the error code, `retryable` or `permanent`, attempts, and the next retry time |
| `settled` | a valid result under a classifier version | the result (I-result) |

- A provider that read the document but could not classify it returns a valid result with class `unknown`: that is `settled`, not `failed`.
- `failed` is retryable for transport errors, a busy or absent provider, an unavailable model, and a timeout. It is permanent for an invalid result, a file mismatch, and a capability that rejects the input at handoff.
- A retryable failure is retried on the analysis retry schedule (5, 15, 60, 360, and 1,440 minutes, then every 1,440 minutes while the message is retained). It is never skipped and never turns into `unknown` by itself.
- Readers treat every state other than `settled` under the active version as unsettled. D-claims owns what unsettled means for a claim.

### I-version: the active classifier

- **The active classifier** is the `document.classify` capability, by identity and version, of the provider instance Email Watcher's Connect catalog reports at the check. Its version changes whenever its rules, its model, or its OCR path change (classifier contract).
- **Only results of the active classifier count.** A result of any other version, including one that settles late, is stored with its version and ignored by every reader.
- **No active classifier.** While no installed provider offers `document.classify`, candidates stay `deferred` (`classifier_unavailable`) and resume when one appears.
- **A version change re-inspects retained work.** When a check observes a new active version, it records a durable sweep. The sweep sends every retained candidate whose inspection another version decided (`settled`, permanently `failed`, or `uninspectable` for its media type or size) back through I-scope's checks and, if inspectable, to `deferred` (`queued`), at most `MAX_INSPECTIONS_PER_PUMP` per pump, resuming from its durable cursor until it finishes. A candidate swept back keeps its old result visible until the new one settles, and readers treat it as unsettled meanwhile.

### I-cache: one classification per document and version

- A result is cached under `(file sha256, classifier id, classifier version)`. The hash is of the exact bytes submitted, computed where Connect jobs already compute it (`prepare_capability_job`).
- A candidate whose bytes, once fetched and hashed, match a cached key of the active classifier settles from the cache, with no job.
- A cache entry is deleted when no retained message holds an attachment with its hash (D-scope's purge, I-purge).

### I-order: the order of attachment work

Owned here, so no other component can reorder it:

1. **Descriptors are persisted** (today's step before analysis). In the same transaction, every new candidate is recorded `deferred` (`queued`), or `uninspectable` for a reason its descriptor already shows (media type, declared size, or count). Inspection therefore never waits for analysis, and a message whose analysis fails permanently is still inspected.
2. **The pump fetches a candidate's bytes** when it reaches it: a candidate that cannot be fetched becomes `uninspectable` (`unfetchable`), and the bytes' hash is computed for the cache and the job.
3. **The post-check pump submits inspections** under the gate (I-gate) and the budget (I-budget), through the one Connect admission owner, `_prepare_or_create_generic_connect_job`. No category, priority, or action-required value is read.
4. **A result settles** in the transaction that settles its Connect job, after strict validation (I-result).
5. **A part settled `invoice` under the active classifier** is recorded `deferred` for `invoice.extract` in that same transaction. Extraction runs on no other part (I-records).
6. **Class checks resolve** in that same settlement transaction (I-rules).

### I-gate: operation class

- Inspection and invoice extraction belong to D-ops' automation class (`docs/THREAD_VIEW_CONTRACT.md`): they may run OCR or a model, so they need `connect.capability_exchange` and `connect.automations`.
- While either is inactive, nothing new is submitted, waiting candidates stay `deferred` with reason `entitlement_inactive`, and submitted jobs follow today's handoff rule (`entitlement_paused`). Recording candidates (I-order step 1) is local and not gated.

### I-budget: bounded work

- At most `MAX_INSPECTIONS_PER_PUMP` (25) inspection submissions per pump, inside the pump's existing dispatch phase.
- Inspections and extractions share Invoice Processor's lane with every other job of that provider instance (25 nonterminal jobs per lane). A submission refused for capacity stays `deferred` (`queue_full`) and is retried next pump. Deferred work is never skipped and never times out into another state while its message is retained.
- Page, text, and OCR bounds are the classifier contract's. A document over one of them is read and answered, not rejected: it settles as `unknown` with `unknown_reason` `over_limit` and its coverage (I-result).

### I-result: the result format, shared with the classifier contract

The classifier contract owns this format. Email Watcher validates it strictly and fails closed. Both contracts must state these fields identically before either is accepted:

- `record_version`;
- `file`: `sha256`, `byte_size`, `media_type`. A `sha256` or size that differs from the submitted bytes fails the job permanently (`classification_file_mismatch`);
- `classifier`: `id`, `version`. A version other than the one the job was submitted to fails the job permanently (`classification_version_mismatch`);
- `class`: one of `invoice`, `quote`, `receipt`, `credit_note`, `statement`, `other`, `mixed`, `unknown`. `other` is a recognized document that is none of the financial classes; `mixed` is a file holding several documents; `unknown` is insufficient or conflicting evidence;
- `unknown_reason`, from a closed set, present exactly when `class` is `unknown`;
- `decided_by`: `rule` or `model`;
- `evidence`: the cited spans the decision rests on, each with page, exact text, and span id, verified by the classifier to be in the document; at least one unless `class` is `unknown`;
- `text_source`: `native` or `ocr`;
- `coverage`: the pages the title and heading scan read, out of the document's total, and the pages the model check sampled. The title scan reads every page within the bounds, so a second document deep in the file is found; only the model check samples;
- `payment_status`: `paid_evidence`, `no_paid_evidence`, or `unknown`, with its own evidence. It is separate from the class and never decides it.

Any other shape fails the job permanently (`classification_result_invalid`). Result text is untrusted provider output and renders as text only.

### I-records: invoice records

- `invoice.extract` runs only on parts settled `invoice` under the active classifier. Extraction has its own state, with I-state's names, gate, budget, retry, and reasons, shown on the attachment row after the inspection's.
- The provider-owned `invoice.extract` 1.0 record (`record_version` 1.1) is validated strictly and fails closed.
- One record is stored per `(message, part, Connect job)`, with its settlement time, canonical JSON, and digest, and no attachment bytes, exactly once per settled job, inside the transaction that settles it. The generic `connect.invoke` dispatcher stays the only path to the provider.
- A record states the hash of the bytes it read (`source.sha256`). It is bound to its part's inspection only when that hash equals the settled inspection's `file.sha256`, so both providers' outputs are provably about the same bytes; D-claims uses no other record. A record the document layer reads through OCR must still state the hash of the submitted file, not of its OCR copy.
- A scanned invoice is classified through OCR, but `invoice.extract` 1.0 reads only native text. Until the document layer reads scanned PDFs for extraction too, its extraction fails with that reason, visibly, and it gives no claim.

### I-rules: the rule engine's document-class condition

- `docs/AUTOMATE_RULE_ENGINE_CONTRACT.md` gains the per-attachment condition `attachment.document_class`, with `equals` and `in` over the class set of I-result.
- **At analysis time**, inside `mark_analyzed`, a rule that has a document-class condition, and whose other conditions match a part, commits a class check `(message, part, rule_id, rule_version)` instead of a fire. If that part is already `settled` under the active classifier, the check resolves in that transaction.
- **When the part settles** under the active classifier, each open check resolves in the settlement transaction. A class that satisfies the condition creates the fire with the check's rule version, under today's identity fence. Any other class closes the check with that class, and no fire is created.
- A part that ends `uninspectable` or permanently `failed` closes its checks with that state as the reason, and no fire is created.
- **A check matches the rule version recorded at analysis.** Editing the rule after analysis does not change what an open check matches, so a new definition never applies retroactively.
- **Disabling or deleting the rule cancels its open checks**, closing them with reason `rule_disabled` or `rule_deleted` in the transaction that disables or deletes it, because disabling means stop. Fires already created are unchanged (rule-engine contract, section 7). This is the default; firing anyway would need an operator decision.
- A check resolves once; a later classifier version does not reopen it.

### I-review: visibility

- Each candidate's state and reason render as authored text on its attachment row in the inbox and in the thread view, as automation outcomes do today. `settled` `unknown` or `mixed`, permanent `failed`, and `uninspectable` read as needing review, with the reason. This is not a new notification channel.

### I-purge

- D-scope's purge rule covers everything this contract stores about a message: its candidates' inspections, results, class checks, queue entries, sweeps, and invoice records go with it, with secure delete.
- A cache entry goes when no retained message holds its hash.
- A dry run inspects nothing and records nothing.

## Acceptance evidence

Each item needs a fail-first test.

- **Blank email:** a blank-body email whose only attachment, `scan_0012.pdf`, is positively classified `invoice` is inspected, extracted, and gives the claim D-claims defines. The same email whose PDF settles as any other class gives no claim, whatever the file name.
- **Category never gates:** an email analyzed as `informational`, and one whose analysis failed permanently, still have their PDFs inspected. A rule with only `attachment.document_class equals invoice` fires for an attachment-only invoice whose email category is `other`.
- **Class checks follow the analysis revision:** a rule edited between analysis and settlement fires with the version matched at analysis; a rule disabled or deleted in between cancels its open check, with no fire. A check never resolves from an inactive version's result.
- **States stay distinct:**
  - a full lane leaves a candidate `deferred` (`queue_full`), shown, and submitted on a later pump;
  - an entitlement lapse leaves candidates `deferred` (`entitlement_inactive`) with nothing submitted, and they resume on reactivation;
  - a transient provider failure is `failed` (`retryable`) with its next retry time, and later settles;
  - an invalid result, and a result whose hash differs from the submitted bytes, are `failed` (`permanent`).
- **Uninspectable:** an over-size PDF, a JPG the classifier does not accept, and a PDF declared `application/octet-stream` are `uninspectable` with their reasons. The 65th candidate of one message is `uninspectable` (`too_many_attachments`).
- **Embedded parts:** a logo the HTML body references by `cid:` is not a candidate; a PDF marked `Content-Disposition: inline` that the body does not reference is a candidate.
- **Coverage:** a PDF whose page 12 is a quote, behind 11 pages of invoice, settles `mixed` or `unknown`, never `invoice`, and reports the pages its title scan read. A document over the page bound settles `unknown` (`over_limit`).
- **File identity:** an `invoice.extract` record whose `source.sha256` differs from the inspection's `file.sha256` is not used.
- **Descriptor replacement:** an analysis retry keeps an unchanged part's inspection and restarts a changed part's.
- **Versions:** a new classifier version sweeps every retained settled part back to `deferred` and re-inspects it. A result from the old version that settles after the switch is ignored.
- **Cache:** the same PDF in two messages runs one classification. Purging one message keeps the cache entry; purging both deletes it, with secure delete.
- **Order:** extraction runs only after a part settles `invoice` under the active classifier, and never on a part of another class.
- **No classifier:** with no provider offering `document.classify`, candidates stay `deferred` (`classifier_unavailable`) and are inspected once one is installed.
- **Extraction state:** an extraction that is waiting, running, or failed is shown with its own state and reason, and the message has no invoice until its record settles.
- **Dry run:** inspects nothing and records nothing.

## Explicit non-scope

- Whether a document is paid or payable by the mailbox. I-result carries payment-status evidence; deciding payability needs the bill-to party and a later contract.
- Matching an invoice to a quote or agreement (D-claims).
- Byte sniffing, or any rule based on file names.
- A new notification channel.

## Dependencies

- **The document classifier contract** in Invoice Processor (`document.classify`), drafted by Codex. It must state I-result identically, and its page, text, and OCR bounds. It should adopt Document Summarizer's accepted dominant-purpose classifier: typed values with `other` apart from `unknown`, `mixed` as an admitted result, bounded sampling with one expanded inspection for the model check, fail-closed on invalid output, and a source content hash binding. Its title and heading conflict scan must read every page within its bounds, since sampling alone cannot find a second document. It cannot accept itself: it waits for format agreement with this contract and a fresh review.
- **Extraction of scanned invoices** needs the document layer to read scanned PDFs for `invoice.extract` (I-records).
- **`docs/AUTOMATE_RULE_ENGINE_CONTRACT.md`** gains the `attachment.document_class` condition, with its evaluation owned by I-rules.

## Revision log

- 2026-10-10: proposed with the second D5 revision (#231), after Codex's review of `a61d76b` found that #231 required attachment inspection while no contract owned its gate, bounds, retries, versions, cache, or rule timing.
