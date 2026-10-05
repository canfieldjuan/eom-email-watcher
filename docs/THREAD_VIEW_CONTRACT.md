# Thread View and Vendor Claims Contract

Status: accepted by the operator on 2026-10-05, with D1-D3 as recommended. It was restructured on 2026-10-05 into a definitions-first form (see the revision log). Each milestone still needs its own accepted plan before code.

**How to read and change this contract.**

- Every rule is stated exactly once, in [Definitions](#definitions). Every other section names the definition it uses and restates nothing.
- `tests/test_thread_view_contract.py` fails if a rule's key phrase appears outside its own definition. A fix therefore changes the one definition, not a copy of it.
- Milestone plans own the exact provider calls, schema, and tests, under these definitions. A review finding about one milestone's mechanics belongs on that milestone's plan PR. This contract changes only when a definition is wrong.

## Why this arc exists

The operator wants Email Watcher users to "keep vendors honest". A user should see a whole conversation with a vendor, including what was quoted, promised, or agreed earlier and their own replies. They should be told when a later message or invoice departs from an earlier claim.

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
- **Public domains are refused.** A domain on the public-provider list is refused with a stated reason. The list is one closed, tested constant: gmail.com, googlemail.com, yahoo.com, outlook.com, hotmail.com, live.com, icloud.com, me.com, aol.com, proton.me, protonmail.com, gmx.com, and similar.
- **`vendor_of(address)`** returns:
  - the vendor that owns the exact address;
  - else the vendor that owns the address's domain;
  - else none.
- **The mailbox's own verified identities never have a vendor.** These are `mail_accounts.address` and the session's authenticated address.

### D-attribution: a message's vendor

- **Inbound:** `vendor_of(From)`.
- **Outbound:**
  - the vendor of the first `To`, then `Cc`, recipient, in stored header order, whose exact address has a vendor;
  - else the first such recipient whose domain has a vendor;
  - else none.
- **Claims and suggestions.** A message's claims belong to its vendor. A thread may contain messages of several vendors; each keeps its own vendor label.

### D-scope: in-scope messages and retention

- **Folders.** A message is in scope only while it is in an admitted folder:
  - Gmail: the `INBOX` or `SENT` label;
  - Microsoft 365: the Inbox or Sent Items folder;
  - IMAP: `INBOX`, or the folder with the RFC 6154 `\Sent` attribute (falling back to a configured name).
  - No other folder is ever fetched, including archive, deleted items, drafts, and clutter.
- **The retention cutoff** is `now - retention_days`.
  - Only messages received at or after the cutoff are fetched or captured.
  - Mail that aged past the cutoff before it was captured is never recovered, and its thread shows "history partial".
- **Purge.**
  - A message outside a followed thread is purged once it is older than the cutoff, as today.
  - A followed thread is purged as one unit once its newest message is older than the cutoff (decision D1).
  - Purge takes bodies, claims, and discrepancies with their message. Deletes run with `PRAGMA secure_delete = ON`.

### D-capture: what is stored, and its provenance

- **Today's admission is unchanged:**
  - `exact_sender`: `From` is a watched address;
  - `gmail_user_label`: a selected Gmail label.
- **With the paid entitlement active ([D-ops](#d-ops-operation-classes-and-gating)), an in-scope message ([D-scope](#d-scope-in-scope-messages-and-retention)) is also captured when:**
  - it is inbound and its vendor ([D-attribution](#d-attribution-a-messages-vendor)) comes from an exact address (`exact_sender`) or a domain (`vendor_domain`);
  - it is outbound with a vendor (`sent_to_vendor`);
  - or it belongs to a followed thread (`thread_follow`, [D-follow](#d-follow-followed-threads)).
- **Anything else leaves no trace.**
- **Admission kind.** When a message matches several rules, its kind is the first match in `exact_sender`, `vendor_domain`, `sent_to_vendor`, `thread_follow`, `gmail_user_label`.
  - The kind is computed from message metadata and the current configuration, never from the discovery path.
  - It is stored once, as immutable provenance (`db.py:3645-3671`), and never rewritten.
  - `messages.admission_kind` stays a closed set (`db.py:3606`), widened by migration.

### D-follow: followed threads

- **A thread becomes followed** when one of its in-scope messages has a vendor ([D-attribution](#d-attribution-a-messages-vendor)) under the current configuration.
  - It is evaluated when a message is captured, and again by [D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass). Stored provenance is never consulted.
  - Following requires the entitlement ([D-ops](#d-ops-operation-classes-and-gating)).
  - A thread is followed at most once (a unique row per thread key), under the operation lock (`engine_api.py:2192-2199`).
- **Owner.** The owner is the vendor of the message that started the follow, recorded with it.
  - It never changes, except through a merge ([D-identity](#d-identity-message-identity-direction-and-thread-keys)).
  - Suggestions go to the owner.
- **A thread stops being followed only when its owner is deleted** ([D-ops](#d-ops-operation-classes-and-gating)).

### D-identity: message identity, direction, and thread keys

- **Source identity.**
  - Gmail message ids are mailbox-wide.
  - Microsoft ids are immutable ids (`Prefer: IdType="ImmutableId"`) and survive moves.
  - IMAP ids are `mailbox:UIDVALIDITY:UID` (`imap.py:310-315`), with a folder token added for the Sent folder, so the unique source key (`db.py:3335-3336`) separates folders.
- **Logical identity.** A message with a `Message-ID` also has the logical identity `(provider, account, mailbox identity, Message-ID)`.
  - A second location of an already-captured logical identity is recorded, not captured again.
  - Without a `Message-ID`, a moved IMAP message can be captured twice; this is best-effort.
- **Direction** is `outbound` when the message carries Gmail's `SENT` label (even with `INBOX`), or when it was first found in the Sent folder; otherwise `inbound`. It is decided once, at capture.
- **Thread key:**
  - Gmail uses `threadId`, and Microsoft 365 uses `conversationId`.
  - IMAP uses components: messages whose id sets overlap form one component, identified by a UUIDv4 key. A message's id set is its own `Message-ID`, `In-Reply-To`, and a bounded `References` list.
  - The result does not depend on arrival order. A message that touches no component forms its own.
- **IMAP merges.** A message that bridges several components merges them into the earliest-created one, in one transaction:
  - every row naming a merged key is re-keyed, and aliases are recorded;
  - if any component was followed, the survivor is followed. Its owner is the survivor's owner if the survivor was followed, else the merged component's owner;
  - the survivor's sync progress resets to a full sync, which marks coverage stale ([D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass)).

### D-reconcile: coverage and the reconcile pass

- **The coverage record.** Each account keeps a durable coverage record:
  - the scope it has reconciled: the vendor set, the retention cutoff ([D-scope](#d-scope-in-scope-messages-and-retention)), the entitlement state, and the claims extractor version;
  - a `synced_through` watermark per followed thread.
- **Any change to that scope marks coverage stale.** So does a provider cursor's expiry or recovery, or an IMAP merge.
- **Stale coverage triggers a reconcile pass.** It is bounded per check, resumable from durable progress, and the only writer of coverage and sync progress. It runs three stages, in order:
  - **(a) Discovery:** in-scope messages that [D-follow](#d-follow-followed-threads) says should start a follow, and that are not in a followed thread yet, are captured and their threads followed.
  - **(b) Thread sync:** for each followed thread, its in-scope messages are fetched from its watermark (from the cutoff after a reset) and captured under [D-capture](#d-capture-what-is-stored-and-its-provenance).
  - **(c) Derived work:**
    - bodies ([D-body](#d-body-stored-bodies)) for captured messages lacking one;
    - claims ([D-claims](#d-claims-claims-and-comparability)) for messages lacking the current extractor version.
- **Normal polling keeps coverage current between changes.** It reads each folder with its own cursor: the Gmail history id is mailbox-wide; Microsoft Inbox and Sent Items have separate delta links; IMAP `INBOX` and Sent have separate `UIDVALIDITY` and UID cursors. One folder's cursor never moves another's.

### D-body: stored bodies

- **What is stored.** Every message captured under [D-capture](#d-capture-what-is-stored-and-its-provenance) from M2 on stores:
  - its normalized text, from `bounded_body_text` with a named storage cap larger than `body_char_limit`;
  - the pre-cut length;
  - the date context: received time and the configured time zone, recorded at capture.
- **Raw HTML is never stored**, and bodies never render as markup.
- **A message without a stored body shows exactly one state:**
  - "Body not stored (source no longer available)";
  - "Body not stored (outside the admitted folders)";
  - "Body not stored yet", pending [D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass) stage (c).

### D-ops: operation classes and gating

- **Every operation belongs to exactly one class:**
  - **read:** listing vendors, threads, messages, bodies, claims, and discrepancies. Never gated, and never calls a provider or model.
  - **gated:** adding vendor data (vendors, addresses, domains, accepted suggestions), capture under [D-capture](#d-capture-what-is-stored-and-its-provenance) beyond today's admission, following, reconcile, and extraction. All require the paid entitlement `connect.capability_exchange` (decision D3, `require_connect_entitlement`).
  - **removal:** removing an address from a vendor, deleting a vendor, and dismissing a suggestion. Never gated.
- **The desktop shows controls by class.**
  - Gated controls are hidden while the entitlement is inactive, under a "Connect required to update" notice.
  - Read and removal controls always show.
- **While the entitlement is inactive:**
  - today's admission and inbox continue, and cursors advance;
  - reactivation is a scope change ([D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass)).
- **Removing an address** from a vendor records a dismissal for that `(vendor, address)` pair, so it is never suggested back.
- **Deleting a vendor**, in one transaction:
  - removes its addresses, domains, dismissals, claims, discrepancies, and the follow rows of the threads it owns, which ends [D-follow](#d-follow-followed-threads) for them;
  - optionally removes its addresses from the watchlist ("Also stop watching these addresses", off by default).
  - Addresses left watched keep today's `exact_sender` admission.

### D-claims: claims and comparability

- **Source.** Each inbound message with a vendor, in a followed thread, is offered to the extraction task. Only its *authored text* is used: `current_message_text` from `_split_quoted_history` (`model.py:288`), minus lines that start with `>`.
  - Subjects and quoted history are never evidence.
- **Types (closed):**
  - `amount`: value, currency, and a role in `total`, `subtotal`, `tax`, `shipping`, `deposit`, `unit_price`, `other`;
  - `date_commitment`: a `what` in `delivery`, `completion`, `payment_due`, `service_start`, `other`, and a date;
  - `quantity`: an item and a count;
  - `term`: text, displayed only;
  - `reference`: a `(kind, number)` pair, with kind in `invoice`, `quote`, `po`. References are used only as anchors.
- **Validation, deterministic for the same input:**
  - A response that fails the schema is rejected whole: "Claims unavailable", and nothing is stored.
  - In a valid response, each claim is checked on its own:
    - every quote is a whitespace-normalized substring of the authored text;
    - every structured field is re-derived from its quote in code: money by a parser, dates by the scheduling rules, counts by a number parser next to the item, references by exact substring and kind pattern;
    - each role or `what` other than `other` needs one of its fixed keywords in the quote.
  - Failing claims are discarded and counted ("Some claims could not be verified"). Passing claims are stored.
- **Binding (algorithm in the M4 plan).**
  - A claim's anchor and its canonical item key must be bound to that claim uniquely, from its own evidence. Item keys are canonicalized in code, never taken from the model's choice of substring.
  - Relative and yearless dates resolve against the message's stored date context ([D-body](#d-body-stored-bodies)). Without that context they are rejected.
  - Only claims of the current extractor version exist for comparison. A new version supersedes a message's older claims atomically.
- **`comparable(a, b)`** holds only if all of these do:
  - same thread, and same vendor;
  - same type, and same validated key;
  - for `amount` roles other than `unit_price`, and for `date_commitment` and `quantity`: a shared bound anchor `(kind, number)`;
  - neither claim is a `reference`, a `term`, or `other`;
  - they come from different messages.
- **Outcome:**
  - **Ambiguous:** either side has more than one comparable value. The thread shows "Several values for <key>; compare manually" and flags nothing.
  - **No shared anchor**, where one is needed: the claims are shown side by side, unflagged.
  - **Otherwise** a discrepancy is flagged when the re-derived values differ, with both quotes and dates:
    - "later than promised" for dates;
    - "price changed since <date>" for `unit_price`.
  - The model only extracts; code decides.

## Behavior

### Vendors (engine and desktop)

- **The operations, by class ([D-ops](#d-ops-operation-classes-and-gating)):**
  - `vendors.list` (read);
  - `vendors.create`, `vendors.rename`, `vendors.addresses.add`, and `vendors.domains.add` (gated);
  - `vendors.addresses.remove` and `vendors.delete` (removal).
- **Adding an address** also adds it to the watchlist, in the existing watchlist mutation (`engine_api.py:5506`). The inbox's sender navigation therefore shows vendor mail.
- **`watchlist.remove`** of an address that belongs to a vendor returns `conflict`, naming the vendor. A hand-edited config can still drop one; `vendors.list` then shows `watched: false` with a "Watch again" action. Capture and following come from [D-capture](#d-capture-what-is-stored-and-its-provenance) and [D-follow](#d-follow-followed-threads), not from the watchlist.
- **Suggestions.** When a followed thread has an inbound message whose sender has no vendor, its owner shows "Add <address> to <vendor>?".
  - A dismissed pair never returns.
  - Accepting a suggestion is a gated add.
  - Suggestions never capture mail.

### Sent mail

- **Sent mail is captured only under [D-capture](#d-capture-what-is-stored-and-its-provenance).**
- **No Sent folder.** If an IMAP account has no resolvable Sent folder, it shows "Sent mail unavailable", and inbound behavior is unchanged.

### Thread view (desktop)

- **Vendors view.** It lists vendors, then each vendor's threads (newest activity first), then a thread.
  - The thread shows its messages oldest first, each with its direction, sender, vendor label, and time.
  - Bodies render with `textContent` only.
- **A Content Security Policy is required first.** Before any body renders, the webview gets a CSP that forbids inline script and remote loads.
- **Inbox and watchlist copy** says that followed threads and Sent replies to vendors also appear.

## Invariants

- **Claims extraction adds no new destination or data kind.** It sends only the source text that [D-claims](#d-claims-claims-and-comparability) defines to the configured model backend, as per-message analysis already sends the body.
  - Loopback stays on the machine.
  - Gateway mode reaches the operator's configured on-prem gateway (`docs/INFERENCE_GATEWAY_V0.md`).
  - Stored data never leaves the machine otherwise.
- **Mailbox access stays read-only.** IMAP keeps `readonly=True` and `BODY.PEEK`.
- **Every shown claim has validated evidence in a stored body.** Every shown discrepancy cites two such claims.

## Failure cases

- **A provider error during reconcile** retries that unit with backoff. Polling is unaffected.
- **The claims model is unavailable:** "Claims unavailable", with nothing stored.
- **The entitlement lapses mid-reconcile:** the pass stops at its next budget check, and its progress is kept.

## Milestones

Each milestone gets its own `plans/PR-*.md` plan PR, accepted before code. No milestone ships UI that depends on a later one.

- **M1, vendors and thread identity.**
  - [D-vendor](#d-vendor-vendors-and-vendor_ofaddress) records with exact addresses.
  - The vendor operations and the `watchlist.remove` guard.
  - [D-identity](#d-identity-message-identity-direction-and-thread-keys) thread keys on newly captured messages, including the IMAP reply-header fetch, components, and merges.
  - A Vendors list in the desktop.
  - Admission is unchanged.
- **M2, capture.**
  - [D-capture](#d-capture-what-is-stored-and-its-provenance) and [D-follow](#d-follow-followed-threads).
  - Sent capture with per-folder cursors.
  - [D-reconcile](#d-reconcile-coverage-and-the-reconcile-pass).
  - [D-body](#d-body-stored-bodies) storage, including the date context.
  - [D-scope](#d-scope-in-scope-messages-and-retention) purge, with the retention settings copy.
- **M3, the thread view**, with its CSP and the inbox copy.
- **M4, [D-claims](#d-claims-claims-and-comparability).** Deploying the extractor marks coverage stale, so retained messages get claims.
- **M5, domains and suggestions.** Domain opt-in under [D-vendor](#d-vendor-vendors-and-vendor_ofaddress), and the suggestion flow.

### Required items carried to milestone plans

Each named plan must include these, with fail-first tests.

- **M2:**
  - Gmail discovery pages `messages.list` with durable page tokens. `threads.get` only syncs known threads.
  - Every Gmail message gets a bounded `format=metadata` fetch before its [D-scope](#d-scope-in-scope-messages-and-retention) check, and the body is fetched only after that check.
  - Microsoft discovery pages each folder by `receivedDateTime` and matches recipients locally, never using `$search`.
  - IMAP sync searches `HEADER Message-ID`, `In-Reply-To`, and `References` until the component stops growing.
  - The date context is stored at capture, with a test that changes the zone after storage.
- **M4:**
  - Per-claim anchor binding, tested on "PO-1 total $100; PO-2 total $200".
  - Canonical item keys, tested on "premium red widget".
  - Atomic supersession by extractor version.
  - Re-extraction months later yields the same dates.

## Operator decisions (accepted 2026-10-05, as recommended)

- **D1:** a followed thread is purged as a unit, by its newest message ([D-scope](#d-scope-in-scope-messages-and-retention)).
- **D2:** no encryption at rest in this arc. Bodies get today's `0600` database in a `0700` directory (`db.py:4502`, `db.py:4826`), plus `secure_delete`. SQLCipher would be its own arc.
- **D3:** gate on the existing `connect.capability_exchange` ([D-ops](#d-ops-operation-classes-and-gating)).

## Dependencies

- **Gateway mode needs a registered claims task** (for example `email.vendor_claims.extract` v1) before M4 works there. Today's tasks are `email.analyze` and `email.schedule.extract` (`model.py:433-435`).
- **Gmail restricted-scope distribution (#74).** Storing bodies locally should be reviewed against it before a public release.

## Explicit non-scope

- Sending, replying, moving, or labeling mail.
- Any cloud model or cloud storage.
- More than one active mailbox at a time (#149).
- Thread-level AI summaries.
- Attachment content in claims.
- Changing today's per-message analysis, notifications, scheduling, or Connect behavior.

## Acceptance evidence

Each milestone plan names its fail-first tests. The arc-level scenarios are:

- **The core scenario**, on each provider: a fixture thread with an inbound quote, an outbound reply, and an inbound invoice that differs, giving one discrepancy with both quotes.
- **No trace:** a non-vendor message leaves no row.
- **Attribution:** an outbound message to vendor A (`To`) and vendor B (`Cc`) is A's, and B's label shows on it.
- **Provenance:** a message captured by `gmail_user_label`, whose sender later becomes a vendor address, starts a follow and keeps its provenance.
- **Gmail direction:** a Gmail message with both `INBOX` and `SENT` gets the same direction and kind on every path.
- **IMAP identity:**
  - colliding Inbox and Sent UIDs give two messages;
  - a moved message with a `Message-ID` is captured once;
  - reverse-order arrival (C, B, A) gives one thread;
  - a bridging message merges two followed components without skipping either one's messages.
- **Reconcile:**
  - a Connect lapse, then reactivation, recovers in-cutoff mail from the lapse and conversations started during it;
  - raising `retention_days` resyncs threads from the earlier cutoff;
  - adding a vendor pulls in retained mail;
  - deploying M4 extracts claims for M2 and M3 messages.
- **Scope:**
  - a Microsoft deleted-items message in a followed conversation is never fetched;
  - an archived, label-captured Gmail message shows "outside the admitted folders".
- **Claims:**
  - a reply quoting an earlier total yields no claim from the quote;
  - two totals for one PO in one message are ambiguous;
  - after PO-1 and PO-2 totals, a PO-2 invoice compares only with PO-2;
  - two orders without a shared anchor stay unflagged.
- **Gating:**
  - with an expired entitlement, reading and removal work while adds and sync are refused;
  - removal controls stay visible.
- **Vendor operations:**
  - `watchlist.remove` of a vendor address returns `conflict`;
  - deleting a vendor ends following, and offers to stop watching its addresses;
  - the mailbox owner is never suggested.
- **Scopes:** `Mail.Read` and `gmail.readonly` remain the only scopes.

## Revision log

- 2026-10-05: proposed, then accepted by the operator with D1-D3 as recommended.
- 2026-10-05: five Codex review rounds (12, 7, 7, 9, and 7 findings) were each answered with local edits.
  - Later rounds were mostly contradictions those edits created, because each rule was restated in up to 14 places.
  - Examples: removal was left ungated while the desktop hid it, and the subject was kept as evidence while only authored text was allowed.
- 2026-10-05: restructured into definitions-first form, at the operator's direction ("share seams, stop symptom patching").
  - Every rule is now stated once in [Definitions](#definitions).
  - A test enforces that its key phrases appear nowhere else.
  - Provider mechanics moved to milestone plans.
  - **One deliberate simplification:** capture and following come from vendor records directly ([D-capture](#d-capture-what-is-stored-and-its-provenance)), not from watchlist membership. A hand-edited `watched: false` therefore affects only inbox navigation.
  - The sixth round's six findings are each answered by one definition: D-capture and D-follow for provenance, D-ops for controls, D-body and D-scope for out-of-folder messages, D-claims for ambiguity, D-body and M2 for the date context, and D-ops for deletion.
