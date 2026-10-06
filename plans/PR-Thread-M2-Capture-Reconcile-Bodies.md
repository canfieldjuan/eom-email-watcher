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
   - **Gmail:** the one history cursor already returns `SENT` events (no `labelId` filter, `gmail.py:838-848`). Only the admission gate changes (step 9). C/D-reconcile's checkpoint rule holds as long as the cursor advances only after the whole batch (`service.py:2152-2158`), which is unchanged.
   - **Microsoft:** a second delta link on `mailFolders/sentitems`. `_DELTA_PATH` already accepts any folder (`microsoft365.py:46-51`).
   - **IMAP:** `LIST` with RFC 6154 `SPECIAL-USE` finds `\Sent`, falling back to a configured `imap_sent_folder` name.
     - With neither, the account has no Sent scope, and health reports "Sent mail unavailable" (C/D-scope).
     - Each select resets the cached `_eom_uid_validity` and `_eom_message_count` (`imap.py:421-434`).
3. **Folder cursors.** A new `mailbox_folder_state(provider, account_id, mailbox_identity_key, folder, cursor, last_success_at)` holds the Sent cursors. `mailbox_state` stays the Inbox (or Gmail mailbox-wide) cursor, so no existing cursor migrates. A folder cursor advances only on its own batch (C/D-reconcile).
4. **IMAP Sent identity.** Sent ids are `eom-imap-sent-v1:<mailbox_id>:<folder_sha256>:<UIDVALIDITY>:<UID>`, the folder token of C/D-identity. Inbox ids are unchanged. `_checked_uid` validates against the folder the id names (`imap.py:1500-1507`).
5. **Storage (schema 30):**
   - `message_recipients(message_id, field, position, address)`, written at capture for every captured message. Outbound attribution reads it (C/D-attribution).
   - `message_locations(message_id, location, provider_message_id, recorded_at)`. Its primary key is `(provider_message_id)` per account and identity, so one source identity is recorded once.
   - `messages.logical_of`, NULL for a logical message. It holds the canonical row's id for a row that duplicates one.
   - Migration:
     - every retained row gets an `inbox` location;
     - retained rows sharing a logical identity (C/D-identity) are coalesced: the canonical-smallest row is the logical message, the others point to it through `logical_of`, and their locations move to it;
     - no row is deleted, so per-row summaries, attachments, and automation runs survive (the M2 required item).
6. **A second location** of a captured logical identity is recorded on the logical message, inside the capture transaction (C/D-identity). It is not a new row, and it gets no admission or analysis.
7. **Health.** `health.get` reports each account's Sent scope: available, unavailable, or not polled. The desktop shows "Sent mail unavailable" on that account.

### M2.2 — Derived state and capture kinds

8. **Derived storage (schema 31):**
   - `message_derived(message_id, direction, vendor_id, match)`, the stored attribution of C/D-attribution, through `vendor_of` with its match (C/D-vendor);
   - `thread_follow(provider, account_id, thread_key, followed, owner_vendor_id)`, the C/D-follow cache;
   - `thread_watermarks(provider, account_id, thread_key, synced_through)`, for C/D-reconcile.
9. **One recompute function, `_recompute_derived(db, changed)`, owns C/D-derived.**
   - It takes the changed inputs: message ids, thread keys, vendor addresses, or identities. From them it finds the affected logical messages, then their threads.
   - It recomputes attribution, then the follow cache. It deletes the cache row and watermark of an empty thread (C/D-follow), and deletes the bodies a newly unfollowed thread may not keep (M2.4, C/D-body).
   - Every writer of an input calls it in its own transaction:
     - capture and location recording;
     - `delete_message` and `clear_messages`;
     - the purge;
     - `_apply_imap_component`;
     - every vendor mutation;
     - `register_mail_account` and `update_mail_account_identity`.
   - A test enumerates the input tables, and fails if a writer of one does not reach `_recompute_derived`. That makes C/D-derived's "by any path" checkable.
10. **Capture kinds (C/D-capture).**
    - `match_mailbox_admission` (`service.py:154-200`) becomes the single owner of the capture decision.
    - Its inputs are the metadata and its location, the vendor index (from `vendor_of`), the follow cache, and whether the gated class is allowed.
    - That last input is decided once per check from `connect_entitlement_decision`, which reads and verifies the license on every call. It is then passed down.
    - It returns the first match in C/D-capture's order, or nothing.
    - `thread_follow` resolves the thread key read-only before insert. For IMAP that is the component lookup of `_imap_thread_key` (`db.py:3360-3395`).
    - Provenance selector ids:
      - `vendor_address`: `vendor-address:<address>`;
      - `sent_to_vendor`: `vendor-recipient:<address>`;
      - `thread_follow`: `thread:<thread_key>`.

      The display name is the vendor's. Provenance stays immutable.
