# Thread View M1: Vendor Records and Thread Keys

Milestone M1 of [`docs/THREAD_VIEW_CONTRACT.md`](../docs/THREAD_VIEW_CONTRACT.md) (accepted 2026-10-05). It includes amendment B of #207 and the review amendment to D-identity and D-ops, each committed separately ahead of this plan.

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
- D-identity thread keys for every message: those captured from now on, and retained rows by migration.
- A desktop Vendors list.

**Out of scope:**
- Admission and capture do not change. D-capture's new kinds arrive in M2.
- Follow state ([D-follow](../docs/THREAD_VIEW_CONTRACT.md#d-follow-followed-threads)) arrives in M2.
- Sent folders arrive in M2, and with them the IMAP Sent folder token and the recorded locations that derive direction under D-identity. In M1 every capture is an inbox capture.
- Bodies, claims, domains, and suggestions belong to later milestones.

### Mechanics

**Storage (schema 29, `SCHEMA_VERSION` 28 to 29):**
1. Vendor tables:
   - `vendors(vendor_id UUIDv4 PRIMARY KEY, display_name, created_at, updated_at)`, with the display name trimmed, non-empty, and at most 200 UTF-8 bytes;
   - `vendor_addresses(address PRIMARY KEY, vendor_id, created_at)`. The primary key enforces D-vendor's uniqueness;
   - `vendor_address_dismissals(vendor_id, address, created_at)`, primary key on both columns, holding D-ops's dismissals from the first removal on, so M5's suggestions inherit them.
   - A trigger deletes a vendor's addresses and dismissals with it.
2. New `messages` columns:
   - `thread_key TEXT`;
   - `rfc_message_id TEXT`.
3. IMAP component tables:
   - `imap_thread_ids(provider, account_id, mailbox_identity_key, rfc_id, thread_key)`, primary key on the first four columns;
   - `thread_key_aliases(old_key PRIMARY KEY, survivor_key)`;
   - a trigger that, when a component's last message is deleted, deletes its `imap_thread_ids` rows and the aliases naming it, so purged threads leave nothing behind.
4. Indexes:
   - `messages(provider, account_id, mailbox_identity_key, thread_key, received_at)`;
   - `messages(provider, account_id, mailbox_identity_key, rfc_message_id)`.
5. Migration of retained rows. Every row gets a key:
   - Gmail and Microsoft rows get `thread_key = thread_id`, which already holds `threadId` or `conversationId`; a row without one gets a fresh UUIDv4.
   - For IMAP, `thread_id` holds the raw `Message-ID` header. Each row, in `message_id` order, goes through the step-7 parser that new metadata uses:
     - a valid id that is already registered joins that component, so retained rows sharing an id share one component;
     - a valid new id gets a fresh UUIDv4 component, and the id is registered and stored as `rfc_message_id`;
     - a missing or malformed id gets a fresh component of its own, with nothing registered.

**IMAP reply headers.**
6. The metadata `FETCH` adds a second header item, `BODY.PEEK[HEADER.FIELDS (IN-REPLY-TO REFERENCES)]`, with its own byte bound.
   - The existing `FROM SUBJECT DATE MESSAGE-ID` item and its `imap_headers_too_large` rejection (`imap.py:43`, `imap.py:1599`) are unchanged.
   - If the reply-header item reaches its bound or fails to parse, it is ignored: the message keeps only its own `Message-ID` in its id set, and admission is unchanged.
7. One parser owns ids (`mailbox.normalize_message_id`), used at capture and by the migration:
   - surrounding whitespace and one pair of angle brackets are trimmed, and case is kept;
   - the result must be `left@right`, both sides non-empty, at most 998 characters, with no whitespace, control characters, or angle brackets. Anything else, such as `not-an-id`, is dropped and never registered;
   - `In-Reply-To` keeps its first id, and `References` keeps at most 64 ids, oldest first.
8. `MessageMetadata` (`mailbox.py`) gains `rfc_message_id` and `reply_ids` (the `In-Reply-To` id, then the `References` ids, deduplicated). Gmail and Microsoft leave them empty in M1.

**Thread keys at capture.** Inside the capture transaction (`BEGIN IMMEDIATE`):
9. Gmail stores `threadId`, and Microsoft stores `conversationId`. A message without one gets a fresh UUIDv4 key.
10. IMAP looks up the message's id set in `imap_thread_ids`:
    - no match: a new component, with a UUIDv4 key;
    - one component: the message joins it, and the key is unchanged;
    - several components: they merge.
11. A merge keeps the survivor's key under D-identity. In the same transaction it re-keys every row naming a merged key, which in M1 means `messages.thread_key`, `imap_thread_ids.thread_key`, and `thread_key_aliases.survivor_key`, then records an alias from each merged key.
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
17. `vendors.addresses.add` returns `conflict`, before any write, for an address equal to any `mail_accounts.address`.
    - That covers both of D-vendor's verified identities, because `reconcile_mailbox_session_identity` (`service.py:1208-1210`) refuses a session whose authenticated address differs from the stored one.
    - An account connected after its address became a vendor address is handled where `vendor_of` is evaluated, by amendment C of #207, before M2 uses it.
18. `vendors.addresses.remove` records the `(vendor, address)` dismissal in the same transaction as the removal.
19. The order of D-ops's two stores:
    - `vendors.addresses.add`: `add_sender` when not watched, then the vendor insert;
    - `vendors.addresses.remove` with `unwatch`, and `vendors.delete` with `unwatch_addresses`: membership is checked, then a new `config.remove_senders` removes every requested address in one config write, then the database transaction. `remove_sender` becomes its single-address case.

**Desktop.**
20. A Vendors tab next to Watchlist. It shows controls by D-ops class, through the existing locked-Connect presentation (`desktop/src/connectAvailability.ts`). Text renders with `textContent`.
21. Typed requests in `desktop/src-tauri/src/engine.rs`, with commands in `lib.rs`.

### Concurrency

- **Vendor mutations** share the watchlist mutation lock, so they serialize with watchlist changes and production checks.
- **IMAP lookup, merge, and insert** run in one `BEGIN IMMEDIATE` transaction. Two inserts cannot miss each other's component, and a reader never sees a half re-keyed component.

### Failure cases

- **An interruption between the two stores** leaves one of D-ops's valid states, and a retry completes the operation (step 19).
- **Malformed message ids** are dropped from the id set and never stored (step 7).

### Files touched

- `src/eom_email_watcher/db.py`, `mailbox.py`, `imap.py`, `config.py`, `service.py`, `engine_api.py`.
- `desktop/src-tauri/src/engine.rs`, `lib.rs`.
- `desktop/src/main.ts`, `desktop/src/vendors.ts` (new, the view model), `desktop/src/styles.css`.
- `docs/ENGINE_API.md`, `README.md`.
- Tests:
  - `tests/test_db.py`, `test_imap.py`, `test_config.py`, `test_service.py`, `test_engine_api.py`;
  - `desktop/test/vendors.test.ts` (new);
  - the Rust typed contract tests.

## Verification (fail-first on the base commit)

**Vendors:**
- create, rename, and delete;
- `vendors.addresses.add` in five cases: new (it becomes watched), already watched (unchanged), on another vendor (`conflict`, nothing written), a mailbox account's address (`conflict`, nothing written), and invalid;
- remove, with and without `unwatch`, records a dismissal;
- `vendors.delete` with and without `unwatch_addresses`, and its dismissals go with it;
- a delete with `unwatch_addresses` interrupted after the watchlist write leaves the vendor showing `watched: false`, and a retry completes it;
- every gated operation refused with nothing written while inactive, while `vendors.list` and the removal operations work;
- display-name bounds: empty, 200 bytes, 201 bytes;
- `watchlist.remove` of a vendor address returns `conflict` and leaves the config unchanged;
- a hand-edited config reports `watched: false`.

**IMAP:**
- a full `References` chain joins its root;
- a missing root joins through a shared id;
- arrival orders A,B,C, C,B,A, and B,C,A give the same components;
- a merge keeps the key of the component holding the smallest member, including when no member has a `Message-ID`;
- a bridging message merges atomically and records an alias, and a second merge re-points the first alias;
- 65 `References` ids keep 64; a 999-character id, `not-an-id`, `@host`, and `left@` are dropped;
- an oversized reply-header item is ignored, and the message is still admitted;
- today's `imap_headers_too_large` rejection is unchanged.

**Gmail and Microsoft:** `thread_key` equals `threadId` or `conversationId`, and a message without one gets a key of its own.

**Migration:**
- a v28 database gains the keys as specified, with every row keyed;
- a reply whose `In-Reply-To` is `<root@example.com>` joins the retained row stored with that raw header;
- two retained rows with one id share a component, and the upgrade completes;
- retained rows with no id, or a malformed one, each get their own key;
- v28 code refuses v29.

**Admission is unchanged:** the existing admission and service suites pass unmodified.

**Desktop:** the `vendors.ts` view model covers the locked, empty, `watched: false`, and conflict states.

**Gates:** `uv run ruff check .`, the full `uv run pytest` including the contract seam check, desktop `node --test`, `tsc`, and the Rust suite in CI.
