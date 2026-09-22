# Gmail user-label admission contract

Status: **Accepted 2026-09-19 after hostile review. Implementation must land
after this contract as a separate commit and name this accepted revision.**

Audited code: `canfieldjuan/eom-email-watcher` at
`84ab4f1bc58fbadac470db19ff49b44579f35e7a`.

## Decision and scope

The first widened-admission slice adds Gmail **USER-label** selectors. The
watcher admits a message exactly when:

```text
"INBOX" in metadata.label_ids
AND (
  normalize(metadata.from_address) in configured_exact_senders
  OR any(active_gmail_user_label_selector.label_id in metadata.label_ids)
)
```

Exact sender admission remains global configuration and keeps its existing
normalization and mutation behavior. Gmail label selection is additional and
is bound to one local Gmail account and its credential-derived mailbox
identity. A label display name never participates in admission.

This slice keeps `gmail.readonly` as the only Gmail watch authorization. It
lists labels and reads mail metadata/content; it does not add or remove a
Gmail label, change a message, delete mail, or send mail.

Authenticated-domain admission is deferred. The current Gmail adapter trusts
the parsed `From` header and asks Gmail only for `From`, `Subject`, and `Date`;
the IMAP adapter also parses `From`, and the Microsoft adapter accepts Graph's
`from.emailAddress`. None collects or verifies an authenticated SPF, DKIM, or
DMARC result (`src/eom_email_watcher/gmail.py:125-139,383-402`;
`src/eom_email_watcher/imap.py:1574-1586`;
`src/eom_email_watcher/microsoft365.py:586-603`). A domain suffix of those
values would therefore widen trust to spoofable input.

## Code-grounded root cause

The code already exposes the necessary Gmail metadata: `MessageMetadata`
contains a label-ID set, and Gmail fills it only from a JSON array of bounded,
non-control strings in `labelIds`; malformed provider shapes are rejected before
sender or label admission
(`src/eom_email_watcher/mailbox.py:54-62`;
`src/eom_email_watcher/gmail.py:125-140`). The blocker is the discovery and
admission path around that metadata:

1. Incremental Gmail history asks only for `messageAdded`, filtered to
   `INBOX`; it never collects `labelAdded` events
   (`src/eom_email_watcher/gmail.py:328-377`). A user label applied after
   delivery is invisible.
2. Stale-cursor recovery constructs only an exact-sender Gmail query
   (`src/eom_email_watcher/gmail.py:458-480`). A label-only watchlist cannot
   recover candidates.
3. The watcher returns inactive when the TOML sender list is empty, and the
   only admission branch is `INBOX` plus exact sender
   (`src/eom_email_watcher/service.py:1187-1192,1261-1290`).
4. The exact sender list is global TOML configuration
   (`src/eom_email_watcher/config.py:75-103,261-286`), while mailbox cursors,
   messages, and account identities are already provider/account scoped
   (`src/eom_email_watcher/db.py:2227-2246,2310-2346,3420-3461`). Gmail label
   selectors therefore belong in account-scoped SQLite state rather than in
   the global sender document.
5. The watcher currently gates on metadata before calling `content()` and the
   model (`src/eom_email_watcher/service.py:1261-1328,1496-1519`). The widened
   gate must retain that ordering.

The root fix is one identity-bound selector store, one pure admission matcher,
and candidate discovery that delivers both message arrivals and later label
additions to that matcher.

## Terms

- **Account reference**: exact `(provider, account_id)` from `mail_accounts`.
- **Mailbox identity**: the credential-derived lowercase SHA-256 key already
  required by `mailbox_session_identity_key()`
  (`src/eom_email_watcher/mailbox.py:104-122`). It is not supplied by the
  frontend.
- **Gmail label ID**: the opaque, case-sensitive `id` returned by Gmail. It is
  not a display name and is never normalized.
- **USER label**: a live Gmail label whose provider-declared `type` decodes to
  the known value `user`. Unknown types fail closed.
- **Control character**: any decoded Unicode scalar whose General Category is
  `Cc`, including C0 `U+0000` through `U+001F`, `DEL`/C1 `U+007F` through
  `U+009F`. JSON escapes are decoded before this test. The same definition
  governs label IDs, label names, and provider message IDs.
- **Selector**: one durable local row binding a generated selector UUID to
  `(gmail, account_id, mailbox_identity_key, label_id)`.
- **Catalog snapshot**: one complete successful label-list response for the
  authenticated mailbox during an operation or polling round.
- **Active selector**: a selector whose account and mailbox identity match the
  open session and whose label ID appears as USER in that round's catalog
  snapshot.
- **Recovery state**: the one durable Gmail-only SQLite row for an account when
  an incremental history cursor is stale. It freezes mailbox identity, sender
  set, active selector set, selector-set revision, bounded time window, and
  replacement history cursor, then owns one broad Gmail query's current page,
  next index, page token, counters, retry schedule, and failure status until
  that query drains.
- **Admission snapshot**: the deterministic matcher result persisted with a
  message. Later selector or remote-label changes do not revoke it.

## Normative invariants

### Common matcher

One pure function owns production and dry-run admission. Its inputs are:

```text
provider
account_id
mailbox_identity_key
metadata(message_id, sender, label_ids, ...)
normalized_exact_senders
active_label_selectors(selector_id, label_id, display_name)
admitted_at
```

Its output is either `not_admitted` or:

```json
{
  "kind": "exact_sender | gmail_user_label",
  "selector_id": "string",
  "display_name": "string or null",
  "mailbox_identity_key": "64 lower-case hex characters",
  "admitted_at": "UTC ISO-8601 timestamp"
}
```

Rules:

1. Missing `INBOX` always returns `not_admitted`.
2. Exact sender match is the normalized full address already used by
   `Config.allowlist`; substring and domain matches are forbidden.
3. Label match is available only when `provider == "gmail"`, account ID and
   mailbox identity match exactly, and the metadata contains the selector's
   exact label ID.
4. Microsoft 365 and IMAP synthetic `INBOX` metadata cannot match a Gmail
   selector. This slice does not add a provider-neutral label/folder concept.
5. If exact sender and one or more labels overlap, exact sender wins. Its
   `selector_id` is `sender:` followed by the normalized full address. If
   multiple labels overlap without a sender match, the lexicographically
   smallest canonical selector UUID wins. This makes production and dry run
   deterministic while the message-ID dedupe still admits one message once.
6. `display_name` is copied only for presentation. For exact sender it is the
   configured sender name, if any; for a label it is the current catalog name.
   It never changes a match.
7. The matcher has no I/O and performs no writes. Callers may persist its
   returned snapshot only after all metadata identity and retention checks
   pass.

### Ordering and effects

For every incremental or recovery candidate the order is:

1. provider/account/credential identity reconciliation;
2. local seen/suppression check;
3. bounded provider metadata fetch;
4. provider message-ID equality check;
5. common matcher;
6. source-time/retention check;
7. atomic message plus admission-snapshot insert;
8. body and attachment metadata fetch;
9. model, notification, Connect, or automation work.

An unadmitted candidate leaves no message, body, attachment, provenance,
model, notification, Connect, or automation row. Metadata transport itself is
the only permitted per-candidate read before admission. The existing
read-only Gmail scope is fixed at
`src/eom_email_watcher/gmail.py:34`.

Already admitted pending work uses its persisted admission snapshot. A later
local selector removal, Gmail rename, Gmail label removal, or catalog outage
does not revoke or reclassify it.

## Durable SQLite contract

The next schema revision adds only three account-scoped structures and leaves
database connection configuration unchanged. Application operations must first
resolve an exact existing `mail_accounts` row under the
native and cross-process mailbox-operation lock, then enforce account and
identity scope again in the same write transaction.