11. **Widening the closed set.**
    - `admission_kind` gains `vendor_address`, `vendor_domain`, `sent_to_vendor`, and `thread_follow`.
    - SQLite cannot change a CHECK in place, so `messages` is rebuilt by SQLite's documented procedure (create, copy, drop, rename, then recreate its indexes and triggers). Earlier migrations rebuilt `automation_fires` this way (`db.py:3484-3506`).
    - `PRAGMA foreign_key_check` and `integrity_check` run inside the migration transaction, which rolls back on any finding.
    - The set's other owners widen together:
      - `_validate_admission_provenance` (`db.py:4164-4195`);
      - `AdmissionDecision.kind` (`service.py:127-141`);
      - the Rust `InboxAdmissionKind` (`engine.rs:1139-1142`);
      - the desktop type (`main.ts:175`).
12. **Gmail recovery** still admits today's kinds only, because its query is `in:inbox` (`gmail.py:1053`). Vendor mail in a recovered gap is captured by the reconcile pass. A cursor that expired or recovered makes coverage stale (C/D-reconcile).

### M2.3 — The reconcile pass

13. **Storage (schema 32):**
    - `reconcile_coverage(provider, account_id, mailbox_identity_key, vendor_set_digest, retention_days, entitlement_active, extractor_version, cursor_epoch, recorded_at)`;
    - `reconcile_progress(...)`, which holds the stage, durable per-folder page tokens, the thread queue, and per-unit attempt and backoff fields.

    Both follow `gmail_recovery_state`'s pattern (`db.py:3756-3833`): frozen inputs, monotonic counters, and a guard trigger.
14. **Staleness** is C/D-reconcile's comparison of the current state with `reconcile_coverage`, made at every check. A stale record starts a pass, or resumes it if a pass is already underway.
15. **Budget.** The pass runs after polling, in `_check_active`, under the production check lock (`engine_api.py:390-391`).
    - Each check gets 30 seconds and at most 200 provider calls, the bounds `_run_gmail_recovery` uses (`service.py:1669-1846`).
    - A unit that errors backs off `min(15, 2**n)` minutes (`service.py:1505-1506`) while polling continues.
    - The pass stops at its next budget check when the gated class is no longer allowed, keeping its progress.
16. **Stage (a), discovery.** Each candidate gets a bounded metadata fetch, then the scope check, then C/D-capture. Bodies are never fetched in this stage.
    - **Gmail:** `messages.list` with `q = (in:inbox OR in:sent) after:<cutoff> (from:a OR to:a OR cc:a ...)` over vendor addresses in batches, with durable page tokens.
    - **Microsoft:** pages each of `inbox` and `sentitems` with `$filter=receivedDateTime ge <cutoff>` and `$select` of the step-1 fields, and matches recipients locally. It never uses `$search`.
    - **IMAP:** `UID SEARCH SINCE <date> OR FROM a TO a CC a`, per folder, in bounded batches.
17. **Stage (b), thread sync.** Each followed thread syncs from its watermark, or from the cutoff when it has none.
    - **Gmail:** `threads.get(format=metadata)`. It is used only for threads already stored.
    - **Microsoft:** `/me/messages?$filter=conversationId eq '<id>' and receivedDateTime ge <since>`, then the step-1 location check.
    - **IMAP:** `UID SEARCH` in `INBOX` and Sent for `HEADER Message-ID`, `HEADER In-Reply-To`, and `HEADER References` over the component's ids, repeated until the component stops growing. Before that, it drains `imap_reply_header_gaps`:
      - a listed row whose mailbox identity is known gets its reply headers fetched and merged through `_apply_imap_component`;
      - a row whose source is gone, or whose identity is unknown, leaves the list.
    - A thread's watermark advances to the start time of the check that finished its sync.
18. **Stage (c), derived work.** Bodies (M2.4). Claims arrive in M4.
19. **Completion.** When every stage has no work left, `reconcile_coverage` records the current state, in one transaction with clearing the progress row.

### M2.4 — Bodies and the followed-thread purge

20. **Storage (schema 33):**
    - `message_bodies(message_id, text, stored_chars, source_chars, received_at, timezone, captured_at)`. A delete trigger ties it to its message.
    - `message_body_unavailable(message_id, reason)`, where `reason` is `source_gone` or `outside_folders`.
21. **The storage normalizer** is a new `stored_body_text` in `mime.py`, beside today's normalizer, which stays unchanged for analysis (C/D-body).
    - Quote containers become `>`-prefixed lines:
      - `<blockquote>`;
      - Gmail's `div.gmail_quote`;
      - Outlook's reply header block, `div#divRplyFwdMsg` and everything after it.
    - The storage cap is `MAX_STORED_BODY_CHARS = 200_000`, above `body_char_limit`'s maximum of 100,000 (`config.py:464-469`).
22. **Fetch, in stage (c) only, for followed-thread messages without a body.** Each provider gets a new gateway method, `stored_body`:
    - Gmail: `format=full`;
    - Microsoft: HTML, without `prefer_text`, so quote containers survive;
    - IMAP: the existing BODYSTRUCTURE path.

    A source that is gone records `source_gone`; other errors back off (step 15). The date context (C/D-body) is recorded at capture.
