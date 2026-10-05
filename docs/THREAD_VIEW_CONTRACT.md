# Thread View and Vendor Claims Contract

Status: accepted by the operator on 2026-10-05, with D1-D3 as recommended. It was revised the same day for the Codex review on #200; see the revision log. Each milestone still needs its own accepted plan before code.

## Why this arc exists

The operator wants Email Watcher users to "keep vendors honest". A user should see a whole conversation with a vendor, including what was quoted, promised, or agreed earlier and their own replies. They should be told when a later message or invoice departs from an earlier claim.

Today the product cannot do this, by construction. A read-only investigation of `origin/main` at `8c1e076`, re-checked at `6a068bf`, found the following.

- **Admission is per message, from INBOX only.**
  - A message is admitted only if it carries `INBOX` and its `From` exactly matches a watched address, or carries a selected Gmail user label (`service.py:153-199`, `service.py:166`).
  - Microsoft 365 reads only `mailFolders/inbox` (`microsoft365.py:275`, `microsoft365.py:615`). IMAP selects only `INBOX` and has no folder discovery (`imap.py:1366`).
  - The user's own replies (Sent) are never read.
  - Non-matching messages leave no trace (`service.py:2105-2106`).
- **Thread identity is stored but never used, and is wrong for IMAP.**
  - `messages.thread_id` (`db.py:4602`) holds Gmail `threadId` (`gmail.py:490`) and Microsoft `conversationId` (`microsoft365.py:611-633`).
  - For IMAP it holds the message's own `Message-ID` (`imap.py:1613`), which is not a thread key.
  - Nothing reads the column, and nothing indexes it.
  - `In-Reply-To`, `References`, `To`, and `Cc` are never fetched (`gmail.py:937-938`, `imap.py:1599`).
- **Bodies are never stored, and the app has never displayed one.**
  - There is no body column (`db.py:4597-4629`). Bodies are fetched for analysis and discarded (`service.py:2319`).
  - An inbox card shows only the stored summary (`desktop/src/main.ts`).
- **There is no backfill.** Every provider starts at "now" (`gmail.py:768-769`, `microsoft365.py:278-280`, `imap.py:1434-1448`). Older messages in a thread cannot be fetched by any code path.
- **Retention drops the oldest messages first.** Purge is keyed on each message's `received_at` (`db.py:11834`). Messages older than the cutoff are never admitted (`service.py:2107-2113`).
- **Analysis sees one message at a time.**
  - Its output has no amounts, quotes, or evidence spans (`model.py:80-92`).
  - The only evidence-bound extractor is scheduling, which requires each quote to be a substring of its source (`scheduling.py:387-429`).
- **The read-only scopes already permit this arc.** Gmail `gmail.readonly` (`gmail.py:42`) and Microsoft `Mail.Read` (`microsoft365.py:36`) both allow reading threads and Sent mail. No new consent is needed.
- **The current UI promises are narrower than this arc.** The inbox states "Only messages from watched senders or Gmail labels appear here." (`desktop/src/main.ts:520`).
- **The desktop webview has no Content Security Policy** (`desktop/src-tauri/tauri.conf.json`, `"csp": null`).

## Operator decisions already made (2026-10-05)

1. **Store email bodies locally.** They stay on the user's machine and never go to a cloud service.
2. **Read Sent mail.** The user's own messages are part of the thread.
3. **Vendor matching:** follow the thread, plus vendor records.
   - Once one message from a vendor is admitted, the whole conversation is admitted.
   - A vendor is a named record with several exact addresses.
   - "Anyone at a domain" is an opt-in that is refused for public email providers.
   - A new address seen in a followed thread is suggested, never auto-added.
   - The user's Sent mail matches on recipients (`To`/`Cc`).