```sql
CREATE TABLE gmail_label_selector_sets (
  provider TEXT NOT NULL CHECK (provider = 'gmail'),
  account_id TEXT NOT NULL CHECK (
    account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
  ),
  current_mailbox_identity_key TEXT NOT NULL CHECK (
    length(current_mailbox_identity_key) = 64
    AND current_mailbox_identity_key = lower(current_mailbox_identity_key)
    AND current_mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
  ),
  revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (provider, account_id)
);

CREATE TABLE gmail_label_selectors (
  selector_id TEXT PRIMARY KEY CHECK (length(selector_id) = 36),
  provider TEXT NOT NULL CHECK (provider = 'gmail'),
  account_id TEXT NOT NULL CHECK (
    account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
  ),
  mailbox_identity_key TEXT NOT NULL CHECK (
    length(mailbox_identity_key) = 64
    AND mailbox_identity_key = lower(mailbox_identity_key)
    AND mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
  ),
  label_id TEXT NOT NULL CHECK (
    label_id <> '' AND length(CAST(label_id AS BLOB)) <= 512
  ),
  selected_display_name TEXT NOT NULL CHECK (
    selected_display_name <> ''
    AND length(CAST(selected_display_name AS BLOB)) <= 1024
  ),
  created_at TEXT NOT NULL,
  UNIQUE (provider, account_id, mailbox_identity_key, label_id)
);

CREATE TABLE gmail_recovery_state (
  provider TEXT NOT NULL CHECK (provider = 'gmail'),
  account_id TEXT NOT NULL CHECK (
    account_id <> '' AND length(CAST(account_id AS BLOB)) <= 128
  ),
  mailbox_identity_key TEXT NOT NULL CHECK (
    length(mailbox_identity_key) = 64
    AND mailbox_identity_key = lower(mailbox_identity_key)
    AND mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
  ),
  selector_revision INTEGER NOT NULL CHECK (selector_revision >= 0),
  sender_snapshot_json BLOB NOT NULL CHECK (
    length(sender_snapshot_json) <= 1048576
  ),
  selector_snapshot_json BLOB NOT NULL CHECK (
    length(selector_snapshot_json) <= 1048576
  ),
  recovery_after_exclusive_epoch INTEGER NOT NULL CHECK (
    recovery_after_exclusive_epoch >= 0
  ),
  recovery_before_exclusive_epoch INTEGER NOT NULL CHECK (
    recovery_before_exclusive_epoch > recovery_after_exclusive_epoch
  ),
  replacement_history_cursor TEXT NOT NULL CHECK (
    replacement_history_cursor <> ''
    AND length(CAST(replacement_history_cursor AS BLOB)) <= 4096
  ),
  page_token TEXT CHECK (
    page_token IS NULL OR length(CAST(page_token AS BLOB)) <= 8192
  ),
  current_page_ids_json BLOB NOT NULL CHECK (
    length(current_page_ids_json) <= 524288
  ),
  page_loaded INTEGER NOT NULL DEFAULT 0 CHECK (page_loaded IN (0, 1)),
  next_index INTEGER NOT NULL DEFAULT 0 CHECK (next_index BETWEEN 0 AND 200),
  page_count INTEGER NOT NULL DEFAULT 0 CHECK (page_count >= 0),
  terminal_candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (
    terminal_candidate_count >= 0
  ),
  invalid_page_token_count INTEGER NOT NULL DEFAULT 0 CHECK (
    invalid_page_token_count >= 0
  ),
  consecutive_retry_count INTEGER NOT NULL DEFAULT 0 CHECK (
    consecutive_retry_count BETWEEN 0 AND 31
  ),
  state TEXT NOT NULL CHECK (state IN ('collecting', 'backoff', 'degraded')),
  failure_code TEXT,
  next_retry_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (provider, account_id)
);
```

Application decoding makes the schema strict beyond SQLite's scalar checks.
Selector snapshots use UTF-8 JSON with `ensure_ascii=false`, sorted object
keys, and compact separators. They contain at most 100 objects with exactly
`selector_id`, `label_id`, and `display_name`: canonical lowercase UUIDv4
(36 ASCII bytes), opaque label ID (1 through 512 UTF-8 bytes), and display name
(1 through 1,024 UTF-8 bytes). Unknown/missing/duplicate keys, decoded `Cc`
control characters under the definition above, and invalid UTF-8 are rejected.
Quotes and backslashes may double
every label/name byte; with the fixed 50-byte empty object encoding, one item
is therefore at most 3,158 bytes and the 100-item array at most 315,901 bytes.
The 1 MiB column cap leaves more than threefold headroom, including the maximum
UTF-8, quote, and backslash cases.

Sender snapshots are canonical sorted JSON made from the already-valid current
exact-sender configuration and decoded through the same existing address/name
rules. This slice adds no sender count or global configuration limit. If that
valid configuration cannot serialize within the practical 1 MiB recovery cap,
creation returns `gmail_recovery_snapshot_too_large` before a recovery row,
provider search, or cursor change; it never truncates or silently drops a
sender.

`current_page_ids_json` uses UTF-8 JSON with `ensure_ascii=false` and compact
separators. Before canonicalization or persistence, its decoder requires zero
through 200 provider-ordered unique string IDs of 1 through 512 UTF-8 bytes and
rejects every decoded `Cc` control character under the definition above.
Quotes and backslashes can double all 512 bytes, so
the exact 200-item worst case is `2 + 200 * (2 + 2 * 512) + 199 = 205,401`
bytes: array brackets, quoted/escaped IDs, and commas. The 524,288-byte column
cap is more than 2.5 times that maximum. Rejected control characters cannot
increase the valid bound; all-quote or all-backslash IDs remain the exact
205,401-byte worst case. `next_index` cannot exceed the decoded array's length.
`page_loaded=0` requires an empty page and zero index;
`page_loaded=1` distinguishes a fetched empty final page from the not-yet-
fetched initial state. A
`gmail_label_selectors_immutable` trigger rejects every selector update.
Selectors are inserted or deleted, never rebound or renamed locally.
`selected_display_name` is the immutable selection-time fallback shown when
live validation is unavailable. A live rename is returned from the catalog
without rewriting the selector row.

A recovery-state immutability trigger rejects changes to provider, account,
mailbox identity, selector revision, sender snapshot, selector snapshot,
the two exclusive epoch bounds, replacement history cursor, or original
`created_at`. Only its current provider page, next index, monotonic counters,
state, failure, retry schedule, and update timestamp may change. `page_count`,
`terminal_candidate_count`, and `invalid_page_token_count` have no normal
completion ceiling; the trigger requires each new value to be greater than or
equal to its old value. Each increment is checked before SQLite's signed 64-bit
maximum; overflow returns stable `gmail_recovery_counter_overflow`, leaves the
old cursor unchanged, and fails closed rather than coercing the value. All
recovery writes validate the exact `mail_accounts` row and current
selector-set identity in application code inside the same `BEGIN IMMEDIATE`
transaction.

The existing account-registration/reconciliation transaction must ensure one
selector-set row for each registered Gmail account once credentials can derive
the mailbox identity. First reconciliation inserts revision zero. Reconciliation
with the same identity changes neither identity nor revision. Every replacement
with a different credential-derived mailbox identity atomically updates
`current_mailbox_identity_key`, increments the selector-set revision exactly
once, and deletes any `gmail_recovery_state` row for that account without
advancing its mailbox cursor. Existing selector rows remain under their old
identity and therefore list as `identity_mismatch`; they are never rebound.
Repeated reconciliation of the already-current identity is idempotent.

At most **100** selectors may exist for one `(provider, account_id)` across all
retained mailbox identities. This is a storage, UI, and bounded-matcher limit;
the single broad recovery query does not grow with selector count. Add at 100
fails without changing the set. The set revision starts at zero and increments
exactly once in the same `BEGIN IMMEDIATE` transaction as each successful
selector insert or delete. Add and remove require exact `expected_revision`;
stale revision, identity mismatch, duplicate, missing selector, limit, or
constraint failure leaves revision and rows unchanged.

The `messages` table gains nullable migration columns with these constraints:

```sql
admission_kind TEXT CHECK (
  admission_kind IS NULL
  OR admission_kind IN ('exact_sender', 'gmail_user_label')
)
admission_selector_id TEXT CHECK (
  admission_selector_id IS NULL
  OR (
    admission_selector_id <> ''
    AND length(CAST(admission_selector_id AS BLOB)) <= 512
  )
)
admission_display_name TEXT CHECK (
  admission_display_name IS NULL
  OR length(CAST(admission_display_name AS BLOB)) <= 1024
)
admission_mailbox_identity_key TEXT CHECK (
  admission_mailbox_identity_key IS NULL
  OR (
    length(admission_mailbox_identity_key) = 64
    AND admission_mailbox_identity_key = lower(admission_mailbox_identity_key)
    AND admission_mailbox_identity_key NOT GLOB '*[^0-9a-f]*'
  )
)
admitted_at TEXT CHECK (
  admitted_at IS NULL
  OR (admitted_at <> '' AND length(CAST(admitted_at AS BLOB)) <= 64)
)
```

Migration preserves old rows with all five fields null. After migration, a
`messages_require_admission_provenance_insert` trigger rejects every new row
unless `admission_kind`, `admission_selector_id`,
`admission_mailbox_identity_key`, and `admitted_at` are populated and the
admission identity equals `mailbox_identity_key`. A
`messages_admission_provenance_immutable` trigger rejects changes to any
populated admission field or its source mailbox identity; it still permits the
existing one-time legacy identity reconciliation while all admission fields
are null. Application parsing also requires a valid UTC timestamp. The message
plus provenance insert remains atomic with the existing account-identity and
deletion-suppression checks in `Store.add_message()`
(`src/eom_email_watcher/db.py:3518-3678`).

