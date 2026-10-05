# Thread View and Vendor Claims Contract

Status: proposed, awaiting operator acceptance. No implementation may start before acceptance (contract-first rule).

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
2. **A vendor may opt in one or more business domains.**
   - Any admitted-folder message whose `From` domain equals an opted-in domain is admitted for that vendor.
   - Domains on the public-provider list (gmail.com, googlemail.com, yahoo.com, outlook.com, hotmail.com, live.com, icloud.com, me.com, aol.com, proton.me, protonmail.com, gmx.com, and similar) are refused with a stated reason.
   - The list is a closed constant owned in one module and tested.
3. **New addresses are suggested, never auto-added.** When a followed thread contains a sender that matches no vendor address, the vendor shows a suggestion: "Add <address> to <vendor>?"
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
6. **Sent mail is read** with the same read-only grants:
   - Gmail: the `SENT` label in history;
   - Microsoft 365: `mailFolders/sentitems` delta;
   - IMAP: the folder with the RFC 6154 `\Sent` attribute, falling back to a configured folder name.
   - A Sent message is admitted only by `sent_to_vendor` or `thread_follow`. Other Sent mail leaves no trace, matching today's rule for non-matching mail.
   - If no IMAP Sent folder can be resolved, the account shows "Sent mail unavailable" and inbound behavior is unchanged.
7. **Thread history backfill.** When a thread becomes followed, earlier messages in that thread that are within retention are fetched and admitted as `thread_follow`, from INBOX and Sent:
   - Gmail: `threads.get`.
   - Microsoft 365: `/me/messages` filtered by `conversationId`.
   - IMAP: searching INBOX and Sent by the thread's `Message-ID` set.
   - Backfill is bounded per thread and per check, and resumes on the next check when a budget is exhausted.
   - It never fetches messages outside retention.

### Thread identity

8. **Each admitted message gets a thread key scoped like message identity:** `(provider, account_id, mailbox_identity_key, provider_thread_key)`.
   - Gmail uses `threadId`, and Microsoft 365 uses `conversationId`.
   - IMAP uses the first `References` entry, else `In-Reply-To`, else the message's own `Message-ID`. Members are joined when any of their `Message-ID`, `In-Reply-To`, or `References` values match.
   - The IMAP rule is stated as best-effort, because clients can break chains. A message with no usable headers forms its own thread.
   - Today's per-message IMAP `thread_id` value is not reused as a thread key.
9. **Each message carries a closed `direction`:** `inbound` (admitted from INBOX) or `outbound` (admitted from Sent).

### Bodies

10. **Admitted messages store their normalized body text locally.**
    - The text comes from the same `bounded_body_text` owner, with a separate storage cap. The cap is larger than `body_char_limit` and is a named constant.
    - Raw HTML is never stored, so nothing renders as markup.
    - The pre-cut length is stored as well, so a stored body that was cut is labeled as cut.
    - Bodies obey the same retention as their message and are deleted with it.
    - The connection that deletes body rows uses `PRAGMA secure_delete = ON`, so purged text does not survive in free pages.
11. **Messages admitted before this arc keep no body.** The UI shows "Body not stored (received before thread view)." Users can trigger a bounded re-fetch for a thread they open, if the source still exists.

### Thread view (desktop)

12. **A Vendors view** lists vendors, then each vendor's threads (newest activity first), then a thread.
    - The thread shows its messages oldest first.
    - Each message shows its direction, sender, and time.
    - Bodies render with `textContent` only.
13. **Rendering bodies requires a Content Security Policy.** Before any body renders, the webview gets a CSP that forbids inline script and remote loads.
14. **The inbox and watchlist copy is updated** to say that followed threads and Sent replies to vendors also appear.

### Vendor claims ("keep vendors honest")

15. **Each vendor message in a followed thread is offered to a new extraction task.** The task returns typed claims, each with evidence quotes. The claim types are a closed set:
    - `amount` (value and currency);
    - `date_commitment` (what, by when);
    - `quantity` (item and count);
    - `term` (short text);
    - `reference` (invoice, quote, or PO number).
