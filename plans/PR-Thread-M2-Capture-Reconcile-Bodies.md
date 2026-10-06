# Thread View M2: Sent Capture, Derived State, Reconcile, and Bodies

Milestone M2 of [`docs/THREAD_VIEW_CONTRACT.md`](../docs/THREAD_VIEW_CONTRACT.md). It includes amendment A of #207 (D-derived), committed separately ahead of this plan.

This plan specifies M2's mechanics only. Every rule it implements is stated in the contract's definitions, and this plan cites them rather than restating them. Where this plan and a definition seem to differ, the definition wins, and this plan is wrong.

C = `docs/THREAD_VIEW_CONTRACT.md`. Definitions are cited as C/D-name.

## Why this slice exists

M1 gave vendors and thread keys. M2 makes them capture and keep mail:
- Sent mail, recipients, and locations, so direction and outbound attribution exist (C/D-identity, C/D-attribution);
- the derived state of C/D-derived (follow cache, attribution, bodies kept);
- the new capture kinds of C/D-capture;
- the reconcile pass of C/D-reconcile;
- stored bodies (C/D-body);
- the followed-thread purge of C/D-scope.

None of it exists today:
- **Polling is inbox-only.**
  - `match_mailbox_admission` returns nothing without `INBOX` (`service.py:165-168`).
  - Microsoft polls `mailFolders/inbox` delta (`microsoft365.py:275`).
  - IMAP selects only `INBOX` (`imap.py:1416`).
  - Both hard-code `labels={"INBOX"}` (`microsoft365.py:641`, `imap.py:1677`).
- **Metadata has no recipients.** Gmail asks only `From, Subject, Date` (`gmail.py:937`), and `MessageMetadata` has no To/Cc or location (`mailbox.py:60-72`).
- **`mailbox_state` holds one cursor per account** (`db.py:4766-4772`).
- **No provider has a thread-scoped fetch.** No `threads.get`, no `conversationId` filter, no IMAP `SEARCH HEADER`.
- **Bodies are never stored.** `html_to_text` flattens quotes (`mime.py:35-63`).
- **Purge ignores threads, and nothing uses `secure_delete`.** Purge is `purge_with_outcome` (`db.py:12273`).
- **The admission kind is a closed CHECK of two values** (`db.py:3855-3882`).

## Operator decisions (2026-10-06)

1. **Unfollow deletes bodies right away.** A thread that stops being followed loses its stored bodies in the same transaction (C/D-body, C/D-derived).
2. **Sent is polled only while Connect is active** (step 2).
3. **The `messages` rebuild gets a backup, besides its in-transaction checks** (step 11).
4. **The stored-body cap is 200,000 characters** (step 21).

## Scope

Ownership lane: thread-view-m2

M2 ships as four implementation PRs under this one plan, in order. Each is fail-first and leaves main releasable. No UI depends on a later slice: M2's only desktop changes are a health notice, the widened admission kinds in the inbox, and the retention copy.

- **M2.1 — Sent, recipients, locations.**
- **M2.2 — Derived state and capture kinds.**
- **M2.3 — The reconcile pass.**
- **M2.4 — Bodies and the followed-thread purge.**

**Out of scope:**
- The thread view and its CSP (M3).
- Claims (M4).
- Domains and suggestions (M5): `vendor_domain` is added to the closed set now, but nothing produces it until M5.
- Any read operation over threads or bodies (M3 adds them).

## Mechanics

### M2.1 — Sent, recipients, locations

1. **Metadata gains** `to`, `cc` (header order kept), `rfc_message_id` for every provider, and `location` (`inbox` or `sent`).
   - **Gmail.** `metadataHeaders` adds `To, Cc, Message-ID, In-Reply-To, References`. The location comes from `labelIds`, already parsed (`gmail.py:465-487`). A message carrying both labels records both locations.
   - **Microsoft.** `$select` adds `toRecipients, ccRecipients, internetMessageId, parentFolderId`. The metadata URL becomes folder-agnostic `/me/messages/{id}`. `parentFolderId` maps to a location through the well-known `inbox` and `sentitems` ids, which are resolved once per check. Any other folder is out of scope (C/D-scope) and leaves no trace.
   - **IMAP.** The first header item adds `TO CC` under its existing 64 KiB bound. The location is the selected folder.
2. **Sent folders,** each polled only while the gated class is allowed (C/D-ops).
   - Sent capture is gated, so polling Sent while the class is inactive could capture nothing. The gap is recovered on reactivation, because coverage compares the entitlement with its record (C/D-reconcile).
   - **Gmail:** the one history cursor already returns `SENT` events (no `labelId` filter, `gmail.py:838-848`). A `SENT`-only message is admitted, and its location recorded, only while the gated class is allowed: the folders in scope are decided once per check from `connect_entitlement_decision` and passed to every admission and insert, which step 10 keeps as the capture decision's input. C/D-reconcile's checkpoint rule holds as long as the cursor advances only after the whole batch (`service.py:2152-2158`), which is unchanged.
   - **Microsoft:** a second delta link on `mailFolders/sentitems`. `_DELTA_PATH` already accepts any folder (`microsoft365.py:46-51`).
   - **IMAP:** `LIST` with RFC 6154 `SPECIAL-USE` finds `\Sent`, falling back to a configured `imap_sent_folder` name.
     - With neither, the account has no Sent scope, and health reports "Sent mail unavailable" (C/D-scope).
     - Each select resets the cached `_eom_uid_validity` and `_eom_message_count` (`imap.py:421-434`).