Inbox projections expose:

```json
"admission": {
  "kind": "exact_sender | gmail_user_label",
  "selector_id": "string",
  "display_name": "string or null",
  "admitted_at": "UTC ISO-8601 timestamp"
}
```

They do not expose `mailbox_identity_key`; backend tests prove the persisted
binding. Legacy rows return `"admission": null`.

## Engine operations

All requests use the existing protocol envelope, reject unknown payload fields,
and accept only exact JSON integer values for revisions; booleans are invalid.
`provider` must equal `gmail`; there is no defaulting. Account lookup is exact
and never falls back to the active account. In this first slice every operation
also requires that exact account to be the backend-reported current active,
connected Gmail account. A stale or non-active reference returns
`account_not_active`; a disconnected active reference returns
`account_unavailable`.

Catalog/list/add acquire the same native mailbox-operation lock and
cross-process operation lock used by watcher checks before opening credentials
or touching selector state. Remove acquires both locks but remains
provider-network-free, so an invalid selector can be removed during a Gmail
network outage while its exact account remains active and locally connected.
No response contains credential material, token paths, the mailbox identity
key, raw provider documents, or non-USER labels.

### `gmail.labels.catalog`

Request:

```json
{"provider":"gmail","account_id":"gmail-default"}
```

The backend loads exactly that registered, connected account, derives the
mailbox identity from its credential, completes account/selector-set
reconciliation, and calls Gmail's read-only label-list endpoint. It decodes the
complete response before using it.

The whole-document guard is exact:

1. The decoded HTTP response entity is capped at **1,048,576 bytes**. A numeric
   `Content-Length` over that cap is rejected before reading. Because the header
   may be absent, compressed, or otherwise untrusted, every response is still
   streamed/read through a bounded reader that stops after at most cap plus one
   decoded byte. Exactly 1,048,576 bytes proceeds to parsing if complete and
   valid; observing byte 1,048,577 rejects the whole response.
2. The complete parsed `labels` array contains at most **10,000** items. Exactly
   10,000 is accepted only when every other bound fits; item 10,001 rejects the
   whole response.
3. Each item must have one nonempty unique opaque `id` of 1 through 512 UTF-8
   bytes, one nonempty `name` of 1 through 1,024 UTF-8 bytes, and one
   provider-declared `type` equal exactly to known `user` or `system`. Missing,
   duplicate, wrong-typed, invalid-UTF-8, or decoded `Cc`-containing fields and
   any unknown type reject the whole response. Other provider fields do not enter
   the normalized catalog. JSON duplicate keys at any level and trailing
   non-whitespace bytes are invalid.
4. The normalized catalog is canonical UTF-8 JSON with `ensure_ascii=false`,
   compact separators, sorted object keys, and items sorted by exact opaque ID.
   Its complete encoded size is capped at **1,048,576 bytes**. Exactly the cap
   is accepted if valid; cap plus one rejects the whole snapshot.

Known system labels are retained in that normalized snapshot only for
rejection/status decisions; only labels with provider-declared type `user` are
returned as selectable items. `INBOX` and all other system labels are never
returned. Frontend label names or types are ignored as claims. UI items sort by
display name then opaque ID. No transport, count, item, parse, or canonical-size
failure yields a partial UI list, selectable item, Add authority, selector
write, catalog-derived revision change, or mailbox-cursor change. An identity
replacement independently reconciled before the failed request retains its
required single selector-set revision increment.

Result:

```json
{
  "provider":"gmail",
  "account_id":"gmail-default",
  "revision":3,
  "items":[
    {
      "label_id":"Label_123",
      "display_name":"Invoices",
      "selected":true,
      "selector_id":"canonical-uuid-or-null"
    }
  ]
}
```

For Gmail HTTP 403, only `rateLimitExceeded` and `userRateLimitExceeded` are
transient. A nonempty reason set containing only those two values returns
retryable `gmail_label_catalog_unavailable`. A 401 is rejected without reading
its response body. A 403 with a missing, malformed, permission, unknown,
`quotaExceeded`, or mixed transient/stable reason set returns stable,
nonretryable `gmail_authorization_rejected`. The manual catalog refresh and
scheduled discovery paths expose the same public code and retryability, and
neither exposes the provider response body.

A transport byte overflow, cleanly completed malformed or truncated successful JSON document,
decoded item-count overflow, unknown label type, malformed item, duplicate ID,
or canonical byte overflow rejects the whole snapshot with stable
`gmail_label_catalog_invalid`; it never becomes a partial result. Locally
disconnected or absent credentials return `account_unavailable`;
account/provider mismatch returns `account_not_active`, `not_found`, or
`unsupported_provider`. A transport exception while reading a successful
response is retryable `gmail_label_catalog_unavailable`, not stable invalid
provider data. No selector or mailbox cursor changes, except that an
identity replacement already detected by established reconciliation performs
the single required selector-set revision change and recovery-state removal.

### `gmail.label_selectors.list`

Request:

```json
{"provider":"gmail","account_id":"gmail-default"}
```

The operation reopens the exact account, derives current identity, completes
reconciliation, reads every stored selector for that account, then attempts
one authenticated complete catalog snapshot for the current identity. It does
not delete, rebind, or silently repair selectors.

Result:

```json
{
  "provider":"gmail",
  "account_id":"gmail-default",
  "revision":3,
  "catalog_state":"current | unavailable | invalid_catalog",
  "items":[
    {
      "selector_id":"canonical-uuid",
      "label_id":"Label_123",
      "display_name":"Current or selection-time name",
      "status":"active | deleted | not_user | identity_mismatch | validation_unavailable",
      "admission_active":true
    }
  ]
}
```

Only `status == "active"` yields `admission_active: true`. A complete catalog
that lacks the ID yields `deleted`; the same ID reported as a known system type
yields `not_user`; a selector bound to an old credential identity yields
`identity_mismatch`. Network/transport failure yields
`validation_unavailable`, retains the row visibly, and makes it inert. An
unknown provider label type or any other invalid complete-catalog shape sets
`catalog_state: "invalid_catalog"` and gives every current-identity selector
`validation_unavailable`; unknown types are never reported as `not_user`.
Known provider-declared system labels alone may yield `not_user`. The frontend
cannot assert or override status.

### `gmail.label_selectors.add`

Request:

```json
{
  "provider":"gmail",
  "account_id":"gmail-default",
  "label_id":"Label_123",
  "expected_revision":3
}
```

Under both mailbox-operation locks, the backend reopens the exact active Gmail
account, derives identity X, and completes the established reconciliation step.
It then fetches and fully validates a fresh complete Gmail catalog with the
read-only credential. The request supplies no label name, type, status, or
mailbox identity, and an earlier catalog displayed by the UI is never trusted
for this mutation.

Immediately before the write, the backend reopens the exact credential and
derives identity Y. If Y differs from X, Add returns
`mailbox_identity_changed`. In one `BEGIN IMMEDIATE` transaction it then
re-reads the exact registered account identity, selector-set
`current_mailbox_identity_key`, and selector-set revision. Both stored
identities must still equal Y and the revision must equal `expected_revision`.
A replacement reconciled since the displayed catalog therefore returns
`stale_revision`; an unreconciled or externally changed identity returns
`mailbox_identity_changed`. Neither outcome writes a selector.

The fresh catalog must contain the exact case-sensitive `label_id` with known
provider-declared type USER. Add rejects `INBOX`, every known system label, a
free-form/nonexistent ID, invalid catalog, duplicate scope plus ID, oversized
field, and the 101st selector. It creates the selector UUID, stores the fresh
catalog display name, inserts the row, and increments the revision in the same
transaction.

Result:

```json
{
  "revision":4,
  "item":{
    "selector_id":"canonical-uuid",
    "label_id":"Label_123",
    "display_name":"Invoices",
    "status":"active",
    "admission_active":true
  }
}
```

Stable errors are `stale_revision`, `conflict`, `limit_exceeded`,
`invalid_request`, `label_not_found`, `label_not_user`,
`gmail_label_catalog_invalid`, `gmail_label_catalog_unavailable`,
`gmail_authorization_rejected`,
`account_not_active`, `account_unavailable`, and
`mailbox_identity_changed`. Every failure leaves selector rows and revision
unchanged, except an independently required identity-reconciliation transaction
may already have incremented revision before Add evaluates the stale request.