16. **Evidence is validated in code, as scheduling already does.**
    - Every quote must be a whitespace-normalized substring of that message's stored body or subject.
    - Values are re-derived from the quote: amounts by a deterministic parser, dates by the scheduling date rules.
    - Claims that fail validation are discarded and logged as rejected. They are never shown.
17. **Discrepancies are computed in code, never by the model.**
    - A later vendor claim of the same type and subject that differs from an earlier one is shown on the thread, with both quotes and dates. Examples: an invoice amount differing from a quoted amount, a delivery date later than a promised date, a quantity changed.
    - The model only extracts; it never decides whether something is a discrepancy.
18. **The model input is bounded.** Claims are extracted per message, not per thread, so input stays within `body_char_limit`. Truncation is recorded as in #146.
    - Thread-level summarization is out of scope for this arc.

### Gating

19. **Every capability in this arc requires the paid Connect entitlement**, `connect.capability_exchange`, the same feature that gates Connect today (`engine_api.py` `require_connect_entitlement`). This covers vendors, following, Sent capture, backfill, body storage, the thread view, and claims.
    - When the entitlement is inactive, capture stops.
    - Already-stored data stays readable until retention removes it.
    - The view shows "Connect required".

## Invariants

- No new data leaves the machine. The claims task uses the configured loopback model, or a gateway task only if that gateway has the task registered (see Dependencies).
- **Mailbox access stays read-only.** No message is moved, labeled, marked read, or sent. IMAP keeps `readonly=True` and `BODY.PEEK`.
- **Admission is still explainable per message.** Every admitted message has exactly one admission kind and selector, and its provenance stays immutable (`db.py:3645-3671`).
- **Non-matching mail still leaves no trace.** A message that matches no rule, including a followed thread, writes nothing.
- **Bodies never render as HTML.**
- **A shown claim always has validated evidence in a stored body.** A shown discrepancy always cites two validated claims.
- Retention removes bodies, claims, and discrepancies with their message (see D1 for thread retention).

## Concurrency and idempotency

- **Admission stays idempotent** on the existing unique source identity (`db.py:3333-3337`). Backfill and polling can meet the same message without duplicates.
- **A thread becomes followed once.** That is a unique row per thread key. Concurrent checks race on it under the existing operation lock (`engine_api.py:2192-2199`).
- **Backfill progress is durable and per thread,** so a crash or budget stop resumes without refetching completed pages.
- **Claims are keyed per message and extractor version.** Re-extraction only happens on an explicit version bump.

## Failure cases

- **A provider thread API error:** that thread's backfill retries with backoff. Polling and inbox behavior are unaffected.
- **No IMAP Sent folder:** the account shows "Sent mail unavailable". Following still works for inbound mail.
- **The claims model is unavailable or returns invalid output:** the message shows "Claims unavailable", with no partial claims.
- **The entitlement lapses mid-backfill:** backfill stops at the next budget check, and existing rows stay.

## Milestones

Each milestone gets its own `plans/PR-*.md` plan PR, accepted before code. No milestone ships UI that depends on a later one.

- **M1, vendor records and thread identity.**
  - Vendor tables and the engine API.
  - A thread key on every newly admitted message, from all providers, including IMAP `In-Reply-To`/`References` fetch.
  - A Vendors list in the desktop, with no thread view yet.
  - No change to what is admitted.
- **M2, follow the thread and Sent mail.** The three new admission kinds, Sent capture on all providers, IMAP Sent discovery, and bounded backfill. `To`/`Cc` are fetched for Sent matching.
- **M3, local bodies and the thread view.** Body storage with `secure_delete`, the CSP, the Vendors → threads → thread UI, and the updated inbox copy.
- **M4, vendor claims and discrepancies.** The extraction task and code validation, then deterministic discrepancy rules and their UI.
- **M5, domain opt-in and address suggestions.** The public-provider refusal list and suggestion accept/dismiss.

## Decisions for the operator

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
- an IMAP thread joined across `In-Reply-To` and `References`;
- `Mail.Read` / `gmail.readonly` remaining the only scopes.

## Revision log

- 2026-10-05: proposed.
