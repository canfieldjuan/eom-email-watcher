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
7. **Thread sync has one owner: a bounded, resumable sync pass per followed thread.** Each followed thread keeps a durable `synced_through` watermark. Every completeness gap is closed by this one mechanism, never by a separate path. A thread is queued for a sync pass when:
   - it becomes followed (a full sync, from the retention cutoff);
   - a vendor address or domain is added. Retained messages already stored from that address or domain, including messages admitted before this arc, make their threads followed, which queues them. This is how pre-arc retained mail enters threads;
   - the Connect entitlement becomes active again after a lapse. Every followed thread resyncs from its watermark, so mail that arrived during the lapse and is still within retention is recovered. Mail that aged out of retention during the lapse is not recovered, and the thread shows "history partial";
   - a provider cursor expires or recovers, for every followed thread on that account.

   A sync pass fetches the thread's messages from INBOX and Sent, within retention, and admits each one as `thread_follow`. For messages that are stored but have no body, it fetches the body if the source still exists. The provider strategies are:
   - **Gmail:** discovery calls `threads.get` with `format=minimal`, which returns ids, `labelIds`, and `internalDate` only, never bodies.
     - Each message id is fetched individually, only if it carries `INBOX` or `SENT` and its `internalDate` is within retention.
     - The discovery response is byte-bounded. If a thread exceeds the bound, only the newest in-retention ids are kept, and the thread is marked "history partial".
     - The per-message fetches are what get split across checks.
   - **Microsoft 365:** two folder-scoped queries, `/me/mailFolders/inbox/messages` and `/me/mailFolders/sentitems/messages`, each filtered by `conversationId` and `receivedDateTime` at or after the retention cutoff. They select metadata only and are paged with bounded `@odata.nextLink`. Messages in any other folder, such as Deleted Items, Drafts, Archive, or Clutter, are never fetched.
   - **IMAP:** searching INBOX and the resolved Sent folder by the thread's `Message-ID` set, with `SINCE` the retention cutoff.
   - Every sync pass is bounded per thread and per check, and resumes from its watermark on the next check when a budget is exhausted. A full sync from the retention cutoff is called **backfill** elsewhere in this contract.
   - A sync pass never fetches messages outside retention.

### Thread identity

8. **Each admitted message gets a thread key scoped like message identity:** `(provider, account_id, mailbox_identity_key, provider_thread_key)`.
   - Gmail uses `threadId`, and Microsoft 365 uses `conversationId`.
   - **IMAP uses an order-independent component model.**
     - Each message contributes its id set: its own `Message-ID`, `In-Reply-To`, and the bounded `References` list.
     - A thread is a connected component of messages whose id sets overlap, identified by a surrogate thread key (UUIDv4) that is created with the component.
     - When a new message's ids touch several existing components, they are merged into the earliest-created one in a single transaction. That transaction re-keys every row that names a merged key: messages, follow state, backfill progress, claims, and suggestions. The merged keys are recorded as aliases, so any stale reference resolves to the survivor.
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
11. **Messages admitted before this arc get their bodies through the thread sync pass (item 7)** when their thread becomes followed. If the source no longer exists, the UI shows "Body not stored (source no longer available)." There is no separate manual re-fetch path.

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
    - Every quote must be a whitespace-normalized substring of that message's authored text (item 15) or subject.
    - **Every structured field is re-derived from its quote in code, or the claim is rejected:**
      - amounts and currencies by a deterministic money parser;
      - dates by the scheduling date rules;
      - quantity counts by a deterministic number parser, with the number required to sit next to the item text inside the quote;
      - reference numbers as exact substrings matching the kind's pattern.
    - **Comparison keys are also checked in code.**
      - An `amount_role` other than `other`, and a `date_commitment` `what` other than `other`, must have one of that role's fixed keywords in the quote.
      - A quantity's or unit price's item text must be a substring of the quote.
    - Claims that fail validation are discarded and logged as rejected. They are never shown.
17. **Discrepancies are computed in code, never by the model. Comparability has one owner: the comparability rule.** Two validated claims are comparable only if every one of these holds:
    1. They are in the same thread.
    2. They belong to the same vendor (item 2a).
    3. They have the same type and the same validated comparison key.
    4. They share a **transaction anchor**: a `reference` number, of any kind, present in the authored text of both messages. This applies to `amount` roles other than `unit_price`, to `date_commitment`, and to `quantity`.
       - Without a shared anchor, two totals in one thread may belong to different orders, so they are shown side by side as "No shared quote, PO, or invoice number; compare manually" and never flagged.
       - `unit_price` needs no anchor. A changed price for the same normalized item from the same vendor is shown as "price changed since <date>".
    - **`reference`, `term`, `role = other`, and `what = other` are never compared.**
    - A discrepancy is flagged only between comparable claims whose re-derived values differ, and it is shown with both quotes and dates. For `date_commitment` the flag is "later than promised" when the later claim's date is after the earlier one.
    - **Matching fails closed.** If more than one earlier claim has the same key, for example two different totals, no discrepancy is flagged, and the thread shows "Several values for <key>; compare manually". Ambiguity never produces a flag.
    - The model only extracts; it never decides whether something is a discrepancy.
18. **The model input is bounded.** Claims are extracted per message, not per thread, so input stays within `body_char_limit`. Truncation is recorded as in #146.
    - Thread-level summarization is out of scope for this arc.

### Gating

19. **Every capability in this arc requires the paid Connect entitlement**, `connect.capability_exchange`, the same feature that gates Connect today (`engine_api.py` `require_connect_entitlement`). This covers vendors, following, Sent capture, backfill, body storage, the thread view, and claims.
    - When the entitlement is inactive, capture stops. Today's inbox behavior continues, and provider cursors keep advancing.
    - On reactivation, the thread sync pass (item 7) resyncs every followed thread from its watermark.
    - Already-stored data stays readable until retention removes it.
    - The view shows "Connect required".

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
- **Sync progress (`synced_through` plus the page state) is durable and per thread,** so a crash, a budget stop, or an entitlement lapse resumes without refetching completed pages. Only the thread sync pass (item 7) writes it.
- **Claims are keyed per message and extractor version.** Re-extraction only happens on an explicit version bump.

## Failure cases

- **A provider thread API error:** that thread's backfill retries with backoff. Polling and inbox behavior are unaffected.
- **No IMAP Sent folder:** the account shows "Sent mail unavailable". Following still works for inbound mail.
- **The claims model is unavailable or returns invalid output:** the message shows "Claims unavailable", with no partial claims.
- **The entitlement lapses mid-backfill:** backfill stops at the next budget check, and existing rows stay.

## Milestones

Each milestone gets its own `plans/PR-*.md` plan PR, accepted before code. No milestone ships UI that depends on a later one.

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
- **M3, the thread view.** The CSP, the Vendors → threads → thread UI, and the updated inbox copy.
- **M4, vendor claims and discrepancies.** The extraction task and code validation, then deterministic discrepancy rules and their UI.
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
- a Connect lapse of N days, then reactivation, recovering the followed-thread mail from the lapse that is still within retention;
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