The required race result is explicit: catalog under credential identity X,
credential replacement/reconciliation to identity Y, then Add of the same
opaque label ID returns `stale_revision` or `mailbox_identity_changed`; it
cannot insert a selector or perform a second revision increment. Raw X and Y
are never returned.

Adding a selector does **not** change, rewind, or delete the mailbox cursor and
does not run an immediate message search. The next normal scheduled check does
apply it to every still-unprocessed candidate reachable from the current
incremental cursor or a recovery window captured after the selector existed.
A recently delivered message that already has the label may therefore be
admitted as bounded recent catch-up. Add never starts an unbounded or
save-time scan.

### `gmail.label_selectors.remove`

Request:

```json
{
  "provider":"gmail",
  "account_id":"gmail-default",
  "selector_id":"canonical-uuid",
  "expected_revision":4
}
```

Removal is an exact local delete scoped by provider, account, and selector ID.
It does not contact Gmail or require the selector's historical mailbox identity,
which permits removal of `deleted`, `identity_mismatch`, and network-offline
selectors. The exact account must still be the active connected Gmail account,
and the transaction re-reads the current selector-set revision. It cannot
delete a same-ID row under another or stale account.

Result:

```json
{"revision":5,"removed_selector_id":"canonical-uuid"}
```

Stale revision returns `stale_revision`; absent or wrong-account selector
returns `not_found`. Already admitted messages and their provenance remain.

## Settings UI

The existing Watchlist view currently renders only exact senders and calls the
three `watchlist.*` operations (`desktop/src/main.ts:457-474,3167-3257`;
`desktop/src-tauri/src/engine.rs:961-964,1420-1427`). Exact senders remain
there. This slice adds one Gmail-label section to the existing **Settings**
view (`desktop/src/main.ts:574-624`); it does not turn the sender form into a
mixed rule editor:

1. The section binds only to the backend-reported current active connected
   Gmail account. This slice adds no account selector. It displays that
   account's presentation name/address but never asks the user for an account
   ID, mailbox key, label ID, or label name as free text.
2. The UI assigns a local generation to the active-account response. On any
   active-account change, disconnect, or reconnect notification it immediately
   clears catalog items, selected rows, and pending mutations; disables
   Add/Remove; increments the generation; and reloads list and catalog only for
   the new backend-reported active Gmail account. A late result from an older
   generation or a response whose exact provider/account differs from the
   current generation is discarded.
3. **Refresh labels** calls `gmail.labels.catalog` and fills a select control
   from returned USER items only. The response's revision is retained only
   with its provider, account, and local generation.
4. **Add label** sends only the selected opaque ID and currently rendered
   revision with the exact provider/account. The backend fetches and validates
   its own fresh catalog. `account_not_active`, `mailbox_identity_changed`,
   `stale_revision`, or a catalog error clears catalog state and reloads the
   current account; the user must choose and submit again.
   Immediately beside Add, visible copy says: **“Applies on the next scheduled
   check and may include recent matching mail. Adding a label does not start a
   full mailbox scan.”**
5. The selected list comes only from `gmail.label_selectors.list`; each row
   shows current/fallback display name, status, and Remove. Deleted,
   identity-mismatched, and unavailable rows remain visible and clearly inert.
6. Remove sends selector UUID plus current revision. On `stale_revision`, the
   UI reloads once and requires a fresh user action; it never blindly retries a
   mutation.
7. The UI does not accept a provider type, status, mailbox identity, or custom
   label ID from DOM state. It does not expose a domain field in this slice.
8. Empty exact senders plus at least one active selector is presented as an
   active watcher configuration. Text that currently says a sender is required
   is updated narrowly to say a watched sender or Gmail label is required
   (`desktop/src/main.ts:368,2882-2886,3170-3174`).

No label creation, rename, provider mutation, bulk edit, search, sorting
preference, styling polish, or provider-neutral folder UI belongs to this
slice.

## Polling and recovery

### Incremental Gmail history

Every active Gmail check uses one stable broad history stream, regardless of
whether the configuration currently has senders, selectors, or both. Every
request uses `historyTypes=["messageAdded", "labelAdded"]` and omits the Gmail
`labelId` server filter. Selector changes therefore never alter the provider
query shape, event order, or continuation meaning. The common local matcher
enforces `INBOX` and sender-or-label admission.

The canonical candidate order is Gmail history-record order, then
`messagesAdded` array order, then `labelsAdded` array order within each record.
The first occurrence of a provider message ID wins. The adapter deduplicates
globally across record types and provider pages before enforcing the existing
limit of 200 unique IDs returned per check. A delivery event and any later
label-add events for the same message yield one candidate per replayed stream.

A new continuation format, `eom-gmail-history-v2:`, contains bounded canonical
JSON with the original numeric `startHistoryId`, the number of unique IDs
already returned, and a SHA-256 digest of that canonical unique-ID prefix. It
is at most 4,096 bytes, permits an offset only from zero through 50,000, and
contains no sender or selector state. An offset or replay beyond that bound
raises `StaleHistoryCursor` and enters durable recovery. When a 201st unique ID
exists, the adapter returns the first 200 plus this continuation instead of the
newest history cursor. On resume it replays the same broad request from the
original history ID, reconstructs and verifies the skipped prefix count and
digest, then returns the next at most 200 unique IDs. A short or changed prefix,
invalid format, oversized token, or missing start history ID raises
`StaleHistoryCursor`; it never skips ahead. Only after all provider pages drain
does the adapter return Gmail's newest numeric history cursor.

An existing `eom-gmail-history-v1:` continuation came from the old narrower
query and is never interpreted under the broad query. It raises
`StaleHistoryCursor` and enters the bounded durable recovery below. An ordinary
numeric Gmail history cursor remains a valid starting cursor.

A selected label applied after delivery produces a future `labelAdded`
candidate. If metadata then contains both `INBOX` and the selected label, the
message is admitted exactly once. The existing message/suppression identity
dedupe runs before metadata fetch, so history replay, overlapping selectors,
and later label events cannot create a second analysis.

### Stale-cursor recovery

Recovery starts from the existing lower instant `L`, derived from last success
minus five minutes and capped by the retention cutoff
(`src/eom_email_watcher/service.py:1234-1259`). It uses the single
`gmail_recovery_state` row, not an in-memory union or one query per rule. The
Gmail adapter adds one provider-specific page operation while the existing
`MailboxChanges(message_ids, cursor)` seam remains unchanged for incremental
Gmail and for Microsoft/IMAP:

```text
recovery_page(
  page_token, after_exclusive_epoch, before_exclusive_epoch, max_results=200
) -> (provider_ordered_message_ids, next_page_token_or_null)
```

Every call uses one broad Gmail `messages.list` query: `in:inbox` plus the fixed
`after:<after_exclusive_epoch>` and `before:<before_exclusive_epoch>` bounds,
`maxResults=200`, and the stored page token. Gmail interprets both epoch-second
operators exclusively. The query uses no sender query, label query, label ID,
or selector-dependent provider parameter. Candidate admission is entirely
local.

On `StaleHistoryCursor`, under both mailbox-operation locks the service:

1. reopens the exact current active Gmail account, completes identity
   reconciliation, and obtains a complete valid catalog if current-identity
   selector rows exist;
2. fully encodes both grant snapshots and rejects
   `gmail_recovery_snapshot_too_large` without cursor change if either exceeds
   1 MiB;
3. captures Gmail's current numeric history cursor **first**;
4. samples a UTC timestamp `S` immediately after that cursor capture, then
   computes `before_exclusive_epoch = floor(S) + 1`. It always advances to the
   next whole second, including when `S` is exactly integral;
5. computes `after_exclusive_epoch = ceil(L) - 1`, the greatest whole epoch
   second strictly earlier than `L`. This outward rounding includes the entire
   second containing the lower instant; the exact metadata retention check
   later rejects any intentionally overlapped pre-`L` message;
6. freezes those two integer bounds, sorted normalized exact senders, active
   selector rows with UUID/opaque ID/display name, selector-set revision,
   mailbox identity, and the captured replacement cursor; and
7. inserts the one recovery row with a null page token, empty current page,
   index zero, and zero counters before the first search. The old mailbox cursor
   remains unchanged.

`L` and `S` must be valid post-epoch instants, so both stored integers are
nonnegative and the upper bound is greater than the lower bound. The
cursor-first/sample-second/outward-rounding sequence is normative. A message or
label event injected after cursor capture, at `S`, or anywhere else in the same
whole second as an exactly integral or fractional `S` remains below the next-
second exclusive bound. It may appear in both recovery and subsequent
incremental history; existing scoped seen/suppression/message uniqueness
deduplicates that deliberate overlap. Sampling or rounding inward before
cursor capture is forbidden because it can lose a same-second event.