4. **The feature is paid Connect.**
5. **Prerequisites are fixed:** UTC prompt time (#194, PR #195) and the body truncation signal (#146, PR #199).

## Observable behavior (whole arc)

### Vendors

1. **A vendor is a named record with one or more exact addresses.** Addresses are normalized with `normalize_validated_address`, the same rule the watchlist uses (`config.py:240-260`).
   - Every vendor address is also a watched sender.
   - Adding an address to a vendor adds it to the watchlist through the existing atomic watchlist mutation (`engine_api.py:5506`).
   - Removing it from the vendor does not remove it from the watchlist unless the user asks.
   - A normalized address belongs to at most one vendor; adding it to a second vendor returns `conflict`.
   - `watchlist.remove` of an address that belongs to a vendor is rejected with `conflict`, naming the vendor. The user must remove it from the vendor first. This keeps "every vendor address is watched" true through the API.
   - A hand edit of the private config can still drop a vendor address from the watchlist. `vendors.list` then reports that address as `watched: false`, as a repair state with a "Watch again" action. Until it is repaired, that address does not trigger following, because following requires an `exact_sender` admission.
2. **A vendor may opt in one or more business domains.**
   - Any admitted-folder message whose `From` domain equals an opted-in domain is admitted for that vendor.
   - Domains on the public-provider list (gmail.com, googlemail.com, yahoo.com, outlook.com, hotmail.com, live.com, icloud.com, me.com, aol.com, proton.me, protonmail.com, gmx.com, and similar) are refused with a stated reason.
   - The list is a closed constant owned in one module and tested.
   - A domain belongs to at most one vendor; opting it in for a second vendor returns `conflict`. An exact vendor address always takes precedence over a domain match.
2a. **Vendor attribution has one owner: the attribution rule.** Every other part of this contract (following, suggestions, claims, the view) reads its result and never decides vendors on its own.
    - **A message's vendor** is decided as follows:
      - inbound: the vendor that owns its `From` address, else the vendor that owns its `From` domain;
      - outbound: the vendor that owns the first matching address in `To` header order, then `Cc` header order (domain matches only after every exact address);
      - otherwise none.
      - Header order is the order in the stored message, so the result is deterministic.
    - **A thread's owning vendor** is the vendor of the message that started following it. It is recorded with the follow and never changes afterwards. The one exception is a component merge, which keeps the owner of the surviving (earliest-created) component.
    - **A thread can involve other vendors.** A message attributed to another vendor stays in the thread, shows its own vendor label ("also involves <vendor>"), and appears under that vendor's view as a linked thread. Ownership does not change.
    - Claims belong to the vendor of the message that carries them, not to the thread owner. Suggestions go to the thread owner.
3. **New addresses are suggested, never auto-added.** When a followed thread contains an **inbound** message whose sender has no vendor under item 2a, the thread's owning vendor shows a suggestion: "Add <address> to <vendor>?"
   - Outbound messages never produce suggestions.
   - Every verified identity of the active mailbox is excluded: `mail_accounts.address`, plus the authenticated address the provider reports for the session.
   - The mailbox owner is therefore never suggested as a vendor address.
   - Accepting runs item 1.
   - Dismissing records the dismissal so the same suggestion does not return.
   - Removing an address from a vendor also records a dismissal for that `(vendor, address)` pair. The product therefore never prompts the user to undo their own removal.
   - Suggestions never admit mail on their own.

### Admission (adds to today's rules, never replaces them)

4. **Admission gains three kinds:**
   - `thread_follow`: the message belongs to a followed thread;
   - `sent_to_vendor`: a Sent message with a vendor address in `To` or `Cc`;
   - `vendor_domain`: an opt-in domain match.
   - Today's `exact_sender` and `gmail_user_label` are unchanged.
   - `messages.admission_kind` stays a closed set (`db.py:3606`) and is widened by migration.
5. **A thread becomes followed** when any message in it is admitted by `exact_sender` for a vendor address, by `vendor_domain`, or by `sent_to_vendor`.
   - Gmail-label-only admission does not follow threads.
   - Following is recorded durably with the triggering message.
5a. **Admission kind and direction come from message metadata, never from the discovery path.** A message can be reachable by several paths (Inbox poll, Sent poll, backfill) and match several rules, so the result is computed only from its labels or folder and its headers.
    - The admission kind is chosen in a fixed precedence: `exact_sender` > `vendor_domain` > `sent_to_vendor` > `thread_follow` > `gmail_user_label`.
    - Whichever path discovers the message first records the same kind and selector. Provenance stays immutable.
6. **Sent mail is read** with the same read-only grants:
   - Gmail: the `SENT` label in history;
   - Microsoft 365: `mailFolders/sentitems` delta;
   - IMAP: the folder with the RFC 6154 `\Sent` attribute, falling back to a configured folder name.
   - A Sent message is admitted only by `sent_to_vendor` or `thread_follow`. Other Sent mail leaves no trace, matching today's rule for non-matching mail.
   - If no IMAP Sent folder can be resolved, the account shows "Sent mail unavailable" and inbound behavior is unchanged.
   - **Cursors are kept per folder.**
     - Microsoft 365 delta links belong to the collection that produced them, so the Sent Items delta link is stored in its own row, keyed by `(provider, account_id, folder)`. It never shares the existing Inbox cursor in `mailbox_state`.
     - IMAP Sent keeps its own `UIDVALIDITY` and UID cursor.
     - Gmail's history id is mailbox-wide, so one cursor covers both.
     - Expiry and recovery of one folder's cursor never move or reset the other's.
7. **Completeness has one owner: the reconcile pass.** Each account keeps a durable **coverage record**: the scope it has reconciled, and a `synced_through` watermark for every followed thread. The scope is the vendor addresses and domains, the retention cutoff, the entitlement state, and the claims extractor version.
   - **Any scope change marks coverage stale:**
     - a vendor address or domain is added;
     - `retention_days` is increased, which moves the cutoff earlier;
     - the Connect entitlement becomes active again;
     - a provider cursor expires or recovers;
     - a new claims extractor version is deployed (M4).
   - A bounded, resumable reconcile pass then brings retained data up to the current scope. No other path fills completeness gaps, so a new trigger is added here and nowhere else.
   - **The reconcile pass has three stages, run in order:**
     - **(a) Discovery.** Find messages within retention in INBOX that come from a current vendor address or domain, and messages in Sent addressed (`To`/`Cc`) to one, that are not yet in a followed thread. Their threads become followed.
       - This is how pre-arc retained mail, conversations started while Connect was lapsed, and mail newly in scope after a vendor or retention change all enter threads.
     - **(b) Thread sync.** For every followed thread, fetch its messages in INBOX and Sent within retention, from its watermark. When the cutoff moved earlier, the sync is full again, from the new cutoff. Each message is admitted under the admission rule (item 5a), which is the only owner of admission kind and direction. A message that also matches `exact_sender` or `vendor_domain` records that stronger kind, whichever path finds it.
     - **(c) Derived work.** Fetch bodies for stored messages that lack one, if the source still exists. Extract claims for inbound vendor messages that lack the current extractor version (M4).
   - Normal polling keeps coverage current between scope changes. The reconcile pass runs only when coverage is stale or a thread's sync is incomplete.
   - **Every stage is bounded per check and resumable from its durable progress,** and never fetches outside retention or outside INBOX and Sent.
     - Mail that aged out of retention before it could be reconciled is not recovered, and the affected thread shows "history partial".
     - A full sync from the retention cutoff is called **backfill** elsewhere in this contract.
   - **Provider rules the M2 plan must satisfy (its plan specifies and tests the exact calls):**
     - **Content is fetched only after the folder or label check and the retention check pass, on per-message metadata.**
       - Gmail: discovery by `threads.get` with `format=minimal` yields only ids and `labelIds`. It is followed by a bounded per-message `format=metadata` fetch for `internalDate` and headers, before any body fetch.
       - Microsoft 365: folder-scoped queries on `/me/mailFolders/inbox/messages` and `/me/mailFolders/sentitems/messages`, filtered by `conversationId` for sync and by `receivedDateTime` at or after the cutoff.
         - Discovery pages each folder by the `receivedDateTime` filter alone, selects sender and recipient metadata, and matches normalized addresses locally.
         - It never relies on `$search`, which is capped and not complete.
         - Only metadata is selected, and paging uses bounded `@odata.nextLink` with durable resume.
     - **Other folders are never fetched,** including Deleted Items, Drafts, Archive, and Clutter.
     - **IMAP thread sync searches `HEADER Message-ID`, `HEADER In-Reply-To`, and `HEADER References`** for every known id of the component, in INBOX and the resolved Sent folder, with `SINCE` the cutoff.
       - It repeats with newly found ids until the component stops growing or the per-check budget runs out.
       - A reply whose own id was unknown is therefore still found through its reply headers.

### Thread identity

8. **Each admitted message gets a thread key scoped like message identity:** `(provider, account_id, mailbox_identity_key, provider_thread_key)`.
   - Gmail uses `threadId`, and Microsoft 365 uses `conversationId`.
   - **IMAP uses an order-independent component model.**
     - Each message contributes its id set: its own `Message-ID`, `In-Reply-To`, and the bounded `References` list.
     - A thread is a connected component of messages whose id sets overlap, identified by a surrogate thread key (UUIDv4) that is created with the component.
     - When a new message's ids touch several existing components, they are merged into the earliest-created one in a single transaction. That transaction re-keys every row that names a merged key: messages, claims, and suggestions. The merged keys are recorded as aliases, so any stale reference resolves to the survivor.
       - **Per-thread state is combined, never overwritten.** If any merged component was followed, the survivor is followed, and its owner is the survivor's owner under item 2a; it is the merged component's owner if the survivor was not followed.
       - The survivor's sync progress is reset to "full sync from the retention cutoff", which marks coverage stale (item 7), so neither component's unsynced messages can be skipped.
       - Duplicate follow and progress rows from the merged keys are deleted in the same transaction.
     - The result is the same thread regardless of arrival order. Reply C, then B, then A, ends as one thread.
     - It remains best-effort only where clients break chains entirely. A message whose ids touch no component forms its own.
   - Today's per-message IMAP `thread_id` value is not reused as a thread key.
9. **Each message carries a closed `direction`:** `inbound` or `outbound`.
   - Direction is derived from the message, not from the path that found it.
   - Gmail: `outbound` if `labelIds` contains `SENT`, else `inbound`. A message labeled both `INBOX` and `SENT` (for example, mail to oneself) is `outbound`.
   - Microsoft 365 and IMAP: `outbound` if the message is in the Sent folder, else `inbound`.
9a. **Message identity has one owner: the identity rule.** Admission, deduplication, following, and backfill all use it.
    - **Source identity is folder-qualified where the provider's ids are.**
      - Gmail message ids are mailbox-wide; labels are not folders.
      - Microsoft 365 ids are requested as immutable ids (`Prefer: IdType="ImmutableId"`, already sent on every Graph request) and survive moves.
      - IMAP UIDs are per folder, and today's IMAP provider id is `mailbox_id:UIDVALIDITY:UID` with no folder (`imap.py:310-315`). Inbox ids keep that format for compatibility. Sent-folder ids add a folder token, and the unique source key (`db.py:3335-3336`) then separates the folders.
    - **Logical identity removes cross-folder duplicates.** On IMAP, a move creates a new UID (RFC 9051 `MOVE`). So a message with a `Message-ID` also has a logical identity, `(provider, account_id, mailbox_identity_key, Message-ID)`.
      - A second source row with an already-admitted logical identity is not admitted again. It is recorded as another location of the same message.
      - Direction is decided once, at first admission, from the folder where it was first found.
      - An IMAP message without a `Message-ID` falls back to its folder-qualified source identity, so a move can then produce a second copy. This is stated as best-effort.

### Bodies

10. **Every message admitted by this arc's capture paths stores its normalized body text locally, from M2 onward.** That covers `thread_follow`, `sent_to_vendor`, `vendor_domain`, backfill, and new `exact_sender` vendor messages. M2 messages therefore have bodies when the M3 view ships.
    - The text comes from the same `bounded_body_text` owner, with a separate storage cap. The cap is larger than `body_char_limit` and is a named constant.
    - Raw HTML is never stored, so nothing renders as markup.
    - The pre-cut length is stored as well, so a stored body that was cut is labeled as cut.
    - Bodies obey the same retention as their message and are deleted with it.
    - The connection that deletes body rows uses `PRAGMA secure_delete = ON`, so purged text does not survive in free pages.
11. **Messages admitted before this arc get their bodies through the reconcile pass (item 7, stages a and c)** when their vendor comes into scope. If the source no longer exists, the UI shows "Body not stored (source no longer available)." There is no separate manual re-fetch path.

### Thread view (desktop)

12. **A Vendors view** lists vendors, then each vendor's threads (newest activity first), then a thread.
    - The thread shows its messages oldest first.
    - Each message shows its direction, sender, and time.
    - Bodies render with `textContent` only.
13. **Rendering bodies requires a Content Security Policy.** Before any body renders, the webview gets a CSP that forbids inline script and remote loads.
14. **The inbox and watchlist copy is updated** to say that followed threads and Sent replies to vendors also appear.

### Vendor claims ("keep vendors honest")

15. **Each inbound vendor message in a followed thread is offered to a new extraction task, and only its newly authored text is used.**
    - The authored text is `current_message_text` from the existing quoted-history splitter (`_split_quoted_history`, `model.py:288`), with every line that starts with `>` removed.
    - Quoted history is never offered for extraction, and evidence quotes must lie inside the authored text. Earlier vendor statements, and the user's own quoted words, can therefore never become new claims.
    - The task The task returns typed claims. Each claim has evidence quotes and a **comparison key**. The claim types are a closed set:
    - `amount`: value, currency, and an `amount_role` from a closed set: `total`, `subtotal`, `tax`, `shipping`, `deposit`, `unit_price`, `other`. For `unit_price`, the key adds the item text.
    - `date_commitment`: the `what`, from a closed set (`delivery`, `completion`, `payment_due`, `service_start`, `other`), and the date.
    - `quantity`: the item text and the count.
    - `term`: short text. Terms are displayed only and never compared.
    - `reference`: kind (`invoice`, `quote`, `po`) and number. References are never compared with each other; they serve only as **transaction anchors** under item 17.
16. **Evidence is validated in code, as scheduling already does.**
    - Every quote must be a whitespace-normalized substring of that message's authored text, as defined by item 15, which is the only owner of the evidence scope.
    - Subjects are never evidence, because replies inherit them unchanged.
    - **Every structured field is re-derived from its quote in code, or the claim is rejected:**
      - amounts and currencies by a deterministic money parser;
      - dates by the scheduling date rules;
      - quantity counts by a deterministic number parser, with the number required to sit next to the item text inside the quote;
      - reference numbers as exact substrings matching the kind's pattern.
    - **Comparison keys are also checked in code.**
      - An `amount_role` other than `other`, and a `date_commitment` `what` other than `other`, must have one of that role's fixed keywords in the quote.
      - A quantity's or unit price's item text must be a substring of the quote.
    - **Validation has two levels, and both are deterministic for the same input:**
      - **The response:** a response that is not valid JSON for the claims schema is rejected whole. The message shows "Claims unavailable" and no claims are stored.
      - **Each claim, in a schema-valid response:** each claim is validated independently. Claims that fail are discarded and counted, and are never shown. Claims that pass are stored. If any were discarded, the message shows a "Some claims could not be verified" note.
17. **Discrepancies are computed in code, never by the model. Comparability has one owner: the comparability rule.** Two validated claims are comparable only if every one of these holds:
    1. They are in the same thread.
    2. They belong to the same vendor (item 2a).
    3. They have the same type and the same validated comparison key.
    4. They share a **transaction anchor**: a validated `reference` pair `(kind, number)` extracted from the authored text of both messages. An invoice that cites "Quote #123" carries the anchor `(quote, 123)`, so it matches the quote. An unrelated "Invoice #123" does not. This applies to `amount` roles other than `unit_price`, to `date_commitment`, and to `quantity`.
       - Without a shared anchor, two totals in one thread may belong to different orders, so they are shown side by side as "No shared quote, PO, or invoice number; compare manually" and never flagged.
       - `unit_price` needs no anchor. A changed price for the same normalized item from the same vendor is shown as "price changed since <date>".
    - **`reference`, `term`, `role = other`, and `what = other` are never compared.**
    - A discrepancy is flagged only between comparable claims whose re-derived values differ, and it is shown with both quotes and dates. For `date_commitment` the flag is "later than promised" when the later claim's date is after the earlier one.
    - **Matching fails closed, after every comparability condition above has been applied.** If more than one earlier claim is *comparable*, meaning it has the same thread, vendor, type, key, and shared anchor, no discrepancy is flagged, and the thread shows "Several values for <key>; compare manually".
      - Earlier claims that are not comparable, such as a total for a different PO, are ignored for this test and never cause ambiguity.
      - Ambiguity never produces a flag.
    - The model only extracts; it never decides whether something is a discrepancy.
18. **The model input is bounded.** Claims are extracted per message, not per thread, so input stays within `body_char_limit`. Truncation is recorded as in #146.
    - Thread-level summarization is out of scope for this arc.

### Gating

19. **Every operation that captures, syncs, reconciles, extracts, or adds vendor data requires the paid Connect entitlement**, `connect.capability_exchange`, the same feature that gates Connect today (`engine_api.py` `require_connect_entitlement`).
    - **Removing data is never gated:** removing an address from a vendor, and deleting a vendor. A user whose Connect has lapsed can therefore still clear an address and then remove it from the watchlist (item 1).
    - **Reading stored data is read-only and is not gated:** listing vendors, threads, messages, bodies, claims, and discrepancies.
    - The read operations never call a provider or model.
    - When the entitlement is inactive, capture stops. Today's inbox behavior continues, and provider cursors keep advancing.
    - Reactivation marks coverage stale, so the reconcile pass (item 7) discovers conversations started during the lapse and resyncs every followed thread from its watermark.
    - Already-stored data stays readable until retention removes it, through the ungated read operations.
    - The view shows "Connect required to update" and hides the controls that change data. It never denies reading.

## Invariants

- **Claims extraction adds no new destination and no new kind of data.**
  - It sends a message's subject and body to the configured model backend, exactly as today's per-message analysis does.
  - In loopback mode that stays on the machine.
  - In gateway mode it goes to the operator's configured on-prem inference gateway (`docs/INFERENCE_GATEWAY_V0.md`), the same place analysis already sends that body.
  - Stored bodies, threads, and claims never leave the machine otherwise.
- **Mailbox access stays read-only.** No message is moved, labeled, marked read, or sent. IMAP keeps `readonly=True` and `BODY.PEEK`.
- **Admission is still explainable per message.** Every admitted message has exactly one admission kind and selector, and its provenance stays immutable (`db.py:3645-3671`).
- **Non-matching mail still leaves no trace.** A message that matches no rule, including a followed thread, writes nothing.
- **Bodies never render as HTML.**
- **A shown claim always has validated evidence in a stored body.** A shown discrepancy always cites two validated claims.
- Retention removes bodies, claims, and discrepancies with their message (see D1 for thread retention).

## Concurrency and idempotency

- **Admission stays idempotent** on the existing unique source identity (`db.py:3333-3337`). Backfill and polling can meet the same message without duplicates.
- **A thread becomes followed once.** That is a unique row per thread key. Concurrent checks race on it under the existing operation lock (`engine_api.py:2192-2199`).
- **Sync progress (`synced_through` plus the page state) is durable and per thread,** so a crash, a budget stop, or an entitlement lapse resumes without refetching completed pages. Only the reconcile pass (item 7) writes it, along with the coverage record.
- **Claims are keyed per message and extractor version.** Re-extraction only happens on an explicit version bump.

## Failure cases

- **A provider thread API error:** that thread's backfill retries with backoff. Polling and inbox behavior are unaffected.
- **No IMAP Sent folder:** the account shows "Sent mail unavailable". Following still works for inbound mail.
- **The claims model is unavailable, or returns a response that fails the schema:** the message shows "Claims unavailable", and no claims are stored. Per-claim failures inside a valid response follow item 16 instead.
- **The entitlement lapses mid-backfill:** backfill stops at the next budget check, and existing rows stay.

## Milestones

Each milestone gets its own `plans/PR-*.md` plan PR, accepted before code. No milestone ships UI that depends on a later one.

This contract owns the arc's rules and invariants. Each milestone plan owns its exact provider calls, schema, and tests, and must satisfy every rule here. Review findings about one milestone's mechanics are raised on that milestone's plan PR, where they can be checked against code. A finding is fixed here only if it contradicts a rule.

- **M1, vendor records and thread identity.**
  - Vendor tables and the engine API, with addresses unique across vendors and `watchlist.remove` guarded.
  - A thread key on every newly admitted message, from all providers. That includes the IMAP `In-Reply-To`/`References` fetch and the component model with atomic merges.
  - A Vendors list in the desktop, with no thread view yet.
  - No change to what is admitted.
- **M2, follow the thread, Sent mail, and body capture.**
  - The three new admission kinds and the metadata-derived precedence.
  - Sent capture on all providers, with per-folder cursors, and IMAP Sent discovery.
  - Bounded, folder-scoped backfill.
  - `To`/`Cc` are fetched for Sent matching.
  - Local body storage with `secure_delete` for every message M2 captures. Nothing renders bodies yet.
  - **D1 thread retention.** Purge removes a followed thread as a unit once its newest message is older than `retention_days`. Messages outside followed threads keep today's per-message purge.
  - The settings copy for retention is updated to say so, and ships in M2 with the purge change.
- **M3, the thread view.** The CSP, the Vendors → threads → thread UI, and the updated inbox copy.
- **M4, vendor claims and discrepancies.**
  - The extraction task and code validation, then deterministic discrepancy rules and their UI.
  - Deploying the extractor version marks coverage stale, so the reconcile pass (item 7, stage c) extracts claims for every retained eligible message. This is bounded and resumable.
- **M5, domain opt-in and address suggestions.** The public-provider refusal list and suggestion accept/dismiss.

## Operator decisions (accepted 2026-10-05, as recommended)

- **D1, retention for followed threads.**
  - Recommended: a followed thread is kept whole until it has had no new message for `retention_days`. Without this, the earliest quote, which is the thing being compared, is deleted first.
  - Alternative: today's per-message retention.
  - This changes what "retention" means for those threads, so the settings copy changes too.
- **D2, encryption at rest for stored bodies.**
  - Recommended: not in this arc. Bodies get the same protection as today's summaries: a `0600` database in a `0700` directory (`db.py:4502`, `db.py:4826`), plus `secure_delete`.
  - Full-database encryption (SQLCipher) would be its own arc, because it changes packaging on Linux and Windows.
- **D3, a separate paid feature.**
  - Recommended: gate on the existing `connect.capability_exchange`. No license changes, and it matches "paid Connect".
  - Alternative: a new feature id, for example `connect.threads`. That needs licenses re-issued with the new feature.

## Dependencies

- **Gateway mode** needs the inference gateway to register a claims task (for example `email.vendor_claims.extract` v1) before M4 works in gateway mode. Loopback mode needs nothing. Today the gateway task ids are `email.analyze` and `email.schedule.extract` (`model.py:433-435`).
- **Gmail restricted-scope distribution** (#74) is unchanged in scope (`gmail.readonly`). Storing bodies locally should be reviewed against that distribution plan before a public release.

## Explicit non-scope

- Sending, replying, moving, or labeling mail.
- Any cloud model or cloud storage.
- More than one active mailbox at a time (#149).
- Thread-level AI summaries.
- Attachment content in claims. Attachment bytes stay on-demand only, as today.
- Changing today's per-message analysis, notifications, scheduling, or Connect behavior.

## Acceptance evidence (per milestone plan)

Each milestone plan names its fail-first tests. The arc-level evidence includes:
- a fixture thread on each provider with an inbound quote, an outbound reply, and an inbound invoice that differs, showing exactly one discrepancy with both quotes;
- a non-vendor message in the same mailbox leaving no row;
- backfill stopping at retention and resuming after a budget stop;
- an IMAP thread joined across `In-Reply-To` and `References`, and the same single thread when the messages arrive in reverse order (C, B, A), with follow state carried across the merge;
- a Gmail message labeled both `INBOX` and `SENT` recording the same direction and admission kind whichever path finds it first;
- the Microsoft Inbox and Sent Items delta cursors advancing and expiring independently;
- a Deleted Items or Archive message in a followed Microsoft conversation never fetched;
- a quote with two `total` amounts producing "compare manually" and no discrepancy, and a quantity claim whose count does not match its quote being rejected;
- the mailbox owner's address never suggested;
- a thread with totals for two orders and no shared reference showing "compare manually" and no flag, and the same totals with a shared PO number flagged;
- an inbound reply quoting an earlier $400 total producing no new claim from the quoted text;
- an outbound message to vendor A (`To`) and vendor B (`Cc`) attributed to A, with B shown as "also involves";
- IMAP Inbox and Sent UIDs that collide numerically producing two distinct messages, and a moved IMAP message with a `Message-ID` admitted once;
- a Connect lapse of N days, then reactivation, recovering the followed-thread mail from the lapse that is still within retention, plus a vendor conversation started during the lapse;
- raising `retention_days` resyncing followed threads from the earlier cutoff;
- an IMAP reply whose own `Message-ID` was unknown, found by reconcile through its `In-Reply-To`;
- after totals for PO-1 and PO-2, an invoice for PO-2 compared only with PO-2's total;
- an expired entitlement still listing stored threads and bodies, while refusing sync and edits;
- deploying the M4 extractor producing claims for messages captured in M2 and M3;
- a vendor address added over retained pre-arc messages pulling those messages into followed threads, with bodies fetched where the source still exists;
- `watchlist.remove` of a vendor address returning `conflict`;
- `Mail.Read` / `gmail.readonly` remaining the only scopes.

## Revision log

- 2026-10-05: proposed.
- 2026-10-05: accepted by the operator with D1-D3 as recommended. Revised in the same PR for twelve Codex findings on #200:
  - the gateway privacy statement is corrected to match existing analysis behavior;
  - validated comparison keys and fail-closed discrepancy matching;
  - order-independent IMAP thread components with atomic merges;
  - `watchlist.remove` guarded for vendor addresses;
  - the mailbox owner excluded from suggestions;
  - every claim field re-derived in code;
  - per-folder Microsoft and IMAP Sent cursors;
  - vendor addresses unique across vendors;
  - body capture moved into M2;
  - Gmail minimal discovery and Microsoft folder-scoped backfill;
  - metadata-derived direction and admission precedence.
- 2026-10-05: the second Codex round on #200 (seven findings) is resolved by consolidating four single owners, so that one class of finding cannot recur in pieces:
  - the vendor attribution rule (2a): multi-vendor messages and threads, and domain uniqueness;
  - the identity rule (9a): folder-qualified IMAP source identity plus a `Message-ID` logical identity;
  - the thread sync pass (7): pre-arc retained mail and entitlement-lapse catch-up go through the one resumable sync;
  - the comparability rule (15, 17): authored text only, transaction anchors, and references used as anchors and never compared.
- 2026-10-05: the third Codex round on #200 (seven findings) is resolved.
  - Completeness moves to one owner, the reconcile pass (item 7). It works from a coverage record, and any scope change marks coverage stale: a vendor added, retention raised, Connect reactivated, cursor recovery, or a new extractor version. One discovery, sync, and derived-work pass then runs, replacing the per-trigger list.
  - Exact provider calls move to the M2 plan, under stated rules: Gmail metadata before the retention check, and IMAP reply-header search.
  - Ambiguity is counted only among comparable claims.
  - Reading stored data is ungated.
- 2026-10-05: fourth Codex round on #200 (nine findings). Most were contradictions created by restating a rule in a second place. Each section now defers to the rule's single owner:
  - admission kind (5a) during sync;
  - evidence scope (15), with subjects excluded;
  - gating (19), with data removal ungated;
  - merge (8), which combines per-thread state.
- Also in the fourth round:
  - anchors are `(kind, number)` pairs;
  - claim validation has two deterministic levels (response, then claim);
  - removing an address suppresses re-suggestion;
  - Microsoft discovery pages by date and matches locally, never with `$search`;
  - D1 thread retention is assigned to M2.
- A governance note now sends milestone-mechanics findings to their milestone plan PR.
