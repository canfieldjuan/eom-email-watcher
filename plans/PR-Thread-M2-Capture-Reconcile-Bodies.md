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

What a slice captures is what its capture decision admits at the time; mail that a later slice would have admitted, and that ages past the cutoff before that slice ships, is gone, as C/D-scope says of mail that aged before capture. Deferring a cursor would not change that: only M2.3's discovery looks back, and only to the cutoff. Two consequences are stated so no slice claims more: M2.1's Sent polling admits today's kinds only, and until M2.3 can prove coverage current no followed thread is purged at all. M2.1 has no follow cache and keeps today's per-message retention rule, applied per logical message (step 5); M2.2 keeps every followed thread whole, whatever its age, since a reply that M1 or M2.1 ignored can only be recovered by M2.3's sync (step 23).

**Out of scope:**
- The thread view and its CSP (M3).
- Claims (M4).
- Domains and suggestions (M5): `vendor_domain` is added to the closed set now, but nothing produces it until M5.
- Any read operation over threads or bodies (M3 adds them).

## Mechanics

### M2.1 — Sent, recipients, locations

1. **Metadata gains** `to`, `cc` (header order kept), `rfc_message_id` for every provider, and `location` (`inbox` or `sent`).
   - **Gmail.** `metadataHeaders` adds `To, Cc, Message-ID, In-Reply-To, References`. The location comes from `labelIds`, already parsed (`gmail.py:465-487`). A message carrying both labels records both locations; a response whose labels name no admitted folder is discarded, as C/D-scope says of every fetch response.
   - **Microsoft.** `$select` adds `toRecipients, ccRecipients, internetMessageId, parentFolderId`. The metadata URL is `/me/messages/{id}`, since ids are mailbox-wide; `parentFolderId` in the same response maps to a location through the well-known `inbox` and `sentitems` ids, resolved lazily and once per gateway, Inbox first, so an Inbox check never asks for Sent Items. A response outside the admitted folders is discarded, as C/D-scope says of every fetch response.
   - **IMAP.** Recipients are a header item of their own, `TO CC`, under their own 64 KiB bound. An optional item that reaches its bound degrades to nothing, the one rule the reply-header item already follows, so a long recipient list never invalidates the message. The location is the selected folder.
2. **Sent folders,** each polled only while the gated class is allowed (C/D-ops). A dry run previews Sent as it previews the Inbox: it reads, and writes neither scope nor cursor.
   - Sent capture is gated, so polling Sent while the class is inactive could capture nothing. The gap is recovered on reactivation, because coverage compares the entitlement with its record (C/D-reconcile).
   - **Gmail:** the one history cursor already returns `SENT` events (no `labelId` filter, `gmail.py:838-848`). A `SENT`-only message is admitted, and its location recorded, only while the gated class is allowed: the folders in scope are decided once per check from `connect_entitlement_decision` and passed to every admission and insert, which step 10 keeps as the capture decision's input. C/D-reconcile's checkpoint rule holds as long as the cursor advances only after the whole batch (`service.py:2152-2158`), which is unchanged.
   - **Microsoft:** a second delta link on `mailFolders/sentitems`, with its own initial cursor and stale-cursor recovery on that folder (`sent_initial_cursor`, `sent_changes_since`, `sent_recover_since`), so no Inbox link is ever stored as the Sent state. `_DELTA_PATH` already accepts any folder (`microsoft365.py:46-51`).
   - **IMAP:** `LIST` with RFC 6154 `SPECIAL-USE` finds `\Sent`, falling back to a configured `imap_sent_folder` name. A listing that fails says nothing about `\Sent`: the Sent poll retries it later, and the fallback is never selected in its place.
     - With neither, the account has no Sent scope, and health reports "Sent mail unavailable" (C/D-scope).
     - Each select resets the cached `_eom_uid_validity` and `_eom_message_count` (`imap.py:421-434`).