When `page_loaded=0`, production requests the stored page token. It rejects a
malformed response, more than 200 IDs, or any ID that is empty, duplicate,
invalid UTF-8, over 512 bytes, or contains a decoded `Cc` control character.
That whole provider page returns `gmail_recovery_page_invalid` without
canonicalization, persistence, page/token/index change, or counter increment.
For a valid response, one transaction stores the complete provider-ordered
page, sets `page_loaded=1`, zeroes `next_index`, records the response's next
page token, and increments `page_count` **before** fetching any message
metadata. It never requests the next provider page until every ID in the
stored page reaches a terminal outcome. After a non-final page
drains, one transaction clears its IDs/index and sets `page_loaded=0` while
retaining the next token. A loaded drained page with no next token is final and
goes directly to cursor commit, including a valid empty first/final page.

For the ID at `next_index`, production runs the existing scoped
seen/suppression check, bounded metadata fetch, and provider-ID equality check
while retaining both mailbox-operation locks. After metadata it re-reads the
current normalized Watchlist and current selector rows. Recovery admission
authority is `frozen_grants INTERSECT current_local_grants`: a frozen sender
authorizes only while that exact normalized address remains configured, and a
frozen label selector authorizes only while the exact selector ID, provider,
account, mailbox identity, and label ID row still exists. Additions never widen
the frozen side of this intersection.

The common matcher receives only that intersection. If one grant was revoked,
another overlapping frozen-and-current sender or selector may still admit the
message under the normal deterministic winner rules. In the same
`BEGIN IMMEDIATE` transaction that advances `next_index` and
`terminal_candidate_count`, a label admission re-reads selector rows and
recomputes the intersection/winner before atomically inserting message plus
provenance. A sender admission uses the authoritative config read protected by
the still-held operation locks. Exact-sender add/remove is required to acquire
those same locks. Thus a removal committed first rejects pending authority; an
admission committed first is already admitted and remains an immutable
snapshot.

The terminal outcomes are admitted, suppressed/seen, unadmitted, or
provider-gone. A transient metadata/provider error leaves `next_index`
unchanged. A crash after metadata but before the terminal transaction also
leaves the index unchanged and re-evaluates current grants on replay. Existing
seen/suppression/message uniqueness constraints make any committed replay
idempotent.

One normal scheduled recovery loop may cross page boundaries but uses a
monotonic deadline at loop start and completes no more than 200 terminal
candidates or 30 seconds of loop time. Every page/metadata request receives a
timeout no greater than the remaining deadline, and no new request begins when
the deadline is exhausted. The next normal scheduled check resumes the row;
this slice adds no faster timer or polling cadence. There is no total page,
candidate, or session-age completion cap.
Valid page token, current page, index, snapshots, fixed time window, and
replacement cursor remain durable however many scheduled checks are needed.

Transient network/provider errors use `next_retry_at` delays of 1, 2, 4, 8,
and then at most 15 minutes. `consecutive_retry_count` saturates at 31 only to
bound backoff arithmetic; a successful provider call or terminal candidate
clears it and the retry time. Ordinary transient/backoff handling never clears
a valid page token, stored page, or index and never advances the cursor.

A Gmail invalid-page-token response is the narrow exception because that
provider continuation cannot be resumed. After bounded backoff it clears the
page token, stored page, and index, sets `page_loaded=0`, and restarts provider
paging at page zero inside the **same** frozen window. It retains all frozen
grants, bounds, replacement and old cursors, and monotonic counters.
`invalid_page_token_count` increments with overflow protection. The first four
resets wait 1, 2, 4, and 8 minutes and report backoff; the fifth and later
wait at most 60 minutes and report stable degraded status with
`failure_code=gmail_recovery_page_token_invalid` and retry at no more than
60-minute intervals. They continue automatically without cursor advance.
Already admitted/seen/suppressed work is absorbed by existing idempotence;
previously unadmitted IDs may be evaluated again.

A later successful page fetch returns state to `collecting`, clears the visible
failure and retry time, and preserves the monotonic invalid-token diagnostic
count. It resumes ordinary durable processing from the newly stored page.

Stable recovery check-status codes are
`gmail_recovery_snapshot_too_large` (creation rejected, no row),
`gmail_recovery_page_invalid` (malformed or over-200 response, valid progress
retained), `gmail_recovery_page_token_invalid` (provider paging restarted after
backoff), `gmail_recovery_provider_unavailable` (transient progress-preserving
backoff), and `gmail_recovery_counter_overflow` (integrity failure, fail closed).
None authorizes a cursor advance.

Timed recovery page and metadata requests classify OAuth refresh failures at
the gateway boundary. Retryable refresh failures enter the existing
progress-preserving provider-unavailable backoff. Nonretryable refresh rejection
remains `gmail_authorization_rejected` and is never relabelled as transient
backoff. Provider details are not exposed.

Resume does not re-enumerate labels or let new local grants into the frozen
snapshot. Current local grants are nevertheless re-read for every candidate so
removal revokes pending authority. Current message metadata still must contain
`INBOX` and a frozen-and-current opaque label ID. A permanently broken provider
can therefore remain visibly degraded and retrying; this contract promises no
guaranteed completion in that external state, but it never silently accepts a
baseline or advances the cursor.

Only after a final page has no next token and every stored ID is terminal may
one `BEGIN IMMEDIATE` transaction re-read the exact account and selector-set
identity, replace the mailbox cursor with the captured numeric replacement
cursor, and delete the recovery row. An account becoming non-active pauses the
row. Credential/mailbox identity replacement deletes the old row in the
established reconciliation transaction without advancing its cursor. Selector
or sender additions after capture cannot widen the row; removals revoke pending
authority through the intersection above. A retention change cannot rewrite an
open, backing-off, or degraded row; it affects only a newly created recovery
after the existing row succeeds and retires, or after identity replacement
invalidates it.

Recovery is not a save-time or unbounded historical scan. It runs only when the
existing Gmail cursor is stale and only inside its captured window. Adding a
selector during an already-open recovery cannot widen its frozen grants;
events after the captured replacement cursor remain reachable through the next
incremental history check.

### Dry run

Dry-run equivalence is deliberately narrow. It uses the same current active
account, credential-derived identity, complete catalog validation, exact
sender and active-selector snapshot, metadata ordering, common matcher,
current-grant intersection, retention check, cursor-first exclusive-epoch
outward rounding, and
deterministic in-memory admission provenance as production. It performs no
watcher SQLite/config/cursor/provenance/suppression, notification, or
automation writes; existing OAuth cache refresh needed for authentication
remains transport behavior.

For stale history, dry run makes the same broad fixed-window query but reads
only its first page, at most 200 IDs, into memory. It does not create or mutate
`gmail_recovery_state`. If the response has a next page token, the dry-run
result must include `"incomplete": true` and
`"reason": "recovery_truncated"`; it cannot claim an exhaustive preview. The
production/dry-run equality assertion covers the matcher result and provenance
for the same candidate, not completion of a multi-page stale window.

### Activation

`Watcher.check()` is active when either:

- the global exact sender set is nonempty; or
- the current active account is Gmail and has at least one selector row bound
  to the current credential identity.

An open recovery row remains active until it drains or is invalidated by
identity reconciliation, even if the mutable sender/selector configuration
becomes empty, because it must finish against its captured snapshot before the
replacement cursor can commit.

The label-only case opens the Gmail session, validates identity and a complete
catalog snapshot, and continues with zero senders. If all rows are deleted,
known-system, remotely deleted, or identity-mismatched and there are no exact
senders, no new history is read and the result is inactive with an explicit
label-state reason. Existing exact-sender-only configurations do not enumerate
the Gmail label catalog and retain their existing activation behavior, while
their active Gmail incremental request still uses the same broad stable stream.

If current-identity selector rows exist but the catalog cannot be completed,
the discovery round fails closed with retryable
`gmail_label_catalog_unavailable` or stable
`gmail_label_catalog_invalid`, or stable nonretryable
`gmail_authorization_rejected`: no new history/recovery request, body fetch,
model call, cursor advance, or message write occurs. Already admitted pending
work may continue from persisted provenance. A union sender-plus-label
configuration delays new exact-sender discovery in that failed round so the
shared cursor cannot discard a qualifying label event. Sender-only
configurations are unaffected.

## Concurrency, identity, and race outcomes