23. **Purge.**
    - `purge_with_outcome` (`db.py:12273`) keeps its predicate for messages outside followed threads. A followed thread purges as one unit when its newest logical message is older than the cutoff (C/D-scope, decision D1). Duplicate rows (`logical_of`) go with their logical message.
    - `Store.connection` sets `PRAGMA secure_delete = ON`.
    - A purge that deleted rows ends with `PRAGMA wal_checkpoint(TRUNCATE)`, so deleted pages leave the WAL too.
    - `_recompute_derived` drops the emptied threads' cache rows and watermarks.
24. **Retention copy.** The settings copy (`main.ts:812`) and README gain: "Followed vendor threads are kept until their newest message is older than this."

## Concurrency

- **Every input write and its `_recompute_derived` share one `BEGIN IMMEDIATE` transaction.** A reader never sees a message without its derived state.
- **The reconcile pass runs only inside the production check lock** (`engine_api.py:390-391`), so it serializes with polling, vendor mutations, and settings changes. Its progress is durable, so a crash resumes it.
- **Locations and coalescing run inside the capture transaction,** so two locations of one logical message cannot race into two rows.

## Failure cases

- **A provider error in the pass** backs off that unit, and polling is unaffected (C/D-reconcile).
- **An IMAP server with neither SPECIAL-USE nor a configured Sent name** has no Sent scope, and health shows it.
- **A body fetch whose source is gone** records `source_gone` and is never retried.
- **A failed `messages` rebuild** rolls back the whole migration, so the previous binary still opens the database.

## Files touched

- **Engine:** `src/eom_email_watcher/mailbox.py`, `gmail.py`, `microsoft365.py`, `imap.py`, `mime.py`, `db.py`, `service.py`, `engine_api.py`, `config.py`.
- **Desktop:** `desktop/src-tauri/src/engine.rs`, `desktop/src/main.ts`.
- **Docs:** `docs/ENGINE_API.md`, `README.md`.
- **Tests:** `tests/test_gmail.py`, `test_microsoft365.py`, `test_imap.py`, `test_mime.py`, `test_db.py`, `test_service.py`, `test_engine_api.py`, and the Rust and desktop suites.

## Verification (fail-first on each slice's base commit)

**M2.1**
- **Locations.** Each provider records `inbox` and `sent` locations, plus the To/Cc order. A Gmail message with both labels records both. A Microsoft message in another folder leaves no trace.
- **IMAP.**
  - `\Sent` is found by SPECIAL-USE and by the configured name. With neither, health shows "Sent mail unavailable".
  - Colliding Inbox and Sent UIDs give two source identities.
  - Selecting Sent leaves Inbox validation correct.
- **Cursors.** Folder cursors advance independently, and a Sent batch failure leaves the Inbox cursor untouched.
- **Gmail checkpoint (M2 required item).** A poll that stops mid-range and resumes misses no `SENT` events.
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
- **Gating.** With the gated class inactive, only today's kinds are captured.
- **The `messages` rebuild:**
  - a v30 database keeps every row, index, trigger, and provenance value;
  - `foreign_key_check` and `integrity_check` are clean;
  - v30 code refuses v31.

**M2.3**
- **Staleness, one case each:** a new vendor address, raised retention, entitlement reactivation, an extractor change, a recovered cursor, and a followed thread without a watermark. Many polls with no change never make coverage stale.
- **Discovery.**
  - Gmail pages with durable tokens across checks (M2 required item).
  - Microsoft pages each folder by `receivedDateTime` with no `$search` (M2 required item).
  - Every Gmail candidate gets metadata before its scope check, and no body in this stage (M2 required item).
- **Thread sync.**
  - Gmail `threads.get` is called only for stored threads (M2 required item).
  - The IMAP header search repeats until the component stops growing (M2 required item).
  - The reply-header gap list drains and merges.
- **Budget.** A pass stops at its budget and resumes on the next check. An inactive class mid-pass stops it with progress kept.
- **Contract reconcile scenarios:** a vendor captured by polling syncs earlier messages; a Connect lapse, then reactivation, recovers the lapse; raising retention from 30 to 180 days fetches the older messages.

**M2.4**
- **Quote containers (M2 required item).** `<blockquote>`, Gmail's quote block, and Outlook's reply header each become `>` lines. `stored_body_text` leaves today's analysis normalization unchanged.
- **Body states.** A stored body, a partial body (with its marker counts), `source_gone`, `outside_folders`, and not stored yet.
- **Date context (M2 required item).** A body keeps its stored zone after the configured zone changes.
- **Purge.**
  - A followed thread stays until its newest message is past the cutoff, then purges whole.
  - Bodies go with it.
  - `secure_delete` is on for the connection, and the WAL is truncated after a purge.
- **No new bodies.** A watched non-vendor message, or a label-only message, outside any followed thread stores no body.

**Gates, every slice:** `uv run ruff check .`, the full `uv run pytest` including the contract seam check, desktop `node --test` and `tsc`, and the Rust suite (`cargo fmt`, `clippy -D warnings`, `cargo test`).