3. **Folder cursors.** A new `mailbox_folder_state(provider, account_id, mailbox_identity_key, folder, cursor, last_success_at)` holds the Sent cursors. `mailbox_state` stays the Inbox (or Gmail mailbox-wide) cursor, so no existing cursor migrates. A folder cursor advances only on its own batch (C/D-reconcile).
4. **IMAP Sent identity.** Sent ids are `eom-imap-sent-v1:<mailbox_id>:<folder_sha256>:<UIDVALIDITY>:<UID>`, the folder token of C/D-identity. Inbox ids are unchanged. `_checked_uid` validates against the folder the id names (`imap.py:1500-1507`).
5. **Storage (schema 30):**
   - `message_recipients(message_id, field, position, address)`, written at capture for every captured message. Outbound attribution reads it (C/D-attribution).
   - `message_locations(message_id, provider, account_id, mailbox_identity_key, provider_message_id, location, recorded_at)`, primary key `(provider, account_id, mailbox_identity_key, provider_message_id, location)`. One source identity is recorded once per location, so a Gmail message with both labels has two rows under one id. `recorded_at` says whether the source identity's latest observation was complete: folders and headers both. It is set when the folders were observed with every admitted folder in scope on a row whose headers were fetched; it is NULL when the row was assumed by the migration or when the latest observation came with Sent out of scope (step 2), which clears an earlier stamp, since neither says whether the message is also in Sent. A retained row, whose headers were never fetched, stays unstamped whatever a change record says about its folders, until the fetch that brings its recipients (discovery, step 16). A gap in polling (C/D-reconcile) clears every stamp of the account, since folder changes during it went unobserved.
   - `messages.logical_of`, NULL for a logical message. It holds the canonical row's id for a row that duplicates one.
   - A trigger deletes a message's recipients and locations with it, so `delete_message`, `clear_messages`, and the purge leave no orphan rows, and a stale location key never blocks a later recapture.
   - A source identity is known through its row, a recorded location, or a suppression (`has_seen_message`), so a copy recorded as a location is never fetched or analyzed again. A logical message is read through any of its source identities, canonical first: a copy that is gone is not the message being gone, and a message whose analysis was skipped because its only copy was gone is queued again when another copy is recorded.
   - Deletion works on the logical message as one unit: `delete_message`, `clear_messages`, and the purge remove the canonical row and every row pointing to it through `logical_of` together, and the first two record a suppression for each recorded source identity, so no copy is recaptured after a cursor recovery. The purge takes a unit only once every row of it has expired, so a copy that crosses the cutoff first does not strand the others.
   - `messages.capture_timezone`, the configured zone at capture. With `received_at`, it is C/D-body's date context, recorded here because a body can arrive in a later check (step 22). Retained rows keep it NULL: their context is unavailable, which C/D-claims already defines (dates without context are rejected).
   - Migration:
     - every retained row gets an `inbox` location with no `recorded_at`: the pre-M2 poller admitted from the Inbox only, so the folder is assumed, not observed. The first fetch of the message with Sent in scope (step 6, or discovery in step 16) records the folders it observes and stamps the row, so a Gmail message that carried `INBOX` and `SENT` all along gets both, and its recipients;
     - retained rows sharing a logical identity (C/D-identity) are coalesced: the row in the most advanced processing state (notified, then analyzed, then pending; ties to the smallest source identity) is the logical message, the others point to it through `logical_of`, and their locations move to it, so the message is neither analyzed nor notified twice and no completed result is hidden behind a pending copy;
     - no row is deleted, so per-row summaries, attachments, and automation runs survive (the M2 required item);
     - the rows that are messages to readers are defined once, as the `logical_messages` view, recreated after every migration. Every reader that treats rows as messages selects from it: the inbox listing (`query_inbox`, which `recent` wraps), the pending and delivery queues, the notification intents and their count, and the pending-work check, so an upgraded duplicate is listed, analyzed, and notified only through its canonical row. Paths that act on storage units (delete, suppress, locations, the trigger) read the table. The duplicate's own summary and attachments stay stored under its row, reachable through its logical message (M3's thread view lists copies).