Selector add/remove, exact-sender Watchlist mutations, account
connect/reconnect/activate/disconnect, production watcher checks, and recovery
state transitions use the existing native mailbox-operation gate and
cross-process operation lock. This orders sender, selector, account identity,
and recovery snapshots. SQLite `BEGIN IMMEDIATE`, selector-set identity, and
revision CAS are the write boundary. Gmail network calls occur under the
operation locks but outside a database transaction; every following write
rechecks exact account, identity, and revision.

| Situation | Required outcome |
|---|---|
| Same label ID under another `account_id` | No match; operations are exact-account scoped. |
| UI active account changes while catalog/list is in flight | Local generation changes, label state clears, and the late response is discarded. Backend rejects a stale-account mutation with `account_not_active`. |
| Same account ID after credential/mailbox identity replacement | Reconciliation updates selector-set identity and increments revision once; old selectors remain visible as `identity_mismatch`, inert, and never rebound. |
| Catalog under identity X, reconnect to Y, then Add same label ID | `stale_revision` or `mailbox_identity_changed`; no selector write and no second revision increment. |
| Frontend supplies name/type/status/identity | `invalid_request`; those values are backend/provider owned. |
| Gmail reports selected ID renamed | It remains active by ID and displays the current name. |
| Complete catalog omits selected ID | Visible `deleted`, inert, with no automatic deletion or widening. |
| Gmail reports selected ID as a known system type | Visible `not_user`, inert. `INBOX` is never selectable. |
| Any item has an unknown provider label type | Reject the whole snapshot as `gmail_label_catalog_invalid`; list reports `invalid_catalog` and current-identity rows as `validation_unavailable`, never `not_user`. No cursor is written. |
| Catalog or network failure | Current-identity selector configurations fail closed and the shared cursor does not advance; exact-sender-only configuration remains usable. |
| Local selector or sender mutation races an ordinary check | Operation locks order them; the check uses the complete snapshot from its side of the lock. |
| Selector or sender addition after recovery capture | It cannot widen frozen grants; it applies to later incremental candidates, including events after the captured replacement cursor. |
| Selector or sender removal before a recovery candidate commits | The current-grant intersection removes its authority. Another overlapping frozen-and-current grant may still admit deterministically. |
| Selector or sender removal after admission commits | Persisted admission provenance remains valid; removal does not revoke completed work. |
| Remote label removed before candidate metadata | Metadata lacks the ID, so no label admission. |
| Remote label removed after metadata matched | Persisted admission provenance remains valid for that one message. |
| Remote label added after delivery | `labelAdded` supplies the candidate; matching metadata admits it once. |
| Event arrives after recovery cursor capture, at the upper sample, or later in that same whole second | Next-second outward rounding includes it in recovery; it may also appear incrementally, and scoped uniqueness admits/analyzes it at most once. |
| Sender and label both match | One message with deterministic exact-sender provenance. |
| Multiple selected labels match | One message with deterministic smallest-selector-UUID provenance. |
| Local Inbox history deletion | Existing scoped suppression prevents rediscovery from `messageAdded`, `labelAdded`, or recovery replay. |
| Legacy v1 continuation or stale numeric cursor | Enter one-row broad recovery; never reinterpret an old narrow-stream offset. |
| Recovery page has more than 200 IDs | Reject the page and write no page progress. |
| Current recovery page is partly processed at crash | Restart from persisted `next_index`; replay safety rests on existing seen/suppression/message uniqueness. |
| Crash occurs after metadata but before the terminal transaction | Index does not advance; replay re-reads current grants and commits at most one terminal result. |
| Gmail rejects a recovery page token | After bounded backoff reset provider paging to page zero in the same fixed window; repeated rejection is visibly degraded and keeps retrying without cursor advance. |
| Page or terminal counter would overflow signed SQLite integer | Stable `gmail_recovery_counter_overflow`; no coercion, terminal advance, or cursor advance. This is an integrity failure, not a normal completion cap. |
| Process restarts during recovery | Resume the one persisted row, current page, next index, token, frozen rules, counters, and retry time. |
| Recovery account becomes non-active | Pause without changing row or cursor. Identity replacement deletes the row in reconciliation without committing its replacement cursor. |
| Dry run stale window has another page | Return `incomplete/recovery_truncated` after the first at most 200 IDs; make no durable recovery writes and claim no exhaustive equality. |

## Fail-first regression plan

These names are normative acceptance tests. Before implementation, each
declared fail-first probe must fail for the stated behavioral reason, not for a
fixture or import error.

### Gmail adapter

- `test_gmail_history_request_is_broad_and_identical_for_sender_label_and_union_configs`
  fails because current history asks only for `messageAdded`, filters by
  `INBOX`, and has no configuration-independent request contract.
- `test_gmail_history_deduplicates_message_and_label_added_in_canonical_order`
  fails because `labelAdded` is not collected.
- `test_gmail_history_v2_resumes_more_than_200_unique_ids_without_skip_or_duplicate`
  fails because the current continuation is the old narrow-stream format.
- `test_gmail_history_v2_rejects_changed_prefix_digest_and_legacy_v1_token_as_stale`
  fails because no broad-stream prefix verification exists.
- `test_gmail_label_added_after_delivery_is_returned_without_inbox_history_filter`
  fails because the current request uses `labelId="INBOX"`.
- `test_gmail_recovery_page_is_one_broad_inbox_window_query_without_rule_filters`
  fails because current recovery builds a sender query and has no page seam.
- `test_gmail_recovery_page_rejects_more_than_200_or_malformed_ids`
  fails because no strict recovery-page decoder exists.
- `test_gmail_recovery_page_rejects_nul_c0_del_and_c1_ids_before_persistence`
  fails because provider message IDs have no decoded Unicode-control guard.
- `test_gmail_catalog_content_length_over_one_mib_rejects_before_body_read`
  fails because the catalog transport has no pre-read size guard.
- `test_gmail_catalog_bounded_reader_accepts_exact_one_mib_and_rejects_plus_one`
  fails because catalog reads are not bounded independently of Content-Length.
- `test_gmail_catalog_accepts_10000_items_and_rejects_10001_whole`
  fails because no decoded item-count guard exists.
- `test_gmail_catalog_canonical_one_mib_accepts_exact_and_rejects_plus_one_whole`
  fails because no normalized-document size guard exists.
- `test_gmail_catalog_item_id_name_utf8_and_escape_boundaries_fail_whole`
  fails because no strict per-item catalog decoder exists.
- `test_gmail_catalog_guard_failure_exposes_no_partial_ui_or_add_authority`
  fails because catalog operations and selector Add do not exist.
- `test_gmail_label_catalog_returns_only_provider_declared_user_labels`
  fails because no label catalog exists.
- `test_gmail_catalog_rejects_unknown_type_partial_duplicate_and_oversized_document_whole`
  fails because no catalog decoder/guard exists.

### Store and matcher

- `test_selector_set_tracks_current_identity_and_replacement_increments_revision_once`
  fails because selector-set identity and reconciliation do not exist.
- `test_repeated_same_identity_reconciliation_is_revision_idempotent`
  fails because there is no selector-set reconciliation.
- `test_gmail_label_selector_cas_bounds_and_immutable_identity`
  fails because selector tables do not exist.
- `test_gmail_label_selector_same_id_is_isolated_by_account_and_mailbox_identity`
  fails because no scoped selector storage exists.
- `test_recovery_state_is_one_strict_account_row_with_bounded_page_and_index`
  fails because durable recovery state does not exist.
- `test_recovery_page_200_max_escaped_ids_is_exactly_205401_and_fits_column`
  fails because the page encoder and 524,288-byte schema bound do not exist.
- `test_recovery_page_json_column_accepts_524288_and_rejects_524289_bytes`
  fails because the revised page column bound does not exist.
- `test_recovery_state_rejects_snapshot_window_identity_and_cursor_mutation`
  fails because the recovery immutability trigger does not exist.
- `test_selector_snapshot_worst_case_quotes_backslashes_and_utf8_fits_one_mib`
  fails because the exact canonical encoder and decoder bounds do not exist.
- `test_valid_sender_snapshot_over_one_mib_fails_without_row_or_cursor_change`
  fails because recovery currently has no bounded sender snapshot.
- `test_recovery_counters_have_no_normal_cap_and_overflow_fails_closed`
  fails because durable monotonic recovery counters do not exist.
- `test_identity_reconciliation_removes_recovery_without_advancing_mailbox_cursor`
  fails because recovery is not identity-bound durable state.
- `test_common_admission_matcher_requires_inbox_and_sender_or_active_label`
  fails because the current gate accepts exact senders only.