3. **Folder cursors.** A new `mailbox_folder_state(provider, account_id, mailbox_identity_key, folder, cursor, last_success_at)` holds the Sent cursors. `mailbox_state` stays the Inbox (or Gmail mailbox-wide) cursor, so no existing cursor migrates. A folder cursor advances only on its own batch (C/D-reconcile).
4. **IMAP Sent identity.** Sent ids are `eom-imap-sent-v1:<mailbox_id>:<folder_sha256>:<UIDVALIDITY>:<UID>`, the folder token of C/D-identity. Inbox ids are unchanged. `_checked_uid` validates against the folder the id names (`imap.py:1500-1507`).
5. **Storage (schema 30):**
   - `message_recipients(message_id, field, position, address)`, written at capture for every captured message. Outbound attribution reads it (C/D-attribution).
   - `message_locations(message_id, provider, account_id, mailbox_identity_key, provider_message_id, location, recorded_at)`, primary key `(provider, account_id, mailbox_identity_key, provider_message_id, location)`. One source identity is recorded once per location, so a Gmail message with both labels has two rows under one id. `recorded_at` is when the folders were observed with every admitted folder in scope; it is NULL when the row was assumed by the migration or observed while Sent was out of scope (step 2), since neither says whether the message is also in Sent.
   - `messages.logical_of`, NULL for a logical message. It holds the canonical row's id for a row that duplicates one.
   - A trigger deletes a message's recipients and locations with it, so `delete_message`, `clear_messages`, and the purge leave no orphan rows, and a stale location key never blocks a later recapture.
   - Deletion works on the logical message as one unit: `delete_message` and `clear_messages` remove the canonical row, every row pointing to it through `logical_of`, and record a suppression for each recorded source identity, so no copy is recaptured after a cursor recovery.
   - `messages.capture_timezone`, the configured zone at capture. With `received_at`, it is C/D-body's date context, recorded here because a body can arrive in a later check (step 22). Retained rows keep it NULL: their context is unavailable, which C/D-claims already defines (dates without context are rejected).
   - Migration:
     - every retained row gets an `inbox` location with no `recorded_at`: the pre-M2 poller admitted from the Inbox only, so the folder is assumed, not observed. The first fetch of the message with Sent in scope (step 6, or discovery in step 16) records the folders it observes and stamps the row, so a Gmail message that carried `INBOX` and `SENT` all along gets both, and its recipients;
     - retained rows sharing a logical identity (C/D-identity) are coalesced: the canonical-smallest row is the logical message, the others point to it through `logical_of`, and their locations move to it;
     - no row is deleted, so per-row summaries, attachments, and automation runs survive (the M2 required item);
     - readers that list messages show one entry per logical message: `query_inbox` (`db.py:11957`, which `recent` wraps) adds `logical_of IS NULL`, so an upgraded duplicate is not a second inbox row, and its page cursor follows the same filter. The duplicate's own summary and attachments stay stored under its row, reachable through its logical message (M3's thread view lists copies).
6. **A second location** of a captured logical identity is recorded on the logical message, inside the capture transaction (C/D-identity). It is not a new row, and it gets no admission or analysis.
   - Gmail and Microsoft ids span folders, so a known id comes back through polling when its folder changes (a `SENT` label added, a move into Sent Items). For such an id, polling fetches metadata while fewer than every location in scope is recorded, and records the location; Sent is in scope only while the gated class is allowed (step 2). The same fetch stores the recipients of a row that has none, since the migration cannot reconstruct To/Cc and D-attribution reads them once a Sent location turns the message outbound. Fetched or not, the event is a changed input of that message (step 9): it is in an admitted folder again, so a completed unavailable reason cannot stand. IMAP copies have folder-tokened ids (step 4) and reach the same transaction through their logical identity.
   - IMAP survivor ranking (`_imap_thread_key`, M1) reads each logical message's smallest source identity across all its recorded locations (C/D-identity's canonical order), so a later location can decide which component survives a merge.
7. **Health.** `health.get` reports each account's Sent scope: available, unavailable, or not polled. The desktop shows "Sent mail unavailable" on that account.

### M2.2 — Derived state and capture kinds