6. **A second location** of a captured logical identity is recorded on the logical message, inside the capture transaction (C/D-identity). It is not a new row, and it gets no admission or analysis.
   - Gmail and Microsoft ids span folders, so a known id comes back through polling when its labels or folder change (a `SENT` label added, a move into Sent Items, a star). The change record names folders the message is in, and polling records the ones in scope from it and fetches nothing. Microsoft's delta names the folder it was read from, the whole set, since a message is in one folder. Gmail's history is a delta: an addition's `labelIds` say which labels were added and the nested message carries only its id, so a record is a whole set only in the rare case it carries the message's `labelIds`. A record that adds no admitted folder, a star or a read, says nothing about folders and changes nothing. One that adds an admitted folder records it, and since it does not say where else the message is, the observation is incomplete and clears the stamp (step 5), so discovery (step 16) observes the message again; only a whole set under full scope stamps a captured row's source rows, and a retained row stays unstamped either way. Removals are not polled: a recorded location means the message was seen there, and whether it is still there is derived work (stage (c)). Sent is in scope only while the gated class is allowed (step 2). The event is a changed input of that message (step 9): it is in an admitted folder again, so a completed unavailable reason cannot stand. A retained row's missing recipients arrive with discovery's fetch (step 16), since the migration cannot reconstruct To/Cc and D-attribution reads them once a Sent location turns the message outbound. IMAP copies have folder-tokened ids (step 4) and reach the same transaction through their logical identity.
   - IMAP survivor ranking (`_imap_thread_key`, M1) reads each logical message's smallest source identity across all its recorded locations (C/D-identity's canonical order), so a later location can decide which component survives a merge.
7. **Health.** `health.get` reports each account's Sent scope as the last check recorded it: available, unavailable, or not polled, the last whenever Sent was out of scope at that check, whatever an earlier active check found. The desktop shows "Sent mail unavailable" on that account.

### M2.2 — Derived state and capture kinds

8. **Derived storage (schema 31):**
   - `message_derived(message_id, direction, vendor_id, match)`, the stored attribution of C/D-attribution, through `vendor_of` with its match (C/D-vendor);
   - `thread_follow(provider, account_id, mailbox_identity_key, thread_key, followed, owner_vendor_id)`, the C/D-follow cache;
   - `thread_watermarks(provider, account_id, mailbox_identity_key, thread_key, synced_through)`, for C/D-reconcile.

   Provider thread ids mean something only within one mailbox, and an account's mailbox can change, so both caches are keyed by the mailbox identity. A legacy row without one contributes under the account's proven legacy identity, else to no cache, as M1 keyed it.
9. **One recompute function, `_recompute_derived(db, changed)`, owns C/D-derived.**
   - It takes the changed inputs: message ids, thread keys, vendor addresses, identities, or (M4) the extractor version, whose change supersedes the previous key's claims as C/D-derived says. From them it finds the affected logical messages, then their threads.
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
      - `vendor_address`: `vendor-address:<sha256 of the address>`;
      - `sent_to_vendor`: `vendor-recipient:<sha256 of the address>`, digests like `thread_follow`'s, since an address M1 accepted may already fill the bound under `sender:` and the message carries the address itself;
      - `thread_follow`: `thread:<sha256 of the thread key>`, since a thread key can itself be 512 bytes.

      The display name is the vendor's. Provenance stays immutable.
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
12. **Gmail recovery** queries `(in:inbox OR in:sent)` (M2.1, so a Sent-only message lost with the history is recovered) under the folders in scope (step 2), and still admits today's kinds only. A candidate that is already stored gets no fetch; the lost interval may have moved it and the search does not say where it is, so its observation is recorded as incomplete (step 5) and discovery observes it again once the recovery makes coverage stale. The folders in scope are told to the gateway once per check, so the recovery query, like every query a gateway makes, reads no gated folder; the recovery state records the scope its saved page belongs to, and a page saved under another scope is restarted, not reused, in a preview too. Vendor mail in a recovered gap is captured by the reconcile pass. A cursor that expired or recovered makes coverage stale (C/D-reconcile).

### M2.3 — The reconcile pass