- `test_common_admission_matcher_rejects_gmail_selector_for_microsoft_and_imap`
  fails because no provider-scoped matcher exists.
- `test_message_insert_atomically_persists_deterministic_admission_provenance`
  fails because `messages` has no provenance columns.
- `test_deleted_message_suppression_blocks_later_label_added_and_recovery_replay`
  initially fails because label events and broad recovery are absent.

Boundary probes cover zero/one/100/101 selectors; empty/one/512/513-byte label
IDs; empty/one/1024/1025-byte names; revision `-1`, `0`, stale, current,
max-safe integer, boolean, and string; missing/partial identity tuples; system,
USER, unknown, deleted, renamed, and duplicate labels; INBOX absent/present;
and no match/exact-only/label-only/overlapping matches. Catalog probes cover
transport bytes 1,048,575/1,048,576/1,048,577 with present, absent, and
untrusted `Content-Length`; decoded item counts 0/1/10,000/10,001; canonical
bytes cap-1/cap/cap+1; and item ID bytes 0/1/511/512/513 plus name bytes
0/1/1,023/1,024/1,025 using quotes, backslashes, and maximum multibyte UTF-8.

Recovery page probes cover empty/1/200/201 IDs; ID bytes 0/1/511/512/513;
all-quote, all-backslash, and maximum multibyte UTF-8 content; decoded/escaped
NUL `U+0000`, other C0 `U+001F`, `DEL` `U+007F`, and C1 `U+0085`/`U+009F`
rejection before canonicalization or persistence; the exact 205,401-byte
escaped 200-ID worst case; raw column bytes
524,287/524,288/524,289; index zero/last/past-end; and not-loaded versus a
loaded empty final page. Recovery time probes cover exact-integer/fractional
upper and lower instants with same-second injected events. Selector snapshot
probes include all-quote, all-backslash, and maximum multibyte UTF-8 values at
exact byte bounds plus one byte over. The selector limit remains 100 for
storage/UI/matcher bounds even though recovery uses one query. Sender snapshot
overflow does not impose a new global Watchlist limit.

### Watcher and API

- `test_label_only_configuration_runs_check_with_zero_senders`
  fails because `Watcher.check()` currently returns inactive.
- `test_unadmitted_candidates_never_fetch_body_or_attachment_or_call_model`
  fails until the metadata-first common gate exists. This deterministic test,
  rather than installed instrumentation, proves the pre-admission effect order.
- `test_catalog_failure_is_retryable_and_does_not_advance_shared_cursor`
  fails because there is no catalog barrier.
- `test_unknown_label_type_invalidates_whole_polling_snapshot_without_cursor_advance`
  fails because no strict catalog decoder exists.
- `test_dry_run_and_production_choose_same_matcher_result_and_provenance_for_candidate`
  fails because shared provenance does not exist.
- `test_dry_run_stale_recovery_reads_one_page_and_reports_truncated_when_more_exist`
  fails because dry run has no label-aware broad recovery preview.
- `test_mailbox_identity_replacement_leaves_old_selectors_inert_and_bumps_once`
  fails because selectors and selector-set identity do not exist.
- `test_selector_add_fetches_fresh_catalog_and_rechecks_identity_and_revision_before_insert`
  fails because the operation does not exist.
- `test_catalog_x_reconnect_y_add_same_id_has_no_selector_write_or_second_revision_bump`
  fails because identity/revision reconciliation is absent.
- `test_selector_add_rejects_system_deleted_free_form_and_frontend_claims`
  fails because the operation does not exist.
- `test_selector_add_runs_no_search_but_next_check_can_admit_reachable_recent_mail`
  fails because selector Add and label-aware scheduled discovery do not exist.
- `test_selector_remove_is_local_cas_and_works_during_gmail_network_outage`
  fails because the operation does not exist.
- `test_stale_recovery_freezes_sender_selector_identity_window_and_cursor_in_one_row`
  fails because current recovery is one in-memory sender query.
- `test_recovery_cursor_then_exact_integer_sample_keeps_injected_same_second_event`
  fails because current recovery has no normative whole-second conversion.
- `test_recovery_cursor_then_fractional_sample_keeps_injected_same_second_event`
  fails because current recovery has no cursor-first outward-rounded boundary.
- `test_recovery_lower_bound_rounds_strictly_earlier_then_metadata_filters_overlap`
  fails because current recovery has no safe whole-second lower bound.
- `test_recovery_persists_page_before_metadata_and_advances_index_only_on_terminal_outcome`
  fails because current recovery does not persist page/index state.
- `test_recovery_loaded_empty_final_page_commits_without_refetching_page_zero`
  fails because current recovery has no durable page-loaded state.
- `test_recovery_check_stops_at_200_terminal_candidates_or_30_seconds`
  fails because the per-check work bounds do not exist.
- `test_recovery_restart_resumes_current_page_index_token_and_frozen_snapshots`
  fails because recovery state is not stored.
- `test_recovery_selector_removal_before_candidate_revokes_frozen_grant`
  fails because current recovery has no frozen/current grant intersection.
- `test_recovery_sender_removal_before_candidate_revokes_frozen_grant`
  fails because Watchlist mutation is not ordered with recovery admission.
- `test_recovery_removal_after_admission_does_not_revoke_provenance`
  fails because persisted admission provenance does not exist.
- `test_recovery_addition_does_not_widen_frozen_grants`
  fails because no captured recovery grant ceiling exists.
- `test_recovery_addition_applies_to_incremental_events_after_replacement_cursor`
  fails because current recovery has no frozen-to-incremental handoff contract.
- `test_recovery_overlapping_remaining_grant_still_admits_deterministically`
  fails because recovery has no shared intersection matcher.
- `test_recovery_crash_after_metadata_before_terminal_transaction_rechecks_revocation`
  fails because page progress and admission do not share a terminal transaction.
- `test_recovery_invalid_token_resets_same_window_with_bounded_retry`
  fails because page reset and retry state do not exist.
- `test_recovery_repeated_invalid_tokens_surface_degraded_and_keep_retrying`
  fails because degraded retry state does not exist.
- `test_recovery_transient_backoff_preserves_valid_page_token_page_and_index`
  fails because current recovery has no durable valid progress.
- `test_recovery_continues_past_old_page_candidate_and_age_thresholds`
  fails because current recovery is not durable across scheduled checks.
- `test_recovery_replacement_cursor_waits_for_final_page_and_terminal_candidate`
  fails because current cursor replacement has no durable drain barrier.
- `test_label_removal_and_add_races_follow_metadata_snapshot`
  fails until selector snapshots and provenance exist.

### Desktop

- Rust engine contract tests prove exact operation names, payloads, result
  decoding, fresh-catalog Add failures, and malformed status/revision/item
  rejection.
- Tauri command tests prove the frontend cannot pass mailbox identity, label
  type/name/status, or an account fallback.
- Frontend tests prove binding to only the backend active Gmail account,
  clearing and invalidating in-flight results on account change, forwarding
  only the opaque label ID plus revision, live catalog-only selection,
  whole-catalog unknown-type failure rendering, selected/invalid list
  rendering, stale-revision reload without automatic mutation retry, visible
  label-only active/polling state, visible bounded-recent-catch-up copy before
  Add, and no free-form account, domain, or label-ID input.

Every guard-shaped test includes both rejection directions and proves the
downstream path uses the validated provider value rather than a raw frontend
claim.

## Installed Linux proof

Acceptance requires one installed Linux package, its packaged UI, packaged
engine sidecar, production scheduler, a real test Gmail account, and synthetic
data. Record the installed package version, implementation commit, package and
sidecar SHA-256 digests, and scheduler identity before the first action and
again after the last; the digests must be unchanged. No source import, test
runner, direct SQLite/config write, substituted engine, development command,
manual engine request, or second manually initiated check may participate. All
check invocations come from the unchanged installed scheduler. No send
authorization is installed, and the app performs no Gmail mutation.

1. Through the packaged Watchlist UI, remove every global exact sender. The
   packaged UI must visibly show zero exact senders.
2. Connect the test Gmail account in packaged Settings and refresh its catalog,
   but do not select the proof label yet. Immediately after a recorded scheduled
   check, deliver a synthetic unlisted-sender PDF, place the real USER label on
   it through Gmail's ordinary UI, and then select that label before the next
   scheduled check. Settings must show the bounded-recent-catch-up copy. Add
   causes no immediate analysis; the next scheduled check may and, with this
   deliberately reachable post-cursor event, must admit it once with label
   provenance. This is recent cursor catch-up, not a save-time mailbox scan.