8. **Derived storage (schema 31):**
   - `message_derived(message_id, direction, vendor_id, match)`, the stored attribution of C/D-attribution, through `vendor_of` with its match (C/D-vendor);
   - `thread_follow(provider, account_id, mailbox_identity_key, thread_key, followed, owner_vendor_id)`, the C/D-follow cache;
   - `thread_watermarks(provider, account_id, mailbox_identity_key, thread_key, synced_through)`, for C/D-reconcile.

   Provider thread ids mean something only within one mailbox, and an account's mailbox can change, so both caches are keyed by the mailbox identity. A legacy row without one contributes under the account's proven legacy identity, else to no cache, as M1 keyed it.
9. **One recompute function, `_recompute_derived(db, changed)`, owns C/D-derived.**
   - It takes the changed inputs: message ids, thread keys, vendor addresses, or identities. From them it finds the affected logical messages, then their threads.
   - It recomputes attribution, then the follow cache. It deletes the cache row of an empty thread (C/D-follow), the watermark of any thread that is not followed (C/D-reconcile), and the bodies a newly unfollowed thread may not keep (M2.4, C/D-body). A message that gains a location, or that polling reports in an admitted folder again (step 6), loses its unavailable reason (M2.4), `outside_folders` or `source_gone` alike: a copy that arrived or returned can supply the body, so stage (c) fetches it again.
   - The schema-31 migration calls it over every retained message, so an upgraded database has its attribution and follow rows before anything changes.
   - Every writer of an input calls it in its own transaction:
     - capture and location recording;
     - `delete_message` and `clear_messages`;
     - the purge;
     - `_apply_imap_component`, which also clears the survivor's watermark (C/D-reconcile);
     - every vendor mutation;
     - `register_mail_account` and `update_mail_account_identity`;
     - `reconcile_mailbox_identity`, which assigns a proven identity to pending legacy rows (`db.py:5542-5547`), so those rows join the caches (step 8) in that transaction.
   - A test enumerates the input tables, and fails if a writer of one does not reach `_recompute_derived`. That makes C/D-derived's "by any path" checkable.
10. **Capture kinds (C/D-capture).**
    - `match_mailbox_admission` (`service.py:154-200`) becomes the single owner of the capture decision.
    - Its inputs are the metadata and its location, the vendor index (from `vendor_of`), the follow cache, and whether the gated class is allowed.
    - That last input is decided once per check from `connect_entitlement_decision`, which reads and verifies the license on every call. It is then passed down to capture; the reconcile pass re-evaluates it at every budget check (step 15).
    - `Watcher.check` returns inactive before `_check_active` when an account has no watchlist sender, Gmail label, recovery, or pending message (`service.py:1386-1391`, `1426-1457`). Capture that does not depend on the watchlist needs that predicate to count the other work, so one function owns it: an account with a vendor record, stale coverage (M2.3), or a Sent folder in scope is active. A vendor-only account therefore polls Sent and runs the pass.
    - It returns the first match in C/D-capture's order, or nothing.
    - `thread_follow` resolves the thread read-only before insert. For IMAP that is the component lookup of `_imap_thread_key` (`db.py:3360-3395`), and the message is in a followed thread when any component its id set touches is followed, since those components merge on insert.
    - Provenance selector ids are built by the owner of `exact_sender_selector_id` (`config.py`), which enforces the 512-byte bound of `admission_selector_id` (`db.py:3863`):
      - `vendor_address`: `vendor-address:<address>`;
      - `sent_to_vendor`: `vendor-recipient:<address>`;
      - `thread_follow`: `thread:<sha256 of the thread key>`, since a thread key can itself be 512 bytes.

      The display name is the vendor's. Provenance stays immutable. The vendor address validator accepts an address only when every kind's selector id fits that bound, which the same owner computes from its longest prefix, so an address a vendor operation accepts never fails at capture.
11. **Widening the closed set.**
    - `admission_kind` gains `vendor_address`, `vendor_domain`, `sent_to_vendor`, and `thread_follow`.
    - SQLite cannot change a CHECK in place, so `messages` is rebuilt by SQLite's documented procedure (create, copy, drop, rename, then recreate its indexes and triggers). Earlier migrations rebuilt `automation_fires` this way (`db.py:3484-3506`).
    - `PRAGMA foreign_key_check` and `integrity_check` run inside the migration transaction, which rolls back on any finding.
    - **Backup (decision 3).** Before that transaction, the migration writes `VACUUM INTO` a private backup next to the database, mode 0600, at a fresh path per attempt: `<database>.pre-v31.<UTC timestamp>.bak`. `VACUUM INTO` refuses an existing file, so a fixed path would block every retry after a failure.
      - If the backup cannot be written (for example, the disk is full), the migration does not start, and the engine reports the error with the database unchanged.
      - Once the migrated database has opened and its checks have passed, every `pre-v31` backup is deleted. If the migration fails, the newest backup is kept and older ones are removed, so the database can be restored by hand and the next start retries.
    - The set's other owners widen together:
      - `_validate_admission_provenance` (`db.py:4164-4195`);
      - `AdmissionDecision.kind` (`service.py:127-141`);
      - the Rust `InboxAdmissionKind` (`engine.rs:1139-1142`);
      - the desktop type (`main.ts:175`) and the inbox's admission label (`main.ts:1771-1773`), which today shows every kind but the Gmail label as "watched sender". Each kind gets its own label, from one lookup table.