13. **Storage (schema 32):**
    - `coverage_generation(provider, account_id, mailbox_identity_key, generation, entitlement_active, retention_days, sent_folder)`, C/D-reconcile's counter and the last observed entitlement state, retention, and resolved Sent folder (its scope and, for IMAP, its folder key). One function, `_bump_coverage_generation`, increments it, and every input change calls it in its own transaction: each vendor mutation, for every configured account's row (vendor records are global, and an inactive account must find its coverage stale when it returns); `register_mail_account` and `update_mail_account_identity`, which change a verified identity; the check, when the entitlement state, `retention_days`, or resolved Sent folder it observes differs from the recorded ones (so an edited `config.toml` or `imap_sent_folder` counts, as does `SPECIAL-USE` resolving to another folder or Sent becoming available, and `settings.update` needs no bump of its own); a cursor expiry or recovery; and (M4) an extractor version change;
    - `reconcile_coverage(provider, account_id, mailbox_identity_key, generation, recorded_at)`, the reconciled generation;
    - `reconcile_progress(...)`, which holds the two values frozen when the pass started, the generation and the start time, plus the stage, durable per-folder page tokens, the thread queue, and per-unit attempt and backoff fields. Every bound a pass query uses derives from the frozen start (step 16), so nothing about a resumed query can move with the clock or the configuration. Progress belongs to the generation it froze: when the current generation differs, the check discards it and starts a fresh pass, so a page token is never reused against a changed query.

    Both follow `gmail_recovery_state`'s pattern (`db.py:3756-3833`): frozen inputs, monotonic counters, and a guard trigger.
14. **Staleness** is C/D-reconcile's comparison, made at every check: the current generation differs from `reconcile_coverage`, a followed thread has no watermark, or derived work is pending: a followed-thread message with no body state, meaning neither a `message_bodies` row nor a `message_body_unavailable` reason (M2.4), when it was received at or after the cutoff (C/D-scope fetches no older one, so an older message that never got a body is not pending); or, from M4, a message with a stored body and no claim attempt for its current key, or a retryable attempt whose deadline has passed, whatever its age, since claim work reads stored text and fetches nothing. An unavailable reason is a completed state until step 9 clears it. An account with no record is stale. A stale record starts a pass, or resumes the one frozen at the current generation (step 13).
    - A dry-run check (`Watcher.check(dry_run=True)`) is read-only: it neither runs the pass nor records observed state.
    - When the check observes `retention_days` above the recorded value, a different Sent folder, an entitlement lapse that ended, or a cursor that expired or recovered, it clears every watermark (C/D-reconcile) in the transaction that bumps the generation, so stage (b) syncs from the cutoff: mail that polling could not admit during such a gap may predate a watermark. A gap also clears the account's location stamps (step 5), so stage (a) fetches its stored messages at or after the cutoff again and stamps them (step 16); a retained message older than the cutoff is fetched by no stage, and its locations are stamped again when polling next reports it (step 6). M2.1 already clears them on the Inbox and Sent cursor recoveries.
15. **Budget.** The pass runs after polling, in `_check_active`, under the production check lock (`engine_api.py:390-391`).
    - Each check gets 30 seconds and at most 200 provider calls, the bounds `_run_gmail_recovery` uses (`service.py:1669-1846`).
    - A unit that errors backs off `min(15, 2**n)` minutes (`service.py:1505-1506`) while polling continues.
    - A definitive not-found completes a unit instead of retrying it: a candidate that vanished between its listing and its metadata fetch is skipped, and a stored message whose copy is gone records what step 22 records for it.
    - A continuation token the provider rejects (a Gmail `messages.list` page token, or a Microsoft delta token, expires while a pass is paused) is not retried: the pass drops that folder's saved page state and restarts the query from the frozen bounds (step 16), as Gmail recovery resets `page_token` (`db.py:6422-6425`). Ids already fetched are skipped (step 16), so the restart costs fetches for new mail only.
    - At each budget check the pass re-evaluates the entitlement; when the gated class is no longer allowed, it stops there, keeping its progress.