3. The packaged UI must visibly show the exact active account, selected label,
   and **active/polling label-only** state while sender count remains zero.
   Record only account display identity, selector UUID, opaque label-ID digest,
   and revision; record no credential, raw identity key, or token.
4. Deliver a second synthetic unlisted-sender PDF into INBOX without the
   selected label. Wait for the next scheduled check. It must not appear in the
   packaged Inbox, and the local model server's external access log must show no
   model request in that scheduled-run window. Apply the selected label after
   delivery, then wait for the following scheduled check only. It must observe
   `labelAdded`, admit and analyze the message exactly once, and show Gmail-label
   admission provenance. The external model access log corroborates that one
   analysis.
5. Immediately after another scheduled check, deliver and label a third
   synthetic unlisted-sender message, then remove the selector through packaged
   Settings before the next scheduled check. That check must not admit it or
   call the model. Re-add the selector only after that check has advanced past
   the now-unadmitted event; the label-only active state must return. Code tests,
   rather than this incremental observation, own the exact open-recovery
   revocation race.
6. Restart the installed app without changing the artifact. Packaged Settings
   must preserve the current selector and label-only polling state; later
   scheduled checks must not add another analysis or model request for an
   admitted message.
7. Use the packaged Inbox's existing Clear/delete action for the second admitted
   row. In Gmail's user UI, remove and re-add the selected label to produce a
   new history event, then wait for the next scheduled check. The row must
   remain suppressed and the external model log must show no new request. No
   database action is part of this proof.
8. Deliver separate synthetic unlisted-sender cases with no selected label, a
   lookalike USER-label name under another opaque ID, and Gmail system labels
   including INBOX. After scheduled checks, none may appear in the packaged
   Inbox or cause a model request.
9. Rename the selected USER label through Gmail's UI and refresh the packaged
   catalog: the same opaque ID remains selected under its new display name.
   Delete it remotely and refresh again: the durable selector remains visible
   as invalid/inert and label-only polling no longer claims it active.

Evidence consists of unchanged artifact digests, scheduled-run timestamps,
packaged UI screenshots, Inbox provenance and suppression observations, and
external local-model access counts. Synthetic sender, subject, body, PDF, and
label names are used. No customer mail, send credential, or send effect is
allowed. This installed proof does not claim visibility into body or attachment
fetches because the product exposes no such buyer surface. The deterministic
fail-first code test owns proof that an unadmitted candidate cannot fetch body
or attachment content before admission.

## Non-goals and deferred work

- Authenticated sender-domain admission and its evidence model.
- Microsoft 365 folders/categories and IMAP folders/keywords.
- A cross-provider label or folder abstraction.
- Gmail label creation, rename, deletion, or message-label mutation.
- An unbounded historical or full-mailbox scan when a selector is saved;
  bounded recent candidates still reachable from normal cursor/recovery state
  are intentionally eligible on the next scheduled check.
- Automation dispatch or new automation rule behavior.
- OCR or document-provider chaining.
- Send authorization, outbound mail, or any provider write scope.
- UI styling, bulk management, search, sorting preferences, onboarding polish,
  analytics, and other hardening that is not required by this vertical proof.

## Planned implementation surface

The implementation is expected to touch the existing seams below; this is a
review map, not authorization:

- `src/eom_email_watcher/gmail.py`: bounded whole-document label catalog,
  two history event types, stable broad v2 continuation, and one paged broad
  recovery query with canonical bounded page IDs.
- `src/eom_email_watcher/mailbox.py`: the Gmail-specific recovery-page seam
  needed by the service; no provider-neutral label abstraction and no change
  to `MailboxChanges` for other providers.
- `src/eom_email_watcher/service.py`: active condition, selector snapshot,
  common matcher, cursor-first exclusive-second recovery bounds, frozen/current
  grant intersection, per-check work budget, durable retry/backoff, and
  admission ordering.
- `src/eom_email_watcher/db.py`: selector-set identity and revision, selector
  CAS, 1 MiB snapshots, the 524,288-byte page JSON column, the single recovery-
  state row and monotonic counters, provenance, triggers, and projections.
- `src/eom_email_watcher/engine_api.py`: the four exact label operations,
  existing Watchlist mutations ordered through the mailbox-operation locks,
  and public Inbox provenance.
- `desktop/src-tauri/src/engine.rs` and `desktop/src-tauri/src/lib.rs`: typed
  engine methods and commands.
- `desktop/src/main.ts`: minimal catalog/add/list/remove surface, bounded-recent-
  catch-up copy, and provenance.
- focused Gmail, service, database, engine, Rust, frontend, and installed-proof
  tests.

No separate matcher, selector store, or UI-owned admission rule may be added.

## Landing gate

This contract may move from Draft to Accepted only after hostile review proves:

1. every normative result and failure above is unambiguous;
2. provider/account/mailbox identity cannot be supplied or widened by the
   frontend;
3. the history request observes post-delivery label additions without relying
   on label names or senders and has stable, verified continuation semantics
   independent of selector changes;
4. catalog transport, decoded-count, item, and normalized-document guards
   accept exact limits, reject limit plus one, and expose no partial UI or
   selector authority;
5. recovery page encoding admits the exact 205,401-byte worst case within its
   524,288-byte column while rejecting invalid count, ID, and column bounds;
6. recovery uses one broad fixed-window query, persists each bounded page
   before processing, rounds both Gmail epoch bounds outward after cursor-first
   sampling, preserves valid progress without a normal total-work cap, and
   retries invalid tokens visibly without cursor loss;
7. frozen grants bound additions while intersection with current local grants
   makes selector/sender removal authoritative before the atomic terminal
   transaction, including overlapping-grant and crash replay cases;
8. fresh-catalog Add, identity replacement, cursor boundary, local-delete,
   revision CAS, snapshot overflow, counter overflow, backoff, and truncated
   dry-run outcomes are fail-closed without losing qualifying events;
9. no body, attachment, model, notification, Connect, or automation effect is
   reachable before admission;
10. existing exact sender configurations and current scoped deletion
   suppression remain working;
11. Add starts no immediate/full-mailbox scan, while Settings and proof honestly
   describe bounded recent catch-up on the next scheduled check; and
12. the installed Linux proof is feasible with read-only Gmail authorization
   and synthetic data through one unchanged installed artifact and its normal
   scheduler.

Implementation must land after the accepted contract as a separate commit and
must name that accepted commit. The implementation PR remains blocked until
all fail-first tests fail for their declared behavioral reason, focused local
checks pass, required CI is green or its exact failure is reproduced and fixed
at the root, hostile review is reconciled, and the installed Linux proof is
recorded. Windows installed proof is outside this Linux slice.

## Revision log

- Accepted 2026-09-19 — Draft 6 passed independent code, security/concurrency,
  and convergence/buyer-proof review with no remaining BLOCKER or MAJOR.
- Draft 6 — fifth hostile-review revision: provider message IDs now reject the
  same decoded Unicode `Cc` control characters as label IDs/names before page
  canonicalization or persistence; the quote/backslash worst-case bound remains
  205,401 within 524,288 bytes. Pending hostile review; no implementation
  authorized.
- Draft 5 — fourth hostile-review revision: recovery page JSON now has a
  demonstrated escaped-ID worst case and 524,288-byte schema bound; Gmail
  catalog validation now has exact transport, decoded-count, per-item, and
  canonical-document limits with whole-snapshot rejection. Pending hostile
  review; no implementation authorized.
- Draft 4 — third hostile-review revision: exact outward whole-second Gmail
  recovery bounds, durable progress without deterministic total-work caps,
  honest bounded recent catch-up, demonstrated 1 MiB snapshot bounds, and
  frozen-grant intersection with current local revocation at the atomic
  terminal transaction. Pending hostile review; no implementation authorized.
- Draft 3 — second hostile-review revision: selector-set identity plus revision
  reconciliation, fresh-catalog Add, one-row broad-query recovery with durable
  page/index and automatic bounded retry, cursor-first overlap safety,
  first-page-only truncated dry run, and buyer-visible installed proof without
  proof-only product surfaces. Pending hostile review; no implementation
  authorized.
- Draft 2 — first hostile-review revision. Its selector mutation and recovery
  architecture is superseded by later drafts. The stable broad incremental stream,
  fail-closed unknown label types, current-active-account-only UI, and
  unchanged-artifact proof requirements remain. Pending hostile review; no
  implementation authorized.
- Draft 1 — code-grounded contract for Gmail USER-label admission; exact
  sender union, credential identity scoping, live catalog validation, CAS
  selector storage, `labelAdded` discovery, bounded recovery, deterministic
  provenance, minimal UI, and installed Linux proof. Pending hostile review;
  no implementation authorized.