12. **Gmail recovery** queries `(in:inbox OR in:sent)` (M2.1, so a Sent-only message lost with the history is recovered) under the folders in scope (step 2), and still admits today's kinds only. Vendor mail in a recovered gap is captured by the reconcile pass. A cursor that expired or recovered makes coverage stale (C/D-reconcile).

### M2.3 — The reconcile pass

13. **Storage (schema 32):**
    - `coverage_generation(provider, account_id, mailbox_identity_key, generation, entitlement_active, retention_days, sent_folder)`, C/D-reconcile's counter and the last observed entitlement state, retention, and resolved Sent folder (its scope and, for IMAP, its folder key). One function, `_bump_coverage_generation`, increments it, and every input change calls it in its own transaction: each vendor mutation, for every configured account's row (vendor records are global, and an inactive account must find its coverage stale when it returns); `register_mail_account` and `update_mail_account_identity`, which change a verified identity; the check, when the entitlement state, `retention_days`, or resolved Sent folder it observes differs from the recorded ones (so an edited `config.toml` or `imap_sent_folder` counts, as does `SPECIAL-USE` resolving to another folder or Sent becoming available, and `settings.update` needs no bump of its own); a cursor expiry or recovery; and (M4) an extractor version change;
    - `reconcile_coverage(provider, account_id, mailbox_identity_key, generation, recorded_at)`, the reconciled generation;
    - `reconcile_progress(...)`, which holds the two values frozen when the pass started, the generation and the start time, plus the stage, durable per-folder page tokens, the thread queue, and per-unit attempt and backoff fields. Every bound a pass query uses derives from the frozen start (step 16), so nothing about a resumed query can move with the clock or the configuration. Progress belongs to the generation it froze: when the current generation differs, the check discards it and starts a fresh pass, so a page token is never reused against a changed query.

    Both follow `gmail_recovery_state`'s pattern (`db.py:3756-3833`): frozen inputs, monotonic counters, and a guard trigger.
14. **Staleness** is C/D-reconcile's comparison, made at every check: the current generation differs from `reconcile_coverage`, a followed thread has no watermark, or derived work is pending: a followed-thread message received at or after the cutoff (C/D-scope fetches no older one, so an older retained message is never pending) with no body state, meaning neither a `message_bodies` row nor a `message_body_unavailable` reason (M2.4), or, from M4, without a claim attempt for its current key or with a retryable attempt whose deadline has passed. An unavailable reason is a completed state until step 9 clears it. An account with no record is stale. A stale record starts a pass, or resumes the one frozen at the current generation (step 13).
    - A dry-run check (`Watcher.check(dry_run=True)`) is read-only: it neither runs the pass nor records observed state.
    - When the check observes `retention_days` above the recorded value, or a different Sent folder, it clears every watermark (C/D-reconcile) in the transaction that bumps the generation, so stage (b) syncs from the new cutoff, Sent included.
15. **Budget.** The pass runs after polling, in `_check_active`, under the production check lock (`engine_api.py:390-391`).
    - Each check gets 30 seconds and at most 200 provider calls, the bounds `_run_gmail_recovery` uses (`service.py:1669-1846`).
    - A unit that errors backs off `min(15, 2**n)` minutes (`service.py:1505-1506`) while polling continues.
    - A definitive not-found completes a unit instead of retrying it: a candidate that vanished between its listing and its metadata fetch is skipped, and a stored message whose copy is gone records what step 22 records for it.
    - A continuation token the provider rejects (a Gmail `messages.list` page token expires while a pass is paused) is not retried: the pass drops that folder's saved page state and restarts the query from the frozen bounds (step 16), as Gmail recovery resets `page_token` (`db.py:6422-6425`). Ids already fetched are skipped (step 16), so the restart costs fetches for new mail only.
    - At each budget check the pass re-evaluates the entitlement; when the gated class is no longer allowed, it stops there, keeping its progress.
