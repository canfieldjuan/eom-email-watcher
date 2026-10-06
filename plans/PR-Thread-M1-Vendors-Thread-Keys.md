# Thread View M1: Vendor Records and Thread Keys

Milestone M1 of [`docs/THREAD_VIEW_CONTRACT.md`](../docs/THREAD_VIEW_CONTRACT.md) (accepted 2026-10-05). It includes amendment B of #207, committed separately ahead of this plan.

This plan specifies M1's mechanics only. Every rule it implements is stated in the contract's definitions, and this plan cites them rather than restating them. Where this plan and a definition seem to differ, the definition wins, and this plan is wrong.

## Why this slice exists

Every later milestone needs two foundations:

- **Vendor records:** [D-vendor](../docs/THREAD_VIEW_CONTRACT.md#d-vendor-vendors-and-vendor_ofaddress), with the address operations and watchlist link of [D-ops](../docs/THREAD_VIEW_CONTRACT.md#d-ops-operation-classes-and-gating).
- **Correct thread keys:** [D-identity](../docs/THREAD_VIEW_CONTRACT.md#d-identity-message-identity-direction-and-thread-keys).

Neither exists today.

- There is no vendor entity. The only sender list is the config watchlist (`config.py:124`, `config.py:428`).
- IMAP fetches only `FROM SUBJECT DATE MESSAGE-ID` (`imap.py:1599`) and stores the message's own `Message-ID` as `thread_id` (`imap.py:1613`).
- `messages.thread_id` is written but never read or indexed (`db.py:4606`).

## Scope

Ownership lane: thread-view-m1

**In scope:**
- Vendor records with exact addresses.
- The address operations of D-ops (domains arrive in M5).
- D-identity thread keys for every message captured from now on.
- A migration for retained rows.
- A desktop Vendors list.

**Out of scope:**
- Admission and capture do not change. D-capture's new kinds arrive in M2.
- Follow state ([D-follow](../docs/THREAD_VIEW_CONTRACT.md#d-follow-followed-threads)) arrives in M2.
- Sent folders arrive in M2, and with them the IMAP Sent folder token and the recorded locations that derive direction under D-identity. In M1 every capture is an inbox capture.
- Bodies, claims, domains, and suggestions belong to later milestones.

### Mechanics

**Storage (schema 29, `SCHEMA_VERSION` 28 to 29):**
1. Two vendor tables:
   - `vendors(vendor_id UUIDv4 PRIMARY KEY, display_name, created_at, updated_at)`, with the display name trimmed, non-empty, and at most 200 UTF-8 bytes;
   - `vendor_addresses(address PRIMARY KEY, vendor_id, created_at)`. The primary key enforces D-vendor's uniqueness, and a trigger deletes a vendor's addresses with it.
2. New `messages` columns:
   - `thread_key TEXT`;
   - `rfc_message_id TEXT`.
3. IMAP component tables:
   - `imap_thread_ids(provider, account_id, mailbox_identity_key, rfc_id, thread_key)`, primary key on the first four columns;
   - `thread_key_aliases(old_key PRIMARY KEY, survivor_key)`.
4. Indexes:
   - `messages(provider, account_id, mailbox_identity_key, thread_key, received_at)`;
   - `messages(provider, account_id, mailbox_identity_key, rfc_message_id)`.
5. Migration of retained rows:
   - Gmail and Microsoft rows get `thread_key = thread_id`. For those providers, `thread_id` already holds `threadId` or `conversationId`.
   - IMAP rows get `rfc_message_id = thread_id`, because for IMAP that column already holds `Message-ID`.
   - Each IMAP row that has an id becomes a one-member component with a fresh UUIDv4 key, and its own id is registered.

**IMAP reply headers.**
6. The metadata `FETCH` adds a second header item, `BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)]`, with its own byte bound.
   - The existing `FROM SUBJECT DATE MESSAGE-ID` item and its `imap_headers_too_large` rejection (`imap.py:43`, `imap.py:1599`) are unchanged.
   - If the reply-header item reaches its bound or fails to parse, it is ignored: the message keeps only its own `Message-ID` in its id set, and admission is unchanged.
7. Ids are parsed as follows:
   - angle brackets and whitespace are trimmed, and case is kept;
   - at most 64 `References` ids are kept, oldest first;
   - an id over 998 characters is dropped.
8. `MessageMetadata` (`mailbox.py`) gains `rfc_message_id`, `in_reply_to`, and `references`. Gmail and Microsoft fill them with `None` and empty values in M1.

**Thread keys at capture.** Inside the capture transaction (`BEGIN IMMEDIATE`):
9. Gmail stores `threadId`, and Microsoft stores `conversationId`.
10. IMAP looks up the message's id set in `imap_thread_ids`:
    - no match: a new component, with a UUIDv4 key;
    - one component: the message joins it;
    - several components: they merge.
11. A merge picks the survivor under D-identity. In the same transaction, it re-keys `messages.thread_key` and `imap_thread_ids` and records aliases.
    - This merge function is the single re-key owner. Later milestones add their thread-keyed tables to it, and nowhere else.
12. The capture sites pass the new metadata to the insert (`service.py:1640`, `1832`, `2125`).

**Engine operations.** The classes are those of D-ops.
13. The operations are:
    - `vendors.list` (read);
    - `vendors.create`, `vendors.rename`, `vendors.addresses.add` (gated);
    - `vendors.addresses.remove` (with optional `unwatch`) and `vendors.delete` (with optional `unwatch_addresses`) (removal).
14. Gated operations call `connect.require_connect_entitlement()`. When it is inactive they return `connect_entitlement_required` and write nothing.
15. Vendor mutations run inside the existing watchlist mutation lock (`_with_watchlist_mutation`, `engine_api.py:5490`). That covers:
    - the watchlist link of D-ops (`add_sender` for an address not yet watched);
    - the new `conflict` guard in `_watchlist_remove`, which applies when the address belongs to a vendor.
16. `vendors.list` reports `watched` per address, read from the config watchlist.

**Desktop.**
17. A Vendors tab next to Watchlist. It shows controls by D-ops class, through the existing locked-Connect presentation (`desktop/src/connectAvailability.ts`). Text renders with `textContent`.
18. Typed requests in `desktop/src-tauri/src/engine.rs`, with commands in `lib.rs`.

### Concurrency

- **Vendor mutations** share the watchlist mutation lock, so they serialize with watchlist changes and production checks.
- **IMAP lookup, merge, and insert** run in one `BEGIN IMMEDIATE` transaction. Two inserts cannot miss each other's component, and a reader never sees a half re-keyed component.

### Failure cases

- **The watchlist add succeeds, then the vendor insert fails.** The address stays watched, which is harmless, and a retry is idempotent.
- **Malformed message ids** are dropped from the id set and never stored.

### Files touched

- `src/eom_email_watcher/db.py`, `mailbox.py`, `imap.py`, `gmail.py`, `microsoft365.py`, `service.py`, `engine_api.py`.
- `desktop/src-tauri/src/engine.rs`, `lib.rs`.
- `desktop/src/main.ts`, `desktop/src/vendors.ts` (new, the view model), `desktop/src/styles.css`.
- `docs/ENGINE_API.md`, `README.md`.
- Tests:
  - `tests/test_db.py`, `test_imap.py`, `test_service.py`, `test_engine_api.py`, and the test fakes that build `MessageMetadata`;
  - `desktop/test/vendors.test.ts` (new);
  - the Rust typed contract tests.

## Verification (fail-first on the base commit)

**Vendors:**
- create, rename, and delete;
- `vendors.addresses.add` in four cases: new (it becomes watched), already watched (unchanged), on another vendor (`conflict`, nothing written), and invalid;
- remove, with and without `unwatch`;
- `vendors.delete` with and without `unwatch_addresses`;
- every gated operation refused with nothing written while inactive, while `vendors.list` and the removal operations work;
- display-name bounds: empty, 200 bytes, 201 bytes;
- `watchlist.remove` of a vendor address returns `conflict` and leaves the config unchanged;
- a hand-edited config reports `watched: false`.

**IMAP:**
- a full `References` chain joins its root;
- a missing root joins through a shared id;
- arrival orders A,B,C, C,B,A, and B,C,A give one component with the same survivor;
- components whose members have no `Message-ID` (ids only from `In-Reply-To` and `References`) merge to the same survivor in every order;
- a bridging message merges atomically and records an alias;
- 65 `References` ids keep 64, and a 999-character id is dropped;
- an oversized reply-header item is ignored, and the message is still admitted;
- today's `imap_headers_too_large` rejection is unchanged.

**Gmail and Microsoft:** `thread_key` equals `threadId` or `conversationId`.

**Migration:**
- a v28 database gains the keys as specified;
- a reply to a retained IMAP row joins its component;
- v28 code refuses v29.

**Admission is unchanged:** the existing admission and service suites pass unmodified.

**Desktop:** the `vendors.ts` view model covers the locked, empty, `watched: false`, and conflict states.

**Gates:** `uv run ruff check .`, the full `uv run pytest` including the contract seam check, desktop `node --test`, `tsc`, and the Rust suite in CI.