16. **Stage (a), discovery.** Each candidate that is not stored, or is stored without a stamped location (step 5: assumed, or observed while Sent was out of scope), gets a bounded metadata fetch, then the scope check, then C/D-capture (for a stored one, step 6's location and recipient recording). Bodies are never fetched in this stage.
    - Every query bound derives from the frozen start time (step 13): the upper bound is the start, the lower bound `<cutoff>` is `start - retention_days`, and for IMAP the highest UID of each folder at the start is held with the progress. Every provider query the pass makes, in stage (a) or (b), applies both bounds to every page, including a resumed one, so no result set can move while the pass spans checks. Mail after the bound reaches polling, or the next pass. No stage fetches a message received before the cutoff (C/D-scope): a stored row older than it, wherever a stage finds it, completes without a provider request.
    - **Gmail:** `messages.list` with `q = (in:inbox OR in:sent) after:<cutoff> before:<start> (from:"a" OR to:"a" OR cc:"a" ...)` over vendor addresses in batches, with durable page tokens. Each address is quoted, so a local part with spaces or search operators cannot change the query.
    - **Microsoft:** a delta round per folder, `mailFolders/{inbox|sentitems}/messages/delta?changeType=created` selecting only `id`, `receivedDateTime`, and `conversationId`: a listing, which fetches no message (C/D-scope), so whatever items Graph includes beyond a filter (it documents that a delta may return events a filter does not match, and caps a filtered delta at 5,000 messages), no header of a pre-cutoff message is ever read. The cutoff and the start bound apply to each item locally; `@removed` entries are ignored; an id in the window that is not stored, or is stored without a stamp (step 5), gets the step-1 metadata fetch through its folder, where the recipient match happens. The delta's `@odata.nextLink` is the durable progress: a server-side position that stays valid when a message is removed, moved, or added between checks, which a listing's `$skip` offset does not, and a check resumes it within its budget (step 15). A round lists the whole folder and a pass fetches each in-window message once, so a pass costs the window's size in fetches; passes run only when an input changed (step 14). It never uses `$search`, `$filter`, or `$skip`.
    - **IMAP:** `UID SEARCH UID 1:<highest> SINCE <date> OR FROM a OR TO a CC a`, per folder, in bounded batches. IMAP's `OR` takes exactly two keys, so the keys nest; a test checks the exact command. IMAP date keys compare the date text of `INTERNALDATE` in the message's own zone, disregarding time and zone, so `SINCE` starts one calendar day before the cutoff's UTC date, and for every UID on the two boundary days the pass reads `INTERNALDATE` alone first, with no header item, and fetches metadata only for those at or after the exact cutoff, so the bound holds to the second (C/D-scope); it is the same `INTERNALDATE` that `_message_date` reads. Every string operand of a search, an address here and a message id in step 17, goes through one encoder, `_search_operand`: an IMAP quoted string with `"` and `\` escaped for ASCII, and for a value outside ASCII `CHARSET UTF-8` with the operand sent as a literal (RFC 3501 section 6.4.4). A `NO [BADCHARSET]` reply means the server cannot search that operand, whichever kind it is: the unit then lists the folder's UIDs with `UID SEARCH UID 1:<highest> SINCE <date>` alone and fetches, in bounded batches, the header item it would have searched, the address headers here and the reply headers in step 17, matching or expanding locally as the Microsoft stage matches, with the UID bound as its durable progress, so the mail is still found, at the cost of the scan.
17. **Stage (b), thread sync.** Each followed thread syncs from `<since>`, the later of its watermark and the pass's cutoff (step 16), so a watermark that fell behind the moving cutoff never makes the pass fetch aged-out mail (C/D-scope); a thread without a watermark syncs from the cutoff.
    - **Gmail:** one `messages.list` per pass, `q = (in:inbox OR in:sent) after:<the oldest <since> among followed threads> before:<start>`, with durable page tokens; it returns ids and `threadId`s only. An id whose `threadId` is a followed thread's native id gets the metadata fetch, and nothing else is fetched, so no archived, trashed, or out-of-retention message is ever retrieved (C/D-scope), which `threads.get` could not promise: it returns a thread's every message. The provider thread id is read from the thread's messages (`messages.thread_id`, the native id as the provider gave it), never from the thread key, which `_provider_thread_key` may have hashed (`db.py:3282-3293`) or M1 may have minted. A thread whose messages all lack a native id has no provider thread; it is synced through its own messages only, and never sent to the provider. Microsoft's `conversationId` lookup reads the same column.
    - **Microsoft:** no second scan. Stage (a) records every in-window item it lists, its id, `conversationId`, `receivedDateTime`, and folder, in the pass's progress; once discovery has finished and the threads it followed are known, this stage matches followed threads against that record and fetches the items at or after each thread's `<since>` through their folder, so a thread followed late in the pass still gets the items listed before it was followed. The folder is the location, and no message outside the admitted folders is fetched (C/D-scope).
    - **IMAP:** `UID SEARCH UID 1:<highest> SINCE <since date>` in `INBOX` and Sent for `HEADER Message-ID`, `HEADER In-Reply-To`, and `HEADER References` over the component's ids, each through `_search_operand` (step 16), repeated until the component stops growing. `SINCE` is applied as step 16 applies it, one day early with the `INTERNALDATE` preflight on the boundary days, and a server that rejects an operand's charset falls back to step 16's scan, here fetching the reply-header item and expanding the component locally. Before that, it drains `imap_reply_header_gaps`:
      - a listed row whose mailbox identity is known, and that was received at or after the cutoff (step 16's bound), gets its reply headers fetched and merged through `_apply_imap_component`; an older row stays listed, untouched, until a later cutoff admits it, so a raised retention can still complete its component;
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
    - Microsoft: HTML, without `prefer_text`;
    - IMAP: the existing BODYSTRUCTURE path.

    `stored_body` prefers the HTML part of a `multipart/alternative` and falls back to plain text, since the quote containers of step 21 exist only in HTML; today's analysis path keeps preferring plain text (`mime.py:147`, `imap.py:1776`) and is unchanged.

    A logical message may have several recorded locations (step 5). Stage (c) tries each in-scope location in canonical order. A body response outside the admitted folders is discarded unstored, as C/D-scope says of every fetch response, and that copy is skipped; a copy that is gone is skipped. Only when every location is skipped does the message record `outside_folders` if any copy still exists, else `source_gone` (C/D-body). Other errors back off (step 15).
23. **Purge.**
    - `purge_with_outcome` (`db.py:12273`) keeps its predicate for messages outside followed threads; from M2.2 every purge runs after the check's polling, so a thread that a just-polled vendor message follows keeps its older members. A followed thread purges as one unit when its newest logical message is older than the cutoff and the account's coverage is current (C/D-scope, decision D1), so a thread whose newer replies are still unfetched survives a lapse. M2.2, which has the follow cache but no coverage, purges no followed thread at all; M2.3 brings the rule complete, so no release deletes history M2 keeps. That purge runs after the check's polling, so a reply that was waiting on the provider cursor is captured, and counted as the newest message, before the decision. Duplicate rows (`logical_of`) go with their logical message.
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
- **Cursors.** Folder cursors advance independently, and a Sent batch failure leaves the Inbox cursor untouched. An expired Sent Items delta link recovers on Sent Items, never on the Inbox. An Inbox or Sent cursor recovery clears that account's location stamps, seeded on two accounts beforehand: the recovered account's clear, the other account's stay.
- **Gmail checkpoint (M2 required item).** A poll that stops mid-range and resumes misses no `SENT` events.
- **Cleanup.** `delete_message`, `clear_messages`, and the purge remove a message's recipients and locations with it. Deleting the canonical row of a coalesced pair removes both rows and suppresses both source identities. A coalesced pair split across the cutoff survives the purge until both rows have expired, then goes together.
- **Seen ids.** A Gmail `SENT` label added to a captured message, and a Microsoft message moved into Sent Items, each record the second location on the next poll with no metadata fetch; a star or a read on a known message costs nothing. A label change during a lapse records what is in scope and leaves the source unstamped, so the next pass after reactivation observes it again. A copy recorded as a location is never fetched or analyzed again.
- **Coalescing (M2 required item).**
  - A retained duplicate becomes `logical_of` its canonical row, with nothing deleted; an analyzed duplicate of a pending copy is the canonical, so the message is not analyzed again and its summary shows.
  - A message skipped because its copy was gone is analyzed once another copy is recorded.
  - A new second location is recorded on the logical message, not inserted as a row.
- **Sent polling** stops while the gated class is inactive, health reports Sent as not polled from that check on, and a dry run previews Sent mail without moving its cursor.
- **One logical message everywhere.** After the upgrade a coalesced message is listed, queued, and notified once, and the duplicate's summary stays stored.
- **Recipient headers.** An IMAP message whose To/Cc headers reach their bound is captured with no recipients rather than skipped.

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
- **Thread purge.** From M2.2, a followed thread keeps every message through the purge, whatever its age, until M2.3's coverage-gated purge; a thread whose oldest message is past the cutoff and whose newest is not keeps both.
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
- **Rejected page token.** A saved Gmail page token the provider rejects does not stall the pass: it completes with every message discovered once.
- **Reappearance.** A message whose every location was recorded and that left both folders is fetched again when polling reports it in one; an IMAP copy arriving for a `source_gone` message supplies its body.
- **Assumed locations.** A retained Gmail message that carried `INBOX` and `SENT` before the upgrade is fetched once by discovery, records both folders and its recipients, and is attributed outbound; a star on it before that pass does not stamp it.
- **Aged gap rows.** A reply-header gap row older than the cutoff waits without a provider request, and is drained once a raised retention admits it.
- **Removal mid-pass.** A Microsoft message deleted, moved, or added between two checks of a pass leaves every other message discovered, and the pass completes; a folder larger than one check's budget completes across checks with no message skipped or captured twice, and no header of a pre-cutoff message is read.
- **Late follow.** A Microsoft reply listed before the vendor message that follows its conversation is synced in the same pass.
- **Aged out.** A followed thread's message older than the cutoff that has no body is neither fetched nor pending, and coverage becomes current without it; one with a stored body still gets its claim work (M4). A thread whose watermark fell behind the cutoff syncs from the cutoff.
- **Merge mid-sync.** An IMAP merge during a thread's sync leaves the survivor without a watermark, and the next unit syncs the merged component from the cutoff.
- **Thread sync scope.** A Gmail thread's archived message and a Microsoft conversation's Deleted Items message are never fetched during sync; an IMAP message dated before the cutoff in its own zone, on the boundary day, is not fetched, and one after it in a zone west of UTC is.
- **Vanished candidate.** A candidate deleted between listing and fetch completes its unit, and the pass records coverage.
- **Markers go with the message.** Deleting, clearing, or purging a message in `source_gone` or `outside_folders` leaves no marker, and a recapture starts with no body state.
- **Inactive account.** Adding a vendor address bumps every account's generation; a reactivated account is stale.
- **Identity assignment.** Rows that `reconcile_mailbox_identity` assigns an identity get attribution and follow rows in that transaction.
- **Bottom-posted Outlook reply.** Text written below `div#divRplyFwdMsg` is stored unprefixed.
- **Body snapshot.** A body whose response shows the copy outside the admitted folders is not stored.
- **Selector bound.** A vendor address of the longest length M1 accepts is captured under every kind.
- **Lapse and return.** A message captured, or relabelled, while Sent was out of scope has no stamped location; the next pass after reactivation fetches it once and records its Sent folder and recipients.
- **Search operands.** A vendor address with a quote, a brace, or a non-ASCII local part is discovered on IMAP and on Gmail, and a server that cannot search a non-ASCII address or message id still has that vendor's mail discovered, and its thread synced, through the bounded scan.
- **Unfollow drops the watermark**, so a refollowed thread is stale and resyncs.
- **Polling gap.** An entitlement lapse that ends, and a cursor that recovered, clear the account's watermarks, so an older reply that gained a folder during the gap is synced. Each also clears that account's location stamps, seeded on two accounts beforehand: the lapsed account's clear, the other's stay.
- **Purge after polling.** An old unfollowed message whose thread a just-polled vendor message follows survives that check's purge.
- **Raised retention clears every watermark,** and the IMAP search command nests its `OR` keys exactly.
- **Discovery.**
  - Gmail pages with durable tokens across checks (M2 required item).
  - Microsoft reads each folder's delta and applies dates and recipients locally, with no `$search` (M2 required item).
  - Every Gmail candidate gets metadata before its scope check, and no body in this stage (M2 required item).
- **Thread sync.**
  - Gmail thread sync is a bounded `messages.list` matched on `threadId`; `threads.get` is never called (M2 required item).
  - The IMAP header search repeats until the component stops growing (M2 required item).
  - The reply-header gap list drains and merges.
- **Budget.** A pass stops at its budget and resumes on the next check. An inactive class mid-pass stops it with progress kept.
- **Contract reconcile scenarios:** a vendor captured by polling syncs earlier messages; a Connect lapse, then reactivation, recovers the lapse; raising retention from 30 to 180 days fetches the older messages.

**M2.4**
- **Quote containers (M2 required item).** `<blockquote>`, Gmail's quote block, and Outlook's reply header each become `>` lines. `stored_body_text` leaves today's analysis normalization unchanged. A `multipart/alternative` whose plain part lacks the markers stores the HTML part's `>` lines.
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