16. **Stage (a), discovery.** Each candidate that is not stored, or is stored without a stamped location (step 5: assumed, or observed while Sent was out of scope), gets a bounded metadata fetch, then the scope check, then C/D-capture (for a stored one, step 6's location and recipient recording). Bodies are never fetched in this stage.
    - Every query bound derives from the frozen start time (step 13): the upper bound is the start, the lower bound `<cutoff>` is `start - retention_days`, and for IMAP the highest UID of each folder at the start is held with the progress. Every provider query the pass makes, in stage (a) or (b), applies both bounds to every page, including a resumed one, so no result set can move while the pass spans checks. Mail after the bound reaches polling, or the next pass.
    - **Gmail:** `messages.list` with `q = (in:inbox OR in:sent) after:<cutoff> before:<start> (from:"a" OR to:"a" OR cc:"a" ...)` over vendor addresses in batches, with durable page tokens. Each address is quoted, so a local part with spaces or search operators cannot change the query.
    - **Microsoft:** pages each of `inbox` and `sentitems` by `receivedDateTime`: `$filter=receivedDateTime ge <cutoff> and receivedDateTime lt <bound>`, `$orderby=receivedDateTime desc`, `$top=100`, with `$select` of the step-1 fields, matching recipients locally. The next page's `<bound>` is the last item's `receivedDateTime`, so the durable progress is a timestamp, not a server token: it does not expire, it does not shift when a message is removed or moved between checks (a `$skip` offset does), and it has no cap (a filtered delta query returns at most 5,000 messages). Items sharing the boundary timestamp are read with `receivedDateTime eq <bound>` inside one check, in pages of the largest size Graph allows, with `$count=true` on the first page; the bound moves past the timestamp only when the items read equal the reported count, and a differing count (a message removed meanwhile) reads the group again. A seen id is skipped. It never uses `$search`, `$skip`, or delta.
    - **IMAP:** `UID SEARCH UID 1:<highest> SINCE <date> OR FROM a OR TO a CC a`, per folder, in bounded batches. IMAP's `OR` takes exactly two keys, so the keys nest; a test checks the exact command. Every string operand of a search, an address here and a message id in step 17, goes through one encoder, `_search_operand`: an IMAP quoted string with `"` and `\` escaped for ASCII, and for a value outside ASCII `CHARSET UTF-8` with the operand sent as a literal (RFC 3501 section 6.4.4). A `NO [BADCHARSET]` reply completes that operand's unit for the account with a warning, since the server cannot search it, instead of backing off forever.
17. **Stage (b), thread sync.** Each followed thread syncs from its watermark, or from the cutoff when it has none.
    - **Gmail:** one `messages.list` per pass, `q = (in:inbox OR in:sent) after:<oldest watermark, or the cutoff> before:<start>`, with durable page tokens; it returns ids and `threadId`s only. An id whose `threadId` is a followed thread's native id gets the metadata fetch, and nothing else is fetched, so no archived, trashed, or out-of-retention message is ever retrieved (C/D-scope), which `threads.get` could not promise: it returns a thread's every message. The provider thread id is read from the thread's messages (`messages.thread_id`, the native id as the provider gave it), never from the thread key, which `_provider_thread_key` may have hashed (`db.py:3282-3293`) or M1 may have minted. A thread whose messages all lack a native id has no provider thread; it is synced through its own messages only, and never sent to the provider. Microsoft's `conversationId` lookup reads the same column.
    - **Microsoft:** one listing per folder from the oldest followed watermark, or the cutoff, paged exactly as step 16 pages discovery; an item whose `conversationId` is a followed thread's native id is a candidate, the rest are skipped locally, and the folder is the location. No per-thread listing, so no message outside the admitted folders (C/D-scope).
    - **IMAP:** `UID SEARCH UID 1:<highest> SINCE <since date>` in `INBOX` and Sent for `HEADER Message-ID`, `HEADER In-Reply-To`, and `HEADER References` over the component's ids, each through `_search_operand` (step 16), repeated until the component stops growing. `SINCE` is day-granular, so the bound rounds down to the day of the watermark or cutoff. Before that, it drains `imap_reply_header_gaps`:
      - a listed row whose mailbox identity is known gets its reply headers fetched and merged through `_apply_imap_component`;
      - a row whose source is gone, or whose identity is unknown, leaves the list.
    - A thread's watermark advances to the pass's frozen start time, never past the bound its queries applied, and only when the thread the unit synced is the one it started with. An IMAP merge during the unit (`_apply_imap_component`, step 9) clears the survivor's watermark and the unit ends without writing one, since its `SINCE` bound came from the old thread; the merged component then has no watermark and the next unit syncs it from the cutoff.
18. **Stage (c), derived work.** Bodies (M2.4). Claims arrive in M4.
19. **Completion.** When every stage has no work left, `reconcile_coverage` records the generation the pass froze at its start (step 13), in one transaction with clearing the progress row. A change mid-pass bumped the generation past it, so the next check starts another pass.

### M2.4 — Bodies and the followed-thread purge

20. **Storage (schema 33):**
    - `message_bodies(message_id, text, stored_chars, source_chars, fetched_at)`. The date context lives on the message (step 5).
    - `message_body_unavailable(message_id, reason)`, where `reason` is `source_gone` or `outside_folders`.
    - Both are tied to their message by step 5's delete trigger, extended to them, so `delete_message`, `clear_messages`, and the purge need nothing of their own, and no marker outlives its message to be inherited by a recapture.
    - `maintenance_state(key PRIMARY KEY, value, updated_at)`, which holds the `checkpoint_pending` flag of step 23 across restarts.
    - The migration deletes every `reconcile_coverage` row, so each account is stale (step 14) and stage (c) fetches bodies for its already-followed messages.
21. **The storage normalizer** is a new `stored_body_text` in `mime.py`, beside today's normalizer, which stays unchanged for analysis (C/D-body).
    - Quote containers become `>`-prefixed lines:
      - `<blockquote>`;
      - Gmail's `div.gmail_quote`;
      - Outlook's reply header block, `div#divRplyFwdMsg`, alone. Outlook puts no container around the message it introduces, and prefixing everything after the header would also prefix text an author wrote below it. The header lines mark where quoting begins; separating the quoted message from text written after it is segmentation, which C/D-claims assigns to the M4 plan (its Outlook-header case).
    - The storage cap is `MAX_STORED_BODY_CHARS = 200_000`, above `body_char_limit`'s maximum of 100,000 (`config.py:464-469`).
22. **Fetch, in stage (c) only, for followed-thread messages without a body that were received at or after the cutoff** (step 14; a followed thread keeps older messages, which stay bodyless). Each provider gets a new gateway method, `stored_body`:
    - Gmail: `format=full`;
    - Microsoft: HTML, without `prefer_text`, so quote containers survive;
    - IMAP: the existing BODYSTRUCTURE path.

    A logical message may have several recorded locations (step 5). Stage (c) tries each in-scope location in canonical order. The body response itself says where the copy is, in the same snapshot as the text: Gmail's `format=full` returns `labelIds`, Microsoft's `$select` adds `parentFolderId`, and an IMAP UID fetch is bound to the folder selected for it. A body whose snapshot is outside the admitted folders is discarded unstored and that copy is skipped, so no separate check can be overtaken between calls; a copy that is gone is skipped. Only when every location is skipped does the message record `outside_folders` if any copy still exists, else `source_gone` (C/D-body). Other errors back off (step 15).
23. **Purge.**
    - `purge_with_outcome` (`db.py:12273`) keeps its predicate and its place at the start of the check (`service.py:1905`) for messages outside followed threads. A followed thread purges as one unit when its newest logical message is older than the cutoff and the account's coverage is current (C/D-scope, decision D1), so a thread whose newer replies are still unfetched survives a lapse. That purge runs after the check's polling, so a reply that was waiting on the provider cursor is captured, and counted as the newest message, before the decision. Duplicate rows (`logical_of`) go with their logical message.
    - `Store.connection` sets `PRAGMA secure_delete = ON`.
    - Every transaction that deletes a `message_bodies` row sets `checkpoint_pending` in `maintenance_state` (step 20), through a delete trigger on that table, so unfollow (step 9), `delete_message`, `clear_messages`, and the purge all schedule it. A check that finds the flag runs `PRAGMA wal_checkpoint(TRUNCATE)` and checks its result; a busy checkpoint, which a concurrent reader's snapshot can cause, leaves the flag for the next check; a success clears it, and only then is secure deletion of those bodies complete.
    - `_recompute_derived` drops the emptied threads' cache rows and watermarks.
24. **Retention copy.** The settings copy (`main.ts:812`) and README gain: "Followed vendor threads are kept until their newest message is older than this."

## Concurrency

- **Every input write and its `_recompute_derived` share one `BEGIN IMMEDIATE` transaction.** A reader never sees a message without its derived state.
- **The reconcile pass runs only inside the production check lock** (`engine_api.py:390-391`), so it serializes with polling, vendor mutations, and settings changes. Its progress is durable, so a crash resumes it.
- **Locations and coalescing run inside the capture transaction,** so two locations of one logical message cannot race into two rows.

## Failure cases

- **A provider error in the pass** backs off that unit, and polling is unaffected (C/D-reconcile).
- **An IMAP server with neither SPECIAL-USE nor a configured Sent name** has no Sent scope, and health shows it.
- **A body fetch whose source is gone** records `source_gone`, a completed state until a copy arrives or returns (step 9).
- **A failed `messages` rebuild** rolls back the whole migration, so the previous binary still opens the database. Its pre-migration backup stays in place (step 11).
- **No room for the backup:** the migration does not start, the engine reports the error, and the database stays at v30.

## Files touched

- **Engine:** `src/eom_email_watcher/mailbox.py`, `gmail.py`, `microsoft365.py`, `imap.py`, `mime.py`, `db.py`, `service.py`, `engine_api.py`, `config.py`.
- **Desktop:** `desktop/src-tauri/src/engine.rs`, `desktop/src/main.ts`.
- **Docs:** `docs/ENGINE_API.md`, `README.md`.
- **Tests:** `tests/test_gmail.py`, `test_microsoft365.py`, `test_imap.py`, `test_mime.py`, `test_db.py`, `test_service.py`, `test_engine_api.py`, and the Rust and desktop suites.

## Verification (fail-first on each slice's base commit)

**M2.1**
- **Locations.** Each provider records `inbox` and `sent` locations, plus the To/Cc order. A Gmail message with both labels records both rows under one provider id. A Microsoft message in another folder leaves no trace.
- **Date context (M2 required item).** `capture_timezone` is recorded at capture, and a body fetched after the configured zone changes still reads the capture-time zone.
- **IMAP.**
  - `\Sent` is found by SPECIAL-USE and by the configured name. With neither, health shows "Sent mail unavailable".
  - Colliding Inbox and Sent UIDs give two source identities.
  - Selecting Sent leaves Inbox validation correct.
- **Cursors.** Folder cursors advance independently, and a Sent batch failure leaves the Inbox cursor untouched.
- **Gmail checkpoint (M2 required item).** A poll that stops mid-range and resumes misses no `SENT` events.
- **Cleanup.** `delete_message`, `clear_messages`, and the purge remove a message's recipients and locations with it. Deleting the canonical row of a coalesced pair removes both rows and suppresses both source identities.
- **Seen ids.** A Gmail `SENT` label added to a captured message, and a Microsoft message moved into Sent Items, each record the second location on the next poll, with one metadata fetch; once every admitted location is recorded, repeats cost none. A retained row with no recipients gets them from that fetch.
- **Coalescing (M2 required item).**
  - A retained duplicate becomes `logical_of` its canonical row, with nothing deleted.
  - A new second location is recorded on the logical message, not inserted as a row.
- **Sent polling** stops while the gated class is inactive.

**M2.2**
- **Capture kinds.** Each kind is captured in its order:
  - an unwatched vendor address is captured as `vendor_address`;
  - a watched one is captured as `exact_sender`;
  - a Sent message to a vendor is captured as `sent_to_vendor`;
  - a reply in a followed thread is captured as `thread_follow`;
  - a non-vendor message leaves no row.
- **Derived state (C/D-derived acceptance).**
  - Removing a vendor's only address unfollows its threads.
  - Purging a followed thread's last message drops its cache row and watermark.
  - A later Sent location makes the message outbound and re-attributes it.
  - Connecting a mailbox whose address is a vendor address leaves its messages without a vendor.
- **The input-writer test** fails when a writer skips `_recompute_derived`.
- **IMAP follow admission.** A non-vendor reply whose id set touches a followed component and an unfollowed one is captured as `thread_follow`, and the components merge.
- **Survivor by locations.** A second location that sorts before a message's own id makes its component the survivor of a later bridge.
- **Migration backfill.** A database upgraded from M1 with vendor mail already stored has its attribution and follow rows right after the upgrade, with no other change.
- **Thread caches** of two mailbox identities under one account never mix.
- **Selector bounds.** A `thread_follow` capture on a 512-byte thread key succeeds, and the inbox labels each new kind by name.
- **Gating.** With the gated class inactive, only today's kinds are captured.
- **The `messages` rebuild:**
  - a v30 database keeps every row, index, trigger, and provenance value;
  - `foreign_key_check` and `integrity_check` are clean;
  - v30 code refuses v31;
  - the backup is written at mode 0600 and a fresh path before the rebuild, and every backup is deleted after a successful open;
  - a rebuild failure injected after the backup keeps both the v30 database and its backup, and the next start retries with a new backup, keeping only the newest;
  - a backup that cannot be written leaves the database at v30, unchanged.

**M2.3**
- **Staleness, one case each:** no record, a vendor address added, an address removed and re-added, a verified identity changed, raised retention through `settings.update` and through an edited `config.toml`, an entitlement lapse observed and then reactivation, an extractor change, a recovered cursor, a followed thread without a watermark, a message polled into a followed thread (pending derived work), and (M4) a retryable claim attempt whose deadline passed. Many polls with no change never make coverage stale.
- **Mid-pass lapse.** A license removed during a pass stops it at the next budget check, with progress kept.
- **Generation moves mid-pass.** Progress frozen at an older generation is discarded, and the new pass starts clean.
- **Dry run.** A stale account previewed with `dry_run=True` writes no progress, coverage, message, or body row.
- **Mid-pass change.** A vendor added after discovery finished bumps the generation past the pass's record, and the next check starts another pass.
- **Frozen bound.** Mail arriving while a pass spans two checks is not paged twice and not skipped, in discovery and in thread sync; it reaches polling. A synced thread's watermark never passes the bound. A page resumed in a later check uses the pass's cutoff, not the clock's.
- **Native ids.** A followed thread is synced with the native thread id its messages carry; one whose messages have none is synced from its own messages, and `threads.get` is never called with a key. A thread whose long `conversationId` was hashed into its key is still queried by that id.
- **Vendor-only account.** An account with a vendor record and an empty watchlist polls Sent and runs the pass.
- **Sent folder change.** A changed `imap_sent_folder`, or Sent becoming available, makes coverage stale and clears watermarks, so the newly admitted folder is discovered and synced.
- **Rejected page token.** A saved Gmail page token the provider rejects restarts that query from the frozen bounds, and the pass completes.
- **Reappearance.** A message whose every location was recorded and that left both folders is fetched again when polling reports it in one; an IMAP copy arriving for a `source_gone` message supplies its body.
- **Assumed locations.** A retained Gmail message that carried `INBOX` and `SENT` before the upgrade is fetched once by discovery, records both folders and its recipients, and is attributed outbound.
- **Removal mid-pass.** A Microsoft message deleted between two checks of a pass shifts no neighbour out of discovery: the next page resumes from the timestamp bound, and a tie group whose count changed is read again.
- **Aged out.** A followed thread's message older than the cutoff is neither fetched nor pending; the thread's coverage becomes current without it.
- **Merge mid-sync.** An IMAP merge during a thread's sync leaves the survivor without a watermark, and the next unit syncs the merged component from the cutoff.
- **Thread sync scope.** A Gmail thread's archived message and a Microsoft conversation's Deleted Items message are never fetched during sync; an IMAP thread search carries its `SINCE` bound.
- **Vanished candidate.** A candidate deleted between listing and fetch completes its unit, and the pass records coverage.
- **Markers go with the message.** Deleting, clearing, or purging a message in `source_gone` or `outside_folders` leaves no marker, and a recapture starts with no body state.
- **Inactive account.** Adding a vendor address bumps every account's generation; a reactivated account is stale.
- **Identity assignment.** Rows that `reconcile_mailbox_identity` assigns an identity get attribution and follow rows in that transaction.
- **Bottom-posted Outlook reply.** Text written below `div#divRplyFwdMsg` is stored unprefixed.
- **Body snapshot.** A body whose response shows the copy outside the admitted folders is not stored.
- **Selector bound.** A vendor address the vendor operation accepts always yields a storable selector id.
- **One inbox entry.** After the upgrade, `inbox.query` lists a coalesced message once, and the duplicate's summary is still stored.
- **Lapse and return.** A message captured from the Inbox while Sent was out of scope has no stamped location; the next pass after reactivation fetches it once and records its Sent folder and recipients.
- **Search operands.** A vendor address with a quote, a brace, or a non-ASCII local part is searched through the encoder on IMAP and quoted on Gmail; a `BADCHARSET` reply completes the unit.
- **Unfollow drops the watermark**, so a refollowed thread is stale and resyncs.
- **Raised retention clears every watermark,** and the IMAP search command nests its `OR` keys exactly.
- **Discovery.**
  - Gmail pages with durable tokens across checks (M2 required item).
  - Microsoft pages each folder by `receivedDateTime` with no `$search` (M2 required item).
  - Every Gmail candidate gets metadata before its scope check, and no body in this stage (M2 required item).
- **Thread sync.**
  - Gmail thread sync is a bounded `messages.list` matched on `threadId`; `threads.get` is never called (M2 required item).
  - The IMAP header search repeats until the component stops growing (M2 required item).
  - The reply-header gap list drains and merges.
- **Budget.** A pass stops at its budget and resumes on the next check. An inactive class mid-pass stops it with progress kept.
- **Contract reconcile scenarios:** a vendor captured by polling syncs earlier messages; a Connect lapse, then reactivation, recovers the lapse; raising retention from 30 to 180 days fetches the older messages.

**M2.4**
- **Quote containers (M2 required item).** `<blockquote>`, Gmail's quote block, and Outlook's reply header each become `>` lines. `stored_body_text` leaves today's analysis normalization unchanged.
- **Body states.** A stored body, a partial body (with its marker counts), `source_gone` only after every recorded location is gone, `outside_folders` for a message archived between stages (b) and (c) while another copy is fetched when one exists, and not stored yet. A message moved back into scope loses `outside_folders` and gets its body on the next pass. An unavailable reason does not keep coverage stale, so the account's aged followed threads can still purge.
- **Date context after upgrade.** A retained row has no context, and a relative date in its later-extracted claims is rejected rather than resolved.
- **Upgrade.** Installing M2.4 on an account with current coverage makes it stale, and stage (c) fetches its followed messages' bodies.
- **Purge.**
  - A followed thread stays until its newest message is past the cutoff, then purges whole.
  - Bodies go with it.
  - `secure_delete` is on for the connection, and deleting bodies through unfollow, `delete_message`, `clear_messages`, or the purge each set the pending checkpoint flag, which survives a restart; the next check truncates the WAL, and a busy checkpoint leaves the flag for the one after.
  - A followed thread whose newest stored message is past the cutoff is kept while coverage is stale, and purged once a pass completes.
  - A reply arriving between checks to such a thread is captured by that check's polling, and the thread survives it.
- **No new bodies.** A watched non-vendor message, or a label-only message, outside any followed thread stores no body.

**Gates, every slice:** `uv run ruff check .`, the full `uv run pytest` including the contract seam check, desktop `node --test` and `tsc`, and the Rust suite (`cargo fmt`, `clippy -D warnings`, `cargo test`).
