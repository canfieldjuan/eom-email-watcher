# Thread View and Vendor Claims Contract

Status: accepted by the operator on 2026-10-05, with D1-D3 as recommended, and restructured the same day into a definitions-first form (see the revision log). Decision D4 (comparison across a vendor's threads) was added on 2026-10-09, and decision D5 (invoice PDFs as a claim source) was proposed the same day. Each milestone still needs its own accepted plan before code.

**How to read and change this contract.**

- Every rule is stated once, in [Definitions](#definitions). Every other section links to the definition it uses.
- `tests/test_thread_view_contract.py` enforces this, and fails in three cases:
  - a definition's own text (any rule line of eight or more words) appears verbatim in another normative section;
  - a listed key phrase of a rule appears outside its definition;
  - a definition link no longer resolves.

  It cannot detect a paraphrase. A fix therefore changes the one definition, never a copy.
- Milestone plans own the exact provider calls, schema, and tests, under these definitions. A finding about one milestone's mechanics belongs on that milestone's plan PR. This contract changes only when a definition is wrong.

## Why this arc exists

The operator wants Email Watcher users to "keep vendors honest". A user should see a whole conversation with a vendor, including what was quoted, promised, or agreed earlier and their own replies. They should be told when a later message or invoice departs from an earlier claim, including when the agreement and the invoice arrive in different threads (decision D4) and when the invoice's figures are only in an attached PDF (decision D5).

Today the product cannot do this, by construction. The findings below come from `origin/main` at `8c1e076`, re-checked at `6a068bf`.

- **Admission is per message, from the inbox only.** A message is admitted only if it carries `INBOX` and its `From` matches a watched address, or carries a selected Gmail user label (`service.py:153-199`, `service.py:166`).
  - Microsoft 365 reads only `mailFolders/inbox` (`microsoft365.py:275`, `microsoft365.py:615`).
  - IMAP selects only `INBOX` (`imap.py:1366`).
  - Sent mail is never read, and non-matching messages leave no trace (`service.py:2105-2106`).
- **Thread identity is stored but never used, and is wrong for IMAP.** `messages.thread_id` (`db.py:4602`) holds Gmail `threadId` (`gmail.py:490`) and Microsoft `conversationId` (`microsoft365.py:611-633`).
  - For IMAP it holds the message's own `Message-ID` (`imap.py:1613`).
  - `In-Reply-To`, `References`, `To`, and `Cc` are never fetched (`gmail.py:937-938`, `imap.py:1599`).
- **Bodies are never stored or displayed.** There is no body column (`db.py:4597-4629`), and bodies are discarded after analysis (`service.py:2319`).
- **There is no backfill.** Every provider starts at "now" (`gmail.py:768-769`, `microsoft365.py:278-280`, `imap.py:1434-1448`).
- **Retention purges per message by `received_at`** (`db.py:11834`, `service.py:2107-2113`).
- **Analysis sees one message at a time, with no evidence spans** (`model.py:80-92`). Scheduling is the only evidence-bound extractor (`scheduling.py:387-429`).
- **The existing read-only scopes permit this arc.** They are `gmail.readonly` (`gmail.py:42`) and `Mail.Read` (`microsoft365.py:36`).
- **The webview has no Content Security Policy** (`desktop/src-tauri/tauri.conf.json`, `"csp": null`).

## Operator decisions already made (2026-10-05)

1. Store email bodies locally; they never go to a cloud service.
2. Read Sent mail.
3. Vendor matching follows the thread, plus vendor records:
   - several exact addresses per vendor;
   - domains are opt-in, and refused for public providers;
   - new addresses are suggested, never auto-added;
   - Sent mail matches on recipients.
4. The feature is paid Connect.
5. Prerequisites are done: #194 (PR #195) and #146 (PR #199).

## Definitions

Each definition is the only place its rule is stated.

### D-vendor: vendors and `vendor_of(address)`

- **A vendor** is a named record that owns:
  - exact addresses, normalized with `normalize_validated_address` (`config.py:245`);
  - opted-in business domains.
- **Uniqueness.** Each address, and each domain, belongs to at most one vendor. Assigning it to a second vendor returns `conflict`.
- **Public domains are refused.** The public-provider list is one constant in code, and nothing else classifies domains. It starts as exactly gmail.com, googlemail.com, yahoo.com, outlook.com, hotmail.com, live.com, icloud.com, me.com, aol.com, proton.me, protonmail.com, and gmx.com. Adding a domain is a code change with a test.
- **`vendor_of(address)`** returns a vendor and how it matched:
  - none for the mailbox's verified identities, before either lookup;
  - else the vendor that owns the exact address, as an exact match;
  - else the vendor that owns the address's domain, as a domain match;
  - else none.

  A record that covers a verified identity, such as an opted-in domain or an address whose mailbox is connected later, is still allowed; the identity is simply excluded.
- **The mailbox's verified identities** are `mail_accounts.address` and the session's authenticated address. Through `vendor_of` they never have a vendor, and they are never suggestion candidates.
- **Suggestion candidates** are senders of inbound messages in a followed thread ([D-follow](#d-follow-followed-threads)) that meet all of these:
  - `vendor_of` returns none;
  - the sender is not one of the mailbox's verified identities;
  - the `(owner, address)` pair has not been dismissed ([D-ops](#d-ops-operation-classes-and-gating)).

  Each candidate is offered to the thread's owner as "Add <address> to <vendor>?". Suggestions never capture mail.

### D-attribution: a message's vendor

- **Inbound:** `vendor_of(From)`.
- **Outbound,** over the `To`, then `Cc`, recipients in stored header order:
  - the vendor of the first recipient whose `vendor_of` is an exact match;
  - else of the first whose `vendor_of` is a domain match;
  - else none.

  Recipients are judged only through `vendor_of`, so its exclusions apply to outbound mail too.
- **Each message has at most one attributed vendor.** It shows that vendor alone, and its claims belong to that vendor.
- **A vendor's threads** are:
  - the threads it owns ([D-follow](#d-follow-followed-threads));
  - plus, listed as linked, the threads that contain a message attributed to it.

### D-scope: in-scope messages and retention

- **Folders.** A message is in scope only while it is in an admitted folder:
  - Gmail: the `INBOX` or `SENT` label;
  - Microsoft 365: the Inbox or Sent Items folder;
  - IMAP: `INBOX`, or the folder with the RFC 6154 `\Sent` attribute (falling back to a configured name).
  - No other folder is ever fetched on purpose: no request targets archive, deleted items, drafts, or clutter. Gmail and Microsoft ids are mailbox-wide, so a message that leaves an admitted folder between a listing and its fetch can still answer; every fetch response carries the message's folders (`labelIds`, `parentFolderId`, or the IMAP folder selected for it), and a response outside the admitted folders is discarded with nothing stored.
  - An IMAP account with no resolvable Sent folder has only `INBOX` in scope, and shows "Sent mail unavailable".
- **The retention cutoff** is `now - retention_days`.
  - A logical message's received time, wherever this contract compares a message with the cutoff, is its newest copy's ([D-identity](#d-identity-message-identity-direction-and-thread-keys)), so a message is within the cutoff while any copy is; a copy not yet captured is judged by its own. What the purge keeps (below) can outlast what may be fetched: a followed thread keeps older messages it never fetches again.
  - Only messages received at or after the cutoff are fetched or captured. Fetching a message reads its headers or body; a listing that returns ids and received times is how a pass finds candidates, and fetches nothing.
  - Mail that aged past the cutoff before it was captured is never recovered, and its thread shows "history partial".
- **Purge.**
  - A message outside a followed thread is purged once it is older than the cutoff, as today.
  - A followed thread is purged as one unit once its newest message is older than the cutoff (decision D1) and the account's coverage is current ([D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass)), so a thread whose newer replies are still unfetched is kept.
  - Purge takes bodies, claims, discrepancies, claim attempts, and invoice records with their message. Deletes run with `PRAGMA secure_delete = ON`.

### D-capture: what is stored, and its provenance

- **Today's admission is unchanged:**
  - `exact_sender`: `From` is a watched address;
  - `gmail_user_label`: a selected Gmail label.
- **While the gated class is allowed ([D-ops](#d-ops-operation-classes-and-gating)), an in-scope message ([D-scope](#d-scope-in-scope-messages-and-retention)) is also captured when:**
  - it is inbound with a vendor ([D-attribution](#d-attribution-a-messages-vendor)) from an exact match (`vendor_address`) or a domain match (`vendor_domain`);
  - it is outbound with a vendor (`sent_to_vendor`);
  - or it belongs to a followed thread (`thread_follow`, [D-follow](#d-follow-followed-threads)).
- **Anything else leaves no trace.**
- **Admission kind.** When a message matches several rules, its kind is the first match in `exact_sender`, `vendor_address`, `vendor_domain`, `sent_to_vendor`, `thread_follow`, `gmail_user_label`. `exact_sender` therefore always means the sender was watched, and `vendor_address` means only the vendor record matched.
  - The kind is computed from message metadata and the current configuration, never from the discovery path.
  - It is stored once, as immutable provenance (`db.py:3645-3671`), and never rewritten.
  - `messages.admission_kind` stays a closed set (`db.py:3606`), widened by migration.

### D-follow: followed threads

- **A thread is followed exactly while** it contains a stored message that has a vendor ([D-attribution](#d-attribution-a-messages-vendor)) under the current configuration.
  - Following is a classification of stored data, not an operation.
- **Its owner** is the vendor of the earliest-received such message. Ties are broken by the canonical order ([D-identity](#d-identity-message-identity-direction-and-thread-keys)).
- **Follow state and owner are derived, never recorded history.**
  - They are a function of the inputs of [D-derived](#d-derived-derived-state-and-invalidation), cached in one row per thread key and kept current by it.
  - A thread with no stored messages has no cache row and no watermark.
  - The result therefore never depends on arrival or discovery order. Stored provenance is never consulted.

### D-identity: message identity, direction, and thread keys

- **Source identity.**
  - Gmail message ids are mailbox-wide.
  - Microsoft ids are immutable ids (`Prefer: IdType="ImmutableId"`) and survive moves.
  - IMAP ids are `mailbox:UIDVALIDITY:UID` (`imap.py:320-321`), with a folder token added for the Sent folder, so the unique source key (`db.py:3585-3588`) separates folders.
- **Logical identity.** A message with a `Message-ID` also has the logical identity `(provider, account, mailbox identity, Message-ID)`.
  - A second location of an already-captured logical identity is recorded, not captured again.
  - Without a `Message-ID`, a moved IMAP message can be captured twice; this is best-effort.
- **Recorded locations.** Each copy of a logical message records the admitted folders it was observed in. A recorded location is confirmed when the copy's latest folder observation is complete with every admitted folder in scope and its headers have been fetched under that same source identity. Header-fetch evidence comes only from a response accepted under D-scope and is stored independently of recipients and capture date context; it persists across incomplete folder observations and polling gaps, and never transfers between source identities. Folder observations are ordered by their UTC observation time, recorded when the response or invalidation is received, independently of database lock acquisition; at equal times an incomplete observation wins. Migration does not infer that evidence from those fields or an earlier confirmation stamp; existing locations start unconfirmed until their source headers are fetched. A latest incomplete folder observation or a gap in polling clears confirmation ([D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass)). A fetch of the copy confirms what it observes when every admitted folder is in scope. Direction reads recorded locations whether or not they are confirmed; an unconfirmed one marks the message for discovery to observe again.
- **Canonical order.** The canonical order of messages is their source identity in byte order. A logical message recorded in several locations sorts by the smallest of their source identities. Every tie-break in this contract uses it.
- **Direction** is derived from the logical message's recorded locations ([D-derived](#d-derived-derived-state-and-invalidation)). It is `outbound` once any location is a Sent folder, or carries Gmail's `SENT` label (even with `INBOX`); otherwise it is `inbound`. Discovery order therefore never decides it.
- **Thread key:**
  - Gmail uses `threadId`, and Microsoft 365 uses `conversationId`.
  - IMAP uses components: messages whose id sets overlap form one component. A message's id set is its own `Message-ID`, `In-Reply-To`, and a bounded `References` list. A message that touches no component forms its own.
  - Which messages share a component does not depend on arrival order. The key does: it is an opaque UUIDv4 handle, allocated when its component is created. No rule may depend on a key's value, only on its members.
  - Every captured message has a thread key, including one whose provider omits a thread id, which forms its own thread.
- **IMAP merges.** A message that bridges several components merges them into one survivor, in one transaction. The survivor is the component whose smallest member under the canonical order sorts first, whether or not its members have a `Message-ID`, and it keeps its key. In that transaction:
  - every row naming a merged key is re-keyed, including earlier aliases, and aliases are recorded.

### D-derived: derived state and invalidation

- **Derived state** is what this contract computes and keeps, rather than records:
  - each message's direction ([D-identity](#d-identity-message-identity-direction-and-thread-keys)) and attributed vendor ([D-attribution](#d-attribution-a-messages-vendor));
  - each thread's follow state and owner ([D-follow](#d-follow-followed-threads));
  - which messages keep a body, and which reasons for a missing body still hold ([D-body](#d-body-stored-bodies));
  - which messages have claims, discrepancies, and attempts, and under which key ([D-claims](#d-claims-claims-and-comparability)).
- **Its inputs** are exactly:
  - the stored messages, with their headers, received times, and recorded locations;
  - the vendor records;
  - the mailbox's verified identities ([D-vendor](#d-vendor-vendors-and-vendor_ofaddress));
  - the active claims extractor version ([D-claims](#d-claims-claims-and-comparability)): recording a new one supersedes the previous key's claims, discrepancies, and attempts in that transaction, and reconciliation creates the replacements;
  - the stored claims and reference profiles, from which the comparison outcomes of [D-claims](#d-claims-claims-and-comparability) are computed. The reconcile pass storing a message's claims is a change to this input;
  - the stored invoice records and the record mapping version ([D-claims](#d-claims-claims-and-comparability)): storing a record, or a new mapping version, is a change to this input, and the selected records' claims follow in that transaction;
  - the attachments' classes ([D-claims](#d-claims-claims-and-comparability)): an inspection settling, failing, or running again under a new classifier version is a change to this input.

  All of them live in the database. Admission provenance and discovery order are never inputs.
- **One rule.** Any change to an input, by any path, brings everything derived from it back in line with its definition, in the same transaction as the change and under the operation lock (`engine_api.py:2192-2199`).
  - The paths include capture, a recorded location, deletion, purge, an IMAP merge, a vendor record change, and a verified identity being added or changed. That list is illustrative; the rule is not.
- **Only local work happens in that transaction.** It recomputes state and deletes what a definition no longer allows. Anything that needs a provider or the model, such as a body to fetch or claims to extract, is left to the reconcile pass ([D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass)).

### D-reconcile: coverage and the reconcile pass

- **The coverage record.** Each account keeps a durable coverage record: the coverage generation it has reconciled, and a `synced_through` watermark per followed thread.
- **The coverage generation** is a counter per account. It increases by one on every change to a reconcile input, whichever operation makes it:
  - a vendor record change: an address or domain added or removed, or a vendor deleted;
  - a verified identity added or changed ([D-vendor](#d-vendor-vendors-and-vendor_ofaddress)), since it changes what `vendor_of` returns;
  - a `retention_days` change;
  - a change in the entitlement state;
  - a new claims extractor version;
  - a provider cursor that expires or recovers;
  - the Sent folder the account resolves ([D-scope](#d-scope-in-scope-messages-and-retention)): a different folder, or Sent becoming available or unavailable.

  Every check records the `retention_days`, entitlement state, and Sent folder it observes and counts a difference as a change, so an edit to the configuration file and a lapse that ends both count. A change and its reversal are two increases, so a removed address that returns, or an entitlement that lapses and returns, is reconciled again.
- **Coverage is stale exactly when** the current generation differs from the reconciled one, a followed thread has no watermark, or derived work is pending: a message in a followed thread lacks the body or claim attempt its definition requires, or has a retryable attempt whose backoff deadline has passed ([D-claims](#d-claims-claims-and-comparability)), which [D-derived](#d-derived-derived-state-and-invalidation) leaves to this pass. This is a comparison made at every check, so no change has to remember to trigger it. The cutoff moving forward with the clock never makes coverage stale.
- **Watermarks.** A watermark exists only while its thread is followed ([D-derived](#d-derived-derived-state-and-invalidation) drops it otherwise), so a newly followed thread has none. A followed thread is synced from its watermark, or from the cutoff when it has none. An IMAP merge clears the survivor's watermark; a `retention_days` increase, a change of the account's Sent folder, or a gap in polling (an entitlement lapse that ends, or a cursor that expired or recovered) clears every watermark, since mail that polling could not admit during the gap may predate them. A gap also leaves every recorded location of the account unconfirmed ([D-identity](#d-identity-message-identity-direction-and-thread-keys)), since folder changes during it went unobserved. The stages confirm those of the stored messages they fetch; any other stored message, including one older than the cutoff, which no stage fetches ([D-scope](#d-scope-in-scope-messages-and-retention)), keeps the locations last recorded for it until polling reports a change or a later pass fetches it.
- **Stale coverage triggers a reconcile pass.** It is bounded per check, resumable from durable progress, and the only writer of coverage and watermarks. It runs three stages, in order:
  - **(a) Discovery:** in-scope messages that have a vendor ([D-attribution](#d-attribution-a-messages-vendor)), whether not captured yet or captured with an unconfirmed location ([D-identity](#d-identity-message-identity-direction-and-thread-keys)), are captured under [D-capture](#d-capture-what-is-stored-and-its-provenance); for a captured one, that records the folders and recipients its fetch observes. Their threads' follow state then follows from [D-follow](#d-follow-followed-threads).
  - **(b) Thread sync:** for each followed thread, its in-scope messages are fetched from its watermark and captured under [D-capture](#d-capture-what-is-stored-and-its-provenance).
  - **(c) Derived work:**
    - bodies ([D-body](#d-body-stored-bodies)) for messages that lack one;
    - claim attempts that are missing for their current [D-claims](#d-claims-claims-and-comparability) key, or are due for retry.
- **Failures.**
  - A provider error retries that unit with backoff, and polling is unaffected.
  - If the gated class stops being allowed mid-pass, the pass stops at its next budget check and keeps its progress.
- **Normal polling keeps coverage current between changes.**
  - Gmail has one mailbox-wide history id. A checkpoint advances only after every event in its range has been handled for both the `INBOX` and `SENT` labels.
  - Microsoft Inbox and Sent Items have separate delta links, and IMAP `INBOX` and Sent have separate `UIDVALIDITY` and UID cursors. One of these folder cursors never moves another.

### D-body: stored bodies

- **Which messages store a body.** From M2 on, every captured message in a followed thread ([D-follow](#d-follow-followed-threads)) stores a body. Messages outside followed threads keep today's summary-only storage, so a thread that stops being followed loses its bodies ([D-derived](#d-derived-derived-state-and-invalidation)).
- **What is stored:**
  - the normalized text, from `bounded_body_text` with a named storage cap larger than `body_char_limit`;
  - the pre-cut length;
  - the date context: received time and the configured time zone, recorded at capture.
- **Raw HTML is never stored**, and bodies never render as markup.
- **Quote boundaries survive normalization.** When the source is HTML, the stored text keeps quote containers as `>`-prefixed lines; the M2 plan names the recognized containers. Today's analysis normalization is unchanged.
- **Each message shows exactly one body state:**
  - the stored text;
  - the stored text, marked "Partial body: first N of M characters" when it is shorter than its pre-cut length;
  - "Body not stored (source no longer available)";
  - "Body not stored (outside the admitted folders)";
  - "Body not stored yet", pending [D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass) stage (c).

### D-ops: operation classes and gating

- **Every operation belongs to exactly one class.**
  - **read:** `vendors.list`, and listing threads, messages, bodies, claims, and discrepancies.
    - Never gated, and never calls a provider or model.
  - **gated:** `vendors.create`, `vendors.rename`, `vendors.addresses.add`, `vendors.domains.add`, and accepting a suggestion. Also capture beyond today's admission, body storage, reconcile, and claim extraction.
    - All need the paid entitlement `connect.capability_exchange` (decision D3, `require_connect_entitlement`).
  - **automation:** producing invoice records ([D-claims](#d-claims-claims-and-comparability)). Only the user's own enabled rule that runs `invoice.extract` produces them, so they need `connect.automations` as well, as every rule does (decision D5, `_require_automation_entitlement`). Claims from a stored record are derived state, not an operation, and need no entitlement.
  - **removal:** `vendors.addresses.remove`, `vendors.domains.remove`, `vendors.delete`, and dismissing a suggestion.
    - Never gated.
- **The desktop shows controls by class.**
  - Gated controls are hidden while the entitlement is inactive, under a "Connect required to update" notice.
  - Read and removal controls always show.
- **While the entitlement is inactive,** today's admission and inbox continue, and cursors advance.
- **Watchlist link.** `vendors.addresses.add` also adds the address to the watchlist, in the existing mutation (`engine_api.py:5506`), so the inbox's sender navigation shows vendor mail.
  - `watchlist.remove` of an address that belongs to a vendor returns `conflict`, naming the vendor.
  - A hand-edited config can still drop one. `vendors.list` then shows `watched: false` with a "Watch again" action.
  - Capture and following never depend on the watchlist.
- **Removal effects:**
  - Removing an address records a dismissal of its `(vendor, address)` pair.
  - Removing a domain stops its matches.
  - Deleting a vendor removes its addresses, domains, and dismissals in one transaction.
    - It can also stop watching its addresses ("Also stop watching these addresses", off by default).
    - Addresses left watched keep today's `exact_sender` admission.
- **Two stores.** The watchlist is a separate file, so no operation changes it and the database in one transaction. An operation that changes both writes the watchlist first, in one write, then the database. An interruption therefore leaves either an extra watched address or a vendor address shown `watched: false`, and a retry completes the operation.

### D-claims: claims and comparability

- **Source.** Each inbound message with a vendor, in a followed thread, is offered to the extraction task. The source text is computed as follows:
  1. Cut the stored body at `body_char_limit` with `bounded_body_text`, the model-input bound, recording truncation as #146 does.
  2. Remove the quoted segments:
     - every line that starts with `>`, wherever it appears;
     - the reply header that introduces a quote: an `On ... wrote:` line, or an Outlook `From:/Sent:/To:/Subject:` block. An unprefixed Outlook header quotes everything after it.

  Everything else is the *authored text*, including text written below a `>`-quoted block. Subjects and quoted history are never evidence. The segmentation algorithm belongs to the M4 plan.
- **Invoice records (decision D5)** are the second source of an offered message.
  - **An attachment's class** is the document class attachment inspection settled for it (Dependencies): `invoice`, `quote`, `receipt`, `credit_note`, `statement`, or `unknown`. It is decided from the document's own text, never from the email's wording, subject, or file name. Until its inspection settles, and when inspection fails, an attachment's class is `unknown`, so pending, deferred, and failed work reads the same everywhere.
  - **Invoice, paid, and payable are separate facts.** The class says only whether a document is an invoice. Whether it is paid, where a PAID mark is evidence of payment and not proof of settlement, and whether it is payable by the mailbox are separate facts the document layer and the payment automations own (Dependencies). Each is `unknown` when uncertain, and none is inferred from another. D-claims reads only the class: a vendor's invoice total is compared whether or not it is paid or payable.
  - **Selection.** An attachment's selected record is its valid `invoice.extract` record with the latest settlement; among equal settlement times, the one whose Connect job id sorts last in byte order. Only selected records are ever used.
  - **Use.** A selected record gives a claim only when all of these hold:
    - its attachment's class is `invoice`;
    - its `invoice_number` is present. The number is the record's own reading of the PDF, with its page and exact text, and it is compared with references under the normalization reference profiles use;
    - the message's authored text never contradicts the record: when it names invoice numbers, the record's number is one of them, and it never names the record's number as a `quote` or `po` reference. The email's words can take a claim away but never make one: the document's own class, not the email, decides that the attachment is an invoice, so a blank email with a generic file name is enough;
    - its `total` is present and re-derives from its exact text with the parsers validation uses, under the record's own number format;
    - its currency was printed on the document (the record's `currency.source` is `printed`), and the total is in that currency;
    - the record's arithmetic is checked, and its total residual is zero or absent.
  - **The claim** is one `amount` claim with role `total`, bound to the record's invoice number. Its evidence is the attachment's file name, then the text its class cites, then the page and exact text of the invoice number and of the total. A record that fails a condition gives no claim and counts toward "Some claims could not be verified". Its other components, its dates, and its line items give no claims: the total is the only value the record's arithmetic corroborates.
  - **Trust (decision D5).** The class, the `total` label, and the choice of which printed number is the invoice number come from the document layer. Its classifier cites the text it relied on and validates the document's context (Dependencies); Invoice Processor admits an amount's role only when the surrounding text proves it, and withholds the amount otherwise. Email Watcher checks what the records let it check (the value against its text, the printed currency, and the arithmetic) and that the email contradicts nothing. Every outcome shows the file name, the class's cited text, and the pages and exact text of the number and the total, so the user can confirm them against the PDF.
  - Record text is untrusted provider output, and renders as text only.
- **Types (closed):**
  - `amount`: value, currency, and a role in `total`, `subtotal`, `tax`, `shipping`, `deposit`, `unit_price`, `other`;
  - `date_commitment`: a `what` in `delivery`, `completion`, `payment_due`, `service_start`, `other`, and a date;
  - `quantity`: an item and a count;
  - `term`: text, displayed only;
  - `reference`: a `(kind, number)` pair, with kind in `invoice`, `quote`, `po`. References are used only as anchors.
- **Keying.** Claims from the authored text, the message's reference profile (below), and attempts are keyed by `(message, attributed vendor, extractor version)`; claims from an invoice record are keyed by `(message, attributed vendor, selected record, record mapping version)`. They, and the discrepancies citing them, exist only while their message is offered under that key (Source above). When the message stops being offered, or its attributed vendor changes, they are deleted ([D-derived](#d-derived-derived-state-and-invalidation)). That covers a vendor's deletion or re-creation, and a message that turns outbound.
- **Attempts.** Each authored-text key has one durable attempt record, with one of three outcomes. An invoice-record key has none, since its claim needs no model call:
  - `succeeded`;
  - `retryable`: the model or its transport was unavailable. Another attempt is made after a backoff deadline, and the message shows "Claims unavailable, will retry";
  - `rejected`: the response failed the schema. It is permanent for that version, and the message shows "Claims unavailable".
- **Validation, deterministic for the same input.** In a schema-valid response, each claim is checked on its own:
  - every quote is a whitespace-normalized substring of the authored text;
  - every structured field is re-derived from its quote in code: money by a parser, dates by the scheduling rules, counts by a number parser next to the item, references by exact substring and kind pattern;
  - each role or `what` other than `other` needs one of its fixed keywords in the quote.

  Failing claims are discarded and counted ("Some claims could not be verified"). Passing claims are stored. Every shown claim therefore has validated evidence.
- **Binding (algorithm in the M4 plan).**
  - A claim's anchor and its canonical item key must be bound to that claim uniquely, from its own evidence. Item keys are canonicalized in code, never taken from the model's choice of substring.
  - Relative and yearless dates resolve against the message's stored date context ([D-body](#d-body-stored-bodies)). Without that context they are rejected.
  - Only claims of their source's current version exist for comparison: the claims extractor version for authored text, and the record mapping version for invoice records. A new version supersedes a message's older claims atomically.
- **A message's reference profile** is computed in code, never from the model's output, whenever the message is offered and its authored text is available, whatever its extraction attempt's outcome: a scan of its authored text with the reference kind patterns that validation uses. For each kind, the profile holds the one reference the scan finds, or marks the kind *several* when the scan finds more than one, counting references by kind and normalized number (the M4 plan names the patterns and the normalization). A kind the scan finds no reference of is absent from the profile.
- **A message's invoice**, which only record pairing reads, is the single `invoice` reference its profile holds. When its authored text names no invoice number, it is the number its invoice attachments carry: when none of its supported attachments has class `unknown`, every attachment of class `invoice` has a selected record carrying a number, and they all carry the same one. Otherwise the message has none: its text marks invoices several, an attachment's class is `unknown`, an invoice attachment has no selected record or no number, or they carry more than one. Attachments of another class never count. An invoice attachment whose record is missing or fails a Use condition still counts, so it can only take a pairing away, never make one. The profile itself stays authored text only, and only selected records count, so a superseded record leaves no number behind.
- **`comparable(a, b)`** holds only if all of these do:
  - same vendor, in the same account and mailbox identity;
  - same type, and same validated key;
  - the anchor condition for where the two claims sit:
    - **in one thread:** a shared bound anchor `(kind, number)`, needed for `amount` roles other than `unit_price` and for `date_commitment` and `quantity`;
    - **in different threads (decision D4):** profile agreement, for every type;
    - **profile agreement:** the two messages' reference profiles have a reference in common and, in every kind both profiles hold, the same single reference. A kind marked several in either profile, while the other profile holds that kind, never agrees. Only the two messages' own profiles count, so claims never pair through a third message;
    - **record pairing**, instead of both, when either claim comes from an invoice record. It treats the two claims alike, so its result never depends on which one is taken first. A claim is *bound to an invoice* when it comes from a record, bound to the record's number, or when it is an authored claim bound to an invoice number that is its message's invoice. Two claims of the same type and key that are bound to the same invoice pair. When the two messages hold no such pair, they pair by profile agreement, with each message's `invoice` kind replaced by its invoice where it has one, and only when the own message of every record-backed side has that record's number as its invoice. A record's number therefore stands in for an invoice its email does not name: two PDFs that carry the same invoice number pair even when neither email names it, and a quote or PO links a PDF invoice to another document only when the vendor's email names that reference;
  - neither claim is a `reference`, a `term`, or `other`;
  - they come from different messages.
- **Outcome.** Every thread that holds one of the claims shows the outcome, with a link to the other claim's message when that message sits in another thread.
  - **Ambiguous:** either side has more than one comparable value. "Several values for <key>; compare manually" is shown, and nothing is flagged.
  - **No shared anchor**, where one is needed: two claims in one thread are shown side by side, unflagged. Claims in different threads that fail the anchor condition are never paired.
  - **Otherwise** a discrepancy, citing both claims' quotes and dates, is flagged when:
    - an amount or quantity differs ("price changed since <date>" for `unit_price`);
    - a later `date_commitment` is after the earlier one ("later than promised").

    A later date that is earlier is shown as "date moved earlier", unflagged.
  - The model only extracts; code decides.

## Thread view (desktop)

- **Vendors view.** It lists vendors, then each vendor's threads ([D-attribution](#d-attribution-a-messages-vendor)), newest activity first, then a thread.
  - The thread shows its messages oldest first, each with its direction, sender, attributed vendor, time, and body state ([D-body](#d-body-stored-bodies)).
  - Bodies render with `textContent` only.
- **A Content Security Policy is required first.** Before any body renders, the webview gets a CSP that forbids inline script and remote loads.
- **Inbox and watchlist copy** says that followed threads and Sent replies to vendors also appear.

## Invariants

- **Claims extraction adds no new destination or data kind.** It sends only the [D-claims](#d-claims-claims-and-comparability) source text to the configured model backend, as per-message analysis already sends the body.
  - Loopback stays on the machine.
  - Gateway mode reaches the operator's configured on-prem gateway (`docs/INFERENCE_GATEWAY_V0.md`).
  - Stored data never leaves the machine otherwise.
- **Mailbox access stays read-only.** IMAP keeps `readonly=True` and `BODY.PEEK`.

## Milestones

Each milestone gets its own `plans/PR-*.md` plan PR, accepted before code. No milestone ships UI that depends on a later one.

- **M1, vendors and thread identity.**
  - [D-vendor](#d-vendor-vendors-and-vendor_ofaddress) records with exact addresses.
  - The address operations and the watchlist link of [D-ops](#d-ops-operation-classes-and-gating).
  - [D-identity](#d-identity-message-identity-direction-and-thread-keys) thread keys on newly captured messages, including the IMAP reply-header fetch, components, and merges.
  - A Vendors list in the desktop.
  - Admission is unchanged.
- **M2, capture:** [D-capture](#d-capture-what-is-stored-and-its-provenance), [D-follow](#d-follow-followed-threads), [D-derived](#d-derived-derived-state-and-invalidation), [D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass), [D-body](#d-body-stored-bodies), and [D-scope](#d-scope-in-scope-messages-and-retention). That includes Sent capture and the purge, with the retention settings copy.
- **M3, the thread view**, with its CSP and the inbox copy.
- **M4, [D-claims](#d-claims-claims-and-comparability).** Deploying the extractor makes coverage stale, so retained messages get claim attempts.
- **M6, invoice claims (decision D5):** the invoice-record source of [D-claims](#d-claims-claims-and-comparability), after M4 and after the invoice-record automation (Dependencies).
- **M5, domains and suggestions:** the domain operations of [D-ops](#d-ops-operation-classes-and-gating), and suggestion candidates under [D-vendor](#d-vendor-vendors-and-vendor_ofaddress).

### Required items carried to milestone plans

Each named plan must include these, with fail-first tests.

- **M2:**
  - Gmail discovery and thread sync page `messages.list` with durable page tokens, bounded to the admitted labels and dates; `threads.get` is never called, since it returns a thread's every message.
  - Every Gmail message gets a bounded `format=metadata` fetch before its scope check, and the body is fetched only after that check.
  - Microsoft discovery reads each admitted folder's delta and applies the dates and the recipient match locally, never using `$search`.
  - IMAP sync searches `HEADER Message-ID`, `In-Reply-To`, and `References` until the component stops growing.
  - IMAP messages retained from before M1 never had their reply headers fetched; M1 lists them. Sync fetches those headers for a listed message whose mailbox identity is known and merges through D-identity. A message whose source is gone, or whose identity is unknown, leaves the list and keeps its own component.
  - Rows stored before locations exist that are one message under [D-identity](#d-identity-message-identity-direction-and-thread-keys), retained or from M1, are coalesced into one message with all their locations.
  - The date context is stored at capture, with a test that changes the zone after storage.
  - The Gmail checkpoint covers both labels, with a test of a poll that stops mid-range and resumes without missing `SENT` events.
  - The recognized HTML quote containers (at least `<blockquote>`, Gmail's quote block, and Outlook's reply header block), each tested.
- **M4:**
  - Per-claim anchor binding, tested on "PO-1 total $100; PO-2 total $200".
  - Canonical item keys, tested on "premium red widget".
  - Atomic supersession by extractor version.
  - Re-extraction months later yields the same dates.
  - Quote segmentation, tested on top-posted, bottom-posted (`>`), inline, and Outlook-header replies.
  - The attempt record's backoff.
  - Comparison across threads ([D-claims](#d-claims-claims-and-comparability), decision D4): a quote in one thread and its invoice in another, joined by a reference both messages name, tested on every provider.
  - Outcomes follow their claims ([D-derived](#d-derived-derived-state-and-invalidation)): tested with either side's claims stored last, and with either side purged or re-attributed.
  - The thread view shows a cross-thread outcome in both threads, each linking to the other message.
- **M6:**
  - Selection, tested with two records of one attachment settled at the same time.
  - Each use condition failing on its own: an attachment whose class is not `invoice`, no invoice number, an email that names only other invoice numbers, an email that names the record's number as a quote or PO, a total that does not re-derive, an inferred currency, unchecked arithmetic, and a non-zero total residual.
  - A message's invoice taken from its selected records when its email names none, tested with one PDF, with two PDFs of different invoices, with two PDFs of which one fails a use condition, with a second invoice PDF whose record carries no invoice number, with a second attachment whose inspection is pending, deferred, or failed, and with an email that names invoices several.
  - Record pairing preferring claims bound to the same invoice, tested in one thread and across threads, between a record and an authored claim and between two records, and with either claim taken first.
  - A newer record, and a new record mapping version, each superseding claims in one transaction.
  - File-name, page, and text evidence shown for every claim from a record, tested on a message with two PDF attachments.

## Operator decisions (accepted 2026-10-05, as recommended)

- **D1:** a followed thread is purged as a unit, by its newest message ([D-scope](#d-scope-in-scope-messages-and-retention)).
- **D2:** no encryption at rest in this arc. Bodies get today's `0600` database in a `0700` directory (`db.py:4502`, `db.py:4826`), plus the secure deletes of D-scope. SQLCipher would be its own arc.
- **D3:** gate on the existing `connect.capability_exchange` ([D-ops](#d-ops-operation-classes-and-gating)).
- **D5 (operator, proposed 2026-10-09, revised twice on 2026-10-10):** an invoice total that is only in an attached PDF becomes a claim when the document itself is classified as an invoice and the vendor's email contradicts nothing ([D-claims](#d-claims-claims-and-comparability)). A blank email with a generic file name is enough: the document layer classifies the PDF from its own text, citing it and validating its context, and Invoice Processor reads its number and total. Whether it is an invoice, whether it is paid, and whether it is payable by the mailbox stay separate, and uncertainty stays unknown. Comparing it with another document is a separate check that still needs a reliable reference: its invoice number, named by another message or carried by another invoice PDF, or a quote or PO number the vendor's email names. The operator chose to trust the classifier's cited, context-checked decision and Invoice Processor's label check and number reading, rather than wait for evidence Email Watcher could verify itself. The accepted risks: an email naming an invoice it says is not the mailbox's can still be compared, and a document the classifier or Invoice Processor misreads is shown as read. Every outcome shows its evidence. Reading the PDF is attachment inspection and extraction through Connect, so it needs Connect and Automations; the comparison itself stays under D3. Email Watcher never reads the PDF's contents itself.
- **D4 (operator, 2026-10-09):** claims are compared across all of a vendor's threads, not only within one ([D-claims](#d-claims-claims-and-comparability)). A quote, invoice, or PO number that both messages name unambiguously, not the thread, decides that two claims concern the same deal. Retention (D1) is unchanged, so only retained threads take part.

## Dependencies

- **Gateway mode needs a registered claims task** (for example `email.vendor_claims.extract` v1) before M4 works there. Today's tasks are `email.analyze` and `email.schedule.extract` (`model.py:433-435`).
- **Attachment inspection and invoice records need one accepted contract before the M6 plan**, modeled on `docs/CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md`. It owns the order of work on attachments:
  - every supported attachment of a message admitted from a watched sender or account is inspected, whatever the email's wording, subject, file name, or analysis category, and no email-category condition can stop inspection from running;
  - a rule's document-class condition ([the Automate rule engine](AUTOMATE_RULE_ENGINE_CONTRACT.md) gains one) is evaluated only once that attachment's inspection has settled, never at analysis time;
  - `invoice.extract` runs on attachments of class `invoice` only;
  - inspection results are cached by the document's content hash with the classifier's identity and version, so a new classifier version inspects again;
  - work that a budget defers waits in a durable queue and is never skipped, and until it settles the attachment's class is `unknown` ({CL}); unreadable and uncertain documents stay visible for review.

  For the records themselves, it must validate the provider-owned `invoice.extract` 1.0 record (`record_version` 1.1) strictly and fail closed. It must store one record per `(message, part, Connect job)`, with its settlement time, canonical JSON, and digest and no attachment bytes, exactly once per settled job, inside the transaction that settles it, and delete it under [D-scope](#d-scope-in-scope-messages-and-retention)'s purge. The generic `connect.invoke` dispatcher stays the only path to the provider.
- **`invoice.extract` 1.0 reports no PO or quote number** (invoice-processor `src/invoice_processor/schema.py`, `InvoiceRecord`, at `8e953b2`). A PDF invoice therefore pairs with another document only through its invoice number, or through a quote or PO its email names in its own words ([D-claims](#d-claims-claims-and-comparability)); until then, a PDF invoice whose email names neither is compared only with documents bound to the same invoice number: an email that names it, or another PDF that carries it. Pairing through a quote or PO printed only in the PDF needs a later `invoice.extract` version that reports each one with its page and exact text, and a contract amendment that adopts it.
- **`invoice.extract` reads every input as an invoice to pay.** Its record has no bill-to, direction, or document type, so a vendor's quote, statement, or credit note looks like an invoice. Classification therefore comes first, from a document classifier in the document layer, which needs its own accepted contract before the M6 plan so that other apps can reuse it. That contract must:
  - classify from the document's own text, native or through `document.ocr`, never from the email's wording or the file name;
  - decide clear cases by rule and ambiguous ones with a local-model check, and in both cite the text it relied on, which code verifies is in the document;
  - validate the document's context, not only the cited text: conflicting titles in one document (QUOTE and INVOICE) give `unknown`, and so does a file that holds several documents;
  - report payment-status evidence separately from the class (a PAID mark is evidence, not proof of settlement), with `unknown` when uncertain;
  - leave "payable by the mailbox" to a later contract, since it needs the bill-to party, which `invoice.extract` 1.0 does not report, and the message's direction ([D-identity](#d-identity-message-identity-direction-and-thread-keys)).
- **`invoice.extract` 1.0 does not say which printed label proved an amount's role.** Its `exact_text` is the value alone, and its role admission (`_withhold_unproven_money_roles`, `src/invoice_processor/pipeline.py` at `8e953b2`) stays inside the provider. Claims beyond the arithmetic-checked total need a later version that reports that evidence.
- **Gmail restricted-scope distribution (#74).** Storing bodies locally should be reviewed against it before a public release.

## Explicit non-scope

- Sending, replying, moving, or labeling mail.
- Any cloud model or cloud storage.
- More than one active mailbox at a time (#149).
- Thread-level AI summaries.
- Attachment content in claims, other than the attachment classes and invoice records of decision D5.
- Changing today's per-message analysis, notifications, scheduling, or Connect behavior.

## Acceptance evidence

Each milestone plan names its fail-first tests. The arc-level scenarios are:

- **The core scenario**, on each provider: a fixture thread with an inbound quote, an outbound reply, and an inbound invoice that differs, giving one discrepancy with both quotes.
- **The invoice-PDF scenario**, on each provider: "Quote Q-512, total $1,200" in one thread, and in another an email "Invoice 9087 for quote Q-512 attached" whose PDF record has invoice 9087 and total $1,450. Together they give one discrepancy, citing the quote's text and the PDF's page and exact text.
- **The attachment-only invoice scenario**, on each provider: a blank-body email whose only attachment, `scan_0012.pdf`, holds invoice 9087 for $1,450 gives an invoice claim bound to 9087, shown with its evidence. Agreement matching stays a separate check: with "Quote Q-512 total $1,200" in another thread and no reference linking them, nothing is compared.
- **The cross-thread scenario**, on each provider: the same quote and invoice in two different threads of one vendor, joined by the quote number, giving one discrepancy that both threads show, each linking to the other.
- **No trace:** a non-vendor message leaves no row.
- **No new bodies:** a watched non-vendor message, or a label-only message, outside any followed thread stores no body.
- **Attribution:** an outbound message to vendor A (`To`) and vendor B (`Cc`) is A's, and the thread is listed under B as linked.
- **Provenance:**
  - a message captured by `gmail_user_label`, whose sender later becomes a vendor address, makes its thread followed and keeps its provenance;
  - an unwatched vendor address is captured as `vendor_address`, never as `exact_sender`.
- **Gmail direction:** a Gmail message with both `INBOX` and `SENT` gets the same direction and kind on every path.
- **IMAP identity:**
  - colliding Inbox and Sent UIDs give two messages;
  - a moved message with a `Message-ID` is captured once;
  - reverse-order arrival (C, B, A) gives one thread;
  - a bridging message merges two followed components, owned by different vendors, without skipping either one's messages. The owner is the same for every arrival order.
- **Reconcile:**
  - many polls with no configuration change never make coverage stale;
  - a vendor message captured by polling, in a conversation that was not followed, syncs that conversation's earlier messages;
  - a Connect lapse, then reactivation, recovers in-cutoff mail from the lapse and conversations started during it;
  - raising `retention_days` from 30 to 180 fetches the 31-180-day messages of followed threads;
  - adding a vendor pulls in retained mail, including a standalone message that was never captured;
  - a thread stays followed while it is active, after its first vendor message passes the cutoff;
  - an ungated `exact_sender` capture while Connect is inactive makes its thread followed, with no gated capture and no body;
  - deploying M4 extracts claims for M2 and M3 messages.
- **Derived state:**
  - removing a vendor's only address unfollows its threads and deletes their bodies, claims, discrepancies, and attempts, while the messages keep their summaries;
  - purging a followed thread's last message drops its cache row and watermark;
  - a Sent location recorded after the Inbox copy makes the message outbound, re-attributes it, and deletes its claims;
  - connecting a mailbox whose address is a vendor address leaves that address's messages without a vendor;
  - none of these needs a reconcile pass to take effect.
- **Scope:**
  - a Microsoft deleted-items message in a followed conversation is never fetched;
  - an archived, label-captured Gmail message shows "outside the admitted folders".
- **Bodies:** a body longer than the storage cap shows its partial marker.
- **Claims:**
  - a reply quoting an earlier total yields no claim from the quote, for both `>` quoting and an HTML blockquote;
  - a bottom-posted reply's new total, below the quote, is extracted;
  - deleting a vendor and re-creating it with the same addresses re-extracts its claims;
  - two totals for one PO in one message are ambiguous;
  - after PO-1 and PO-2 totals, a PO-2 invoice compares only with PO-2;
  - two orders without a shared anchor stay unflagged;
  - across threads:
    - "Quote Q-512, total $1,200" in one thread and "Invoice 9087 for quote Q-512, total $1,450" in another give one discrepancy, whichever message is extracted last;
    - the same quote number from a different vendor is never compared;
    - two threads of one vendor whose messages name no reference in common are never paired;
    - an invoice that names two quotes is compared with neither quote, whatever the model returns, including a total whose own evidence names only one of them;
    - "Invoice I-1 for PO-1, quote Q-1" and "Invoice I-2 for PO-2, quote Q-1" each pair with the quote Q-1 message, and never with each other;
    - a message that names "Q-512" twice still holds Q-512 in its profile;
    - "Invoice 9087, balance $1,450" pairs with "Invoice 9087 for quote Q-512, total $1,450" but not with the quote, because claims never pair through a third message;
    - a `unit_price` in another thread is compared only when the two profiles share a reference;
    - purging the quote's thread removes the discrepancy from the invoice's thread;
  - from invoice PDFs (each PDF is classified `invoice` unless the case says otherwise):
    - a PDF's total never compares with its own email's text, since both come from one message;
    - the invoice-PDF scenario's two emails in one thread also give one discrepancy, through profile agreement;
    - an email "I've attached our latest invoice" whose PDF record has invoice 9087 gives a claim bound to 9087, showing the page and exact text of the number and the total, and never compared with "Quote Q-512 total $1,200", in its own thread or another;
    - that claim pairs with "Invoice 9087 total $1,500" in another thread, giving one discrepancy, because that email names the invoice;
    - "I've attached our latest invoice for quote Q-512" with a PDF record for invoice 9087 and total $1,450, against "Quote Q-512 total $1,200" in another thread, gives one discrepancy;
    - "Invoices attached for quote Q-512" with PDF records for invoices 9087 and 9088 gives each its own claim, and neither pairs with the quote;
    - the same email whose 9088 record has an inferred currency gives 9087 a claim and 9088 none, and 9087 still never pairs with the quote, because 9088's record says the message carries two invoices;
    - "I've attached our latest invoice for quote Q-512" with a record for invoice 9087 and a second invoice PDF whose record carries no invoice number gives 9087 a claim that never pairs with the quote;
    - the same email with a second attachment whose inspection is pending, or deferred by the budget, gives 9087 a claim that does not pair with the quote while the other is unsettled; once it settles as a quote, 9087 pairs with the quote, and if it settles as `unknown` or as an invoice for another number, it never does;
    - two emails "I've attached our latest invoice", each with a PDF record for invoice 9087, with totals $1,450 and $1,500, give one discrepancy, though neither email names the number;
    - a one-PDF email for invoice I-1 "for quote Q-1" and an email "Invoices attached for quote Q-1" with records for invoices I-2 and I-3 never pair, whichever claim is taken first;
    - "We'll send the invoice next week; our quote is attached" with a PDF classified as a quote gives no claim, and so does "Invoice attached" with a PDF classified as a quote: the email's words never decide the class;
    - a PDF titled both QUOTE and INVOICE, and a PDF that holds an invoice and a quote, are classified `unknown`, give no claim, are shown for review, and keep their message without an invoice;
    - an invoice PDF stamped PAID is still classified `invoice` and gives its claim; the stamp is payment-status evidence only, which D-claims never reads;
    - an email "Invoice for quote Q-512 attached" whose PDF record reads Q-512 as the invoice number gives no claim;
    - a vendor's quote PDF, classified as a quote and sent as "Quote Q-512 attached", gives no claim;
    - an email naming invoice 9087 with a PDF record for invoice 9088 gives no claim;
    - a total that re-derives to another value, an inferred currency, unchecked arithmetic, or a non-zero total residual gives no claim, counted as unverified;
    - an email "Quote Q-512 total $100; Invoice 9087 total $120" and a PDF record for invoice 9087 compare the PDF's total with the $120 invoice total only;
    - a PDF record for invoice I-1 and an email "Invoice I-1 total $100; Invoice I-2 total $200; quote Q-1" are never paired, because that email marks invoices several;
    - two emails that each name several invoices and the same quote never pair their PDFs through the quote;
    - a PDF record for invoice I-2 in an email "Invoice I-1 for quote Q-1 attached; invoice I-2 attached for reference" is never paired with "Quote Q-1 total $100";
    - a PDF's total pairs through its email's quote number even when the email's own extraction attempt was rejected;
    - a PDF's subtotal, tax, due date, and line items give no claims;
    - a newer record for the same attachment replaces the older record's claim;
    - purging the message deletes its invoice records;
    - with Automations lapsed, no new record is requested, and claims from stored records stay readable;
  - a delivery date moved earlier is not flagged;
  - an unavailable model leaves a retryable attempt that succeeds later;
  - a message longer than `body_char_limit` is extracted from its bounded slice.
- **Gating:**
  - with an expired entitlement, reading and removal work while adds and sync are refused;
  - removal controls stay visible.
- **Vendor operations:**
  - `watchlist.remove` of a vendor address returns `conflict`;
  - removing a domain stops new domain matches;
  - deleting a vendor ends following, and offers to stop watching its addresses;
  - the mailbox owner is never suggested, including on a self-sent inbound copy.
- **Scopes:** `Mail.Read` and `gmail.readonly` remain the only scopes.

## Revision log

- 2026-10-05: proposed, then accepted by the operator with D1-D3 as recommended.
- 2026-10-05: five Codex review rounds (12, 7, 7, 9, and 7 findings) were each answered with local edits.
  - Later rounds were mostly contradictions those edits created, because each rule was restated in up to 14 places.
- 2026-10-05: restructured into definitions-first form, at the operator's direction ("share seams, stop symptom patching"), with `tests/test_thread_view_contract.py` enforcing it.
  - **One deliberate simplification:** capture and following come from vendor records, not the watchlist.
- 2026-10-05: the review of the restructure found twelve definition gaps. Each was fixed inside its owner:
  - D-body: bodies only for followed threads; a partial-body state;
  - D-reconcile: the configuration, not the moving cutoff, is compared; watermarks reset when retention grows;
  - D-claims: model input bounded by `body_char_limit`; only later dates are flagged; durable claim attempts;
  - D-vendor: an exact public-provider list; mailbox identities excluded from suggestions;
  - D-ops: domain removal;
  - D-attribution: one vendor per message, with linked thread listing.
- The same review showed that rules still sat outside their definitions: the operation list, the suggestion rule, the failure cases, and an invariant. Those were moved into their owners. The seam check now also catches verbatim copies of any definition line.
- 2026-10-05: the next review found six gaps. Two of them, a follow created by polling never syncing and a merge owner depending on arrival order, came from one root: recorded follow history. D-follow now derives follow state and owner from stored messages and configuration, which also removed the owner special cases from merges and deletion. The others:
  - D-capture: `vendor_address`, so `exact_sender` stays truthful;
  - D-reconcile: a Gmail checkpoint covers both labels;
  - D-body: HTML quote boundaries survive;
  - the seam check compares definitions against each other too.
- 2026-10-05: the next review found five gaps.
  - Three came from last round's D-follow predicate, which mixed fetch eligibility with classification. D-follow now classifies stored messages, D-ops no longer lists following as an operation, and discovery is defined directly.
  - D-claims keys claims and attempts by attributed vendor, and keeps authored text below quotes.
- 2026-10-05: merged on green at the operator's direction. The final review's six findings are tracked as amendments A-D in #207.
- 2026-10-05: amendment B (#207), in the M1 plan PR. D-identity defines one canonical order that every tie-break uses, including the merge survivor without a `Message-ID`, and direction is derived from the recorded locations.
- 2026-10-05: the M1 plan review found two definition flaws, fixed in their owners:
  - D-identity: amendment B claimed the merge survivor never depends on arrival order, which a UUIDv4 key cannot satisfy. Component membership is order-independent and the key is an opaque handle. Every captured message now has a thread key, and merges re-key earlier aliases.
  - D-ops: deleting a vendor claimed one transaction across the watchlist file and the database. Operations that change both now write the watchlist first, so a retry completes them.
  - A third finding, that a location added later changes direction without recomputing follow state or claims, is an input to amendment A (#207).
- 2026-10-05: the second M1 plan review found that a logical message with several locations had no single source identity to order by; it sorts by the smallest. It also added two M2 items: fetch the reply headers of IMAP messages retained from before M1, and coalesce rows that are one message.
- 2026-10-05: amendment C (#207), in the M1 plan PR. `vendor_of` excludes the mailbox's verified identities before either lookup, which also covers an address whose mailbox is connected after it became a vendor address.
- 2026-10-05: the fourth M1 plan review found that outbound attribution looked up recipients' addresses and domains directly instead of through `vendor_of`, so amendment C's exclusion did not reach outbound mail. D-attribution now judges recipients only through `vendor_of`.
- 2026-10-05: the fifth M1 plan review found that D-attribution asked how `vendor_of` matched, which `vendor_of` did not say. `vendor_of` now returns its match, exact or domain, and outbound attribution and D-capture's kinds both read it. A finding that a newly verified identity leaves follow state and claims stale is an input to amendment A (#207), with the direction finding above.
- 2026-10-06: amendment A (#207). Derived state was invalidated by event lists in two places, D-follow's recompute triggers and D-reconcile's stale triggers, and every new input added an event they missed: a removal, a purge, a direction change, and an identity becoming verified. A new definition, D-derived, owns one rule over the inputs instead: any change to the stored messages, their locations, the vendor records, or the verified identities recomputes what depends on them, in the same transaction. Each definition keeps its own invariant (D-follow's empty threads, D-body's bodies, D-claims' keys), and coverage staleness became a comparison with the coverage record, so it needs no triggers.
- 2026-10-06: the second M2 plan review found three findings of one class: comparing the current state with the coverage record cannot see a change that reverts (an entitlement lapse that ends, an address removed and re-added, a thread unfollowed and refollowed). D-reconcile now keeps a coverage generation that every input change bumps, so a change and its reversal both count, and a watermark exists only while its thread is followed. D-scope keeps a followed thread until the account's coverage is current, so a lapse cannot purge a thread whose newer replies are unfetched. D-derived names a missing-body reason as derived state.
- 2026-10-06: the third M2 plan review found two more gaps in D-reconcile's staleness: a message polled into a followed thread left derived work pending with no signal, and a `retention_days` edit in the configuration file bypassed the one bump path. Pending derived work now makes coverage stale, and every check records the inputs it can observe and counts a difference as a change, so the check is the one bump owner for them.
- 2026-10-06: the fifth M2 plan review: a verified identity added or changed is a reconcile input too, so it bumps the coverage generation (D-reconcile).
- 2026-10-06: the sixth M2 plan review: a retryable claim attempt whose backoff deadline has passed is pending derived work, so it makes coverage stale (D-reconcile).
- 2026-10-07: the gap-stamps and M2.1 code reviews found two concepts without an owner. D-reconcile used "unconfirmed location" with no definition, so each rewording of its gap rule restated which messages the stages reach; D-identity now defines recorded locations and their confirmation, and the gap rule cites the stages' fetches. The purge, analysis, and Connect each compared a logical message's age with the cutoff on their own; D-scope now defines a logical message's received time once, as its newest copy's, for every comparison, and keeps what may be fetched separate from what the purge keeps.
- 2026-10-09: decision D4, from the operator: the main use is an agreement made in one thread and invoiced in another, which "same thread" excluded. D-claims now compares claims across a vendor's threads in one account. It adds linked references (one message naming exactly one reference of each of two kinds) and a claim's anchors (its bound reference plus one link). Binding prefers the most specific kind. Anchor-free `unit_price` comparison stays within one thread, and outcomes show in every thread that holds a side. Retention (D1) and the gate (D3) are unchanged. M4 carries the new required items.
- 2026-10-09: the Codex review of #224 found three gaps in D4's first draft, each fixed in its owner. D-derived now lists the stored claims as an input of the comparison outcomes, so storing an extraction recomputes them, even one that only creates a link; this replaces an event list the draft had put in the M4 items. D-claims adds a shared anchor that must agree in every kind both claims carry, so invoices for different POs of one quote no longer pair through the quote. Links count distinct references, not claim objects.
- 2026-10-09: the second Codex review of #224 found that link counts still trusted the model to return every reference, so an omitted quote could create a false link. Linked references now come from a deterministic scan of the authored text with validation's kind patterns. A model omission can only leave a link out, never add one.
- 2026-10-09: the third Codex review of #224 found three more gaps, all in the link graph: links had no lifecycle of their own, unrelated one-link neighbors could veto a valid pairing, and per-claim binding bypassed the authored-text ambiguity check. The graph is removed rather than patched. Pairing across threads now reads only the two messages' own reference profiles, computed in code from the authored text and keyed and deleted with the claims, and never goes through a third message. Per-claim binding is again only the same-thread anchor.
- 2026-10-09: decision D5, proposed by the operator: invoice figures that are only in a PDF were outside D-claims, which made the main invoice case mostly unreachable. Invoice records from the user's own `invoice.extract` rule are now a second claim source. They are mapped in code, anchored by their invoice number, re-derived from the provider's exact text, and fed into the message's reference profile. Producing records needs Automations; deriving claims from them is local. The same-thread anchor condition also accepts profile agreement, so a quote and its PDF invoice in one thread pair exactly as they would across threads. Milestone M6 and the invoice-record automation contract are new dependencies. `invoice.extract` 1.0's lack of PO and quote numbers is recorded as a limit.
- 2026-10-09: the first Codex review of #228 found eight gaps in D5's draft, with four roots, each fixed at its owner.
  - Trust: a vendor PDF was assumed to be a payable invoice with correctly labeled values. A record is now used only when the vendor's email names its invoice number, which also settles a missing number. Because the 1.0 record cannot prove other labels, only the arithmetic-checked total becomes a claim.
  - Selection: one deterministic selected record per attachment. Record claims carry a mapping version, and records purge with their message.
  - Profiles are again authored text only, so a superseded record cannot leave a stale invoice number behind.
  - Profile agreement within one thread now applies only when an invoice record is involved, so authored claims keep their per-claim anchors.
- 2026-10-09: the second Codex review of #228 found that the email-confirmed number, the label, and the currency still could not be proven from a 1.0 record. The operator chose to trust Invoice Processor's own label check now (decision D5), with its accepted risk recorded. The currency must be printed, which the record states. Record pairing, one rule for both thread modes, prefers the other message's claim bound to the record's invoice number before profile agreement. The dependency now links D-scope's purge instead of restating it.
- 2026-10-09: the third Codex review of #228 found that a reference profile treated a kind named several times like a kind never named, so profile agreement ignored it, and an ambiguous invoice kind could vanish during record pairing's fallback. D-claims' profile now marks such a kind *several*, and profile agreement fails when either profile marks a kind several while the other holds it. That also makes D4's authored pairings stricter in the same way. Per-claim binding inside a message that names several invoices still never decides, as D4 established.
- 2026-10-09: the fourth Codex review of #228. Record pairing's profile fallback now requires the record's own message to name the record's invoice number as its only invoice. A profile no longer waits for extraction to store claims. A record claim's evidence names its attachment. Attempt records belong to authored-text keys only.
- 2026-10-10: decision D5 revised by the operator: "email body only isn't sufficient", and "I've attached our latest invoice" should work, with the invoice number taken from the PDF, while matching it to a quote or agreement still needs reliable references. The gate had been the email naming the invoice number, which excluded the most common invoice email. The number now comes from the record, and the email says the message carries an invoice and contradicts the record nowhere; that keeps a quote PDF sent as a quote from becoming an invoice claim, since Invoice Processor reads every PDF as an invoice. Record pairing reads one new definition, a message's invoice, so a record's number stands in for an invoice its email does not name while profiles stay authored text only. A quote or PO printed only in the PDF waits for a later `invoice.extract` version.
- 2026-10-10: the oversight review of #231 found that a message's invoice read only its record claims, so a second invoice whose record failed a Use condition vanished, and the first invoice paired with a quote the vendor had split across both. It now reads the message's selected records, whether or not they give claims, and a record without an invoice number leaves the message without one; a failing record can only take a pairing away. The word cue's accepted risk is now an explicit scenario.
- 2026-10-10: the Codex review of #231 at bbc9fdb found that record pairing's guard named only "the record", so with records on both sides the result depended on which claim was taken first, and that the contract never said whether two PDFs with the same number pair. Record pairing is now stated over both claims alike: a claim is bound to an invoice when it comes from a record or when its message's invoice is its bound number, claims bound to the same invoice pair, and the profile fallback requires every record-backed side's message to have its record's number as its invoice. Two PDFs that carry the same invoice number pair.
- 2026-10-10: decision D5 revised again by the operator: a blank email with a generic PDF file name must work, so the email's wording cannot decide that an attachment is an invoice. The document layer classifies each supported attachment from its own text, cites the text it relied on, and validates the document's context (conflicting titles and mixed documents give `unknown`). D-claims reads that class, and a pending, deferred, or failed inspection reads as `unknown` everywhere, which also counts an unsettled attachment toward a message's invoice instead of ignoring it. Invoice, paid, and payable are kept separate, and agreement matching stays a separate check. One dependency contract owns the order of attachment work, its cache key, and its deferral queue; the classifier needs its own contract in the document layer.
