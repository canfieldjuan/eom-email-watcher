# Body Truncation Signal (#146)

## Why this slice exists

Every analysis reads at most `body_char_limit` characters of the message body. The default is 20,000, and config allows 1,000 to 100,000 (`src/eom_email_watcher/config.py:457-462`). When a body is longer, the summary, priority, action, and deadline are computed on a prefix. Nothing records that this happened, and nothing tells the user. Long vendor threads and forwarded chains are where a late deadline or changed term sits past the cut.

A read-only investigation of `origin/main` at `b65d34a` found the following.

- **There are four cut sites, all `normalized[:limit]`.** Each one has already computed the full normalized text, so the pre-cut length is free to report.
  - Gmail: `mime.py:139`.
  - IMAP: `imap.py:1193` and `imap.py:1748`.
  - Microsoft 365: `microsoft365.py:337`.
- **`MessageContent` has no field that could carry a truncation signal** (`mailbox.py:67`).
- **The only per-message record of the limit is erased on success.**
  - `analysis_body_char_limit` is a reservation field.
  - `mark_analyzed` sets it to `NULL` (`db.py:11180`), so an analyzed row keeps no trace of the limit.
- **The gateway's 100,000-character body cap (`model.py:60`) is unreachable.** The configured limit already bounds the body at 100,000 or less before the model sees it.
- **The gateway cuts other fields, but the user still sees them in full.**
  - It cuts the subject at 4,096 characters (`model.py:710`) and keeps at most 100 attachment names (`model.py:61`).
  - These are model-input cuts only. The inbox shows the stored, uncut subject and every attachment.
- **The issue's second remedy is contradicted by code: "prefer trimming quoted history before new text".**
  - Clients put new text above the quote in a top-posted reply, so a head cut already removes quoted history first. A long quoted thread cannot crowd out new text that comes before it.
  - In a bottom-posted reply, `_split_quoted_history` (`model.py:288`) classifies everything after the first `On ... wrote:` marker as quoted history, including the reply. Reordering the budget cannot rescue that text.
  - So the remedy changes nothing for top-posted replies and cannot help bottom-posted ones. This slice pins the top-posted behavior with a test instead of changing it.

### Problem-derived contract

- **Root cause:** each provider adapter discards the body's pre-cut length at the cut. `MessageContent`, the store, the inbox API, and both notification paths therefore cannot distinguish "analyzed the whole email" from "analyzed the first N characters".
- **A correct fix must:**
  - carry the pre-cut length from every adapter, through one shared owner of the normalize-and-cut rule;
  - persist it atomically with the analysis it describes;
  - expose it on `inbox.query` through every hop: store, engine, Rust `InboxItem`, and TypeScript;
  - render it in the inbox;
  - mark both notification paths.
- **It must not change:**
  - the cut itself (head cut at `body_char_limit`);
  - the model prompt or schema;
  - gateway request content or identity;
  - admission, retention, scheduling extraction, Connect, or automation behavior.

## Scope (this PR)

Ownership lane: analysis-truncation-signal
Slice phase: record and surface an existing silent limit; no analysis behavior change

1. One shared owner computes the cut body and its pre-cut length for all four adapter sites.
2. The truncation facts are persisted with the stored analysis, under a schema bump to 28.
3. `inbox.query` exposes them, and the desktop renders them.
4. Analysis notifications on both paths carry a fixed note when the analysis was partial.

### Observable behavior

Engine and store:

1. `MessageContent` gains a required `body_source_chars: int`. This is the length of the normalized body before the cut, and `body` stays the cut text. `len(body) == min(body_source_chars, limit)` holds at every adapter.
   - The field is required, with no default. An adapter or fake that omits it fails construction rather than silently reporting "not truncated".
2. The four sites call one helper in `mime.py`. It takes the selected text and the limit, and returns the cut text and the pre-cut length. It performs today's exact line-strip, blank-line drop, and head cut. The four inline copies of that rule are removed.
3. `mark_analyzed` stores `analysis_body_chars = len(body)` and `analysis_body_source_chars = body_source_chars`, in the same transaction as the summary.
4. `inbox.query` items gain three fields:
   - `body_analyzed_chars`: integer or `null`;
   - `body_source_chars`: integer or `null`;
   - `body_truncated`: `true`, `false`, or `null`, derived by the engine as `source > analyzed`.
   - All three are `null` for messages not yet analyzed and for messages analyzed before this change. No value is backfilled.

Notifications:

5. When `body_truncated` is true, the analysis notification body gains one final fixed line: `Summary covers only the beginning of a long email.`
   - The line carries no counts or email text.
   - It appears on both paths: `send_analysis` (`notifications.py:129`, CLI and service delivery, desktop popup and ntfy) and `_notification_payload` (`engine_api.py:5718`, host delivery).
   - Fallback and review notifications are unchanged.

Desktop:

6. A row whose `body_truncated` is true shows a "Partial summary" badge in its collapsed row.
7. The expanded card shows: `Summary based on the first 20,000 of 54,321 characters. Read the full email in your mail app.` The numbers come from the item, with locale digit grouping.
8. A row with `false` or `null` shows neither.
9. The ntfy disclosure (`desktop/src/main.ts:727` and `README.md` "ntfy" section) adds "and, when the summary covers only part of a long email, a fixed note saying so." (See decision D1.)

### Invariants

- The stored counts always describe the analysis stored with them.
  - They are written in the same `UPDATE` as the summary.
  - No path re-analyzes an analyzed message. `requeue_analysis` accepts only pending, permanently paused rows (`db.py`, `requeue_analysis`), which carry no summary.
  - Rows leave the table only by delete or purge, which take the counts with them.
  - A store guard rejects a write where exactly one count is set, where `analyzed > source`, or where either count is negative.
- `body_truncated` is never `true` unless the stored analysis was computed on a strict prefix of the normalized body.
- A `null` count is never shown as "not truncated" or as "partial". The UI renders nothing for unknown.
- The prompt, the gateway request, and the reservation are byte-identical to today for the same message. This slice adds no model input.
- No email text reaches the new notification line or the badge.

### Concurrency

- Counts and summary commit in one transaction (`mark_analyzed`). A reader sees both or neither.
- A retry reserves the same `body_char_limit` (`reserve_analysis_request`). Only the attempt that succeeds writes counts, from the content it actually analyzed.
- This slice adds no new writer. The counts change only inside `mark_analyzed`, under its existing pending-state check.

### Failure cases

- **An adapter returns content without `body_source_chars`:** construction fails, so the message takes the existing analysis-failure path. Tests prove every adapter sets it.
- **The store guard rejects inconsistent counts:** `mark_analyzed` raises, the transaction rolls back, and the message stays pending for retry. It is never stored with a summary and no counts.
- **The Rust host drops unknown fields:** `InboxItem` has no `deny_unknown_fields` (`desktop/src-tauri/src/engine.rs:1131`). The three fields must be declared there, and the typed contract test must carry them.

### Closure Declaration

- **`body_truncated`:** CLOSED as `true | false | null`, derived only from the two stored counts.
- **Notification note:** one fixed string. There is no open vocabulary.

### Boundary-change enumeration

- **New stored columns:** `messages.analysis_body_chars INTEGER` and `messages.analysis_body_source_chars INTEGER`, added by `ALTER TABLE ... ADD COLUMN` under `SCHEMA_VERSION = 28`. An older binary refuses the database, as with every prior bump; the latest was 27 in #175.
- **Values at the boundary:**
  - source equals the limit: not truncated;
  - source is the limit plus 1: truncated;
  - empty body: `0/0`, not truncated;
  - the 1,000 minimum and the 100,000 maximum limit;
  - legacy rows: `NULL/NULL`, rendering nothing.
- **New API output:** three nullable fields on each `inbox.query` item. There is no new input.

### Files touched

- `src/eom_email_watcher/mailbox.py`, `mime.py`, `gmail.py`, `imap.py`, `microsoft365.py`
- `src/eom_email_watcher/service.py`, `db.py`, `engine_api.py`, `notifications.py`
- `desktop/src-tauri/src/engine.rs`
- `desktop/src/main.ts`, `desktop/src/inboxBodyTruncation.ts` (new), `desktop/src/styles.css`
- `desktop/test/inboxBodyTruncation.test.ts` (new)
- `README.md`, `docs/ENGINE_API.md` (the `inbox.query` contract section)
- `tests/test_mime.py`, `test_imap.py`, `test_microsoft365.py`, `test_service.py`, `test_db.py`, `test_engine_api.py`, `test_notifications.py`, and every test fake that constructs `MessageContent`

## Mechanism

- **`mime.py`:** a helper returns `(text, source_chars)`. `extract_body` and the three other sites call it and build `MessageContent` with `body_source_chars`.
- **`db.py`:**
  - `SCHEMA_VERSION = 28` and the two `ADD COLUMN`s on the existing idempotent path.
  - The guard follows the repo's trigger style.
  - `mark_analyzed` gains keyword-only `body_chars` and `body_source_chars`, kept separate from the model result dict so rule matching (`automation/rules.py`) sees nothing new.
  - `query_inbox`, `pending_delivery`/`AnalyzedMessage`, and `NotificationIntent` select the counts.
- **`service.py`:** passes the counts from `content` to `mark_analyzed` and to `_deliver_analysis`. Stored-row delivery reads them from `AnalyzedMessage`.
- **`notifications.py` / `engine_api.py`:** a shared predicate and the fixed line appended when it is true.
- **Desktop:** `inboxBodyTruncation.ts` is a pure function from an item to badge and note text, or nothing. `main.ts` renders it. `engine.rs` adds the three fields.

## Decisions for review

- **D1 (privacy, operator's call).** The fixed note reaches ntfy, so the disclosure wording grows by one clause. The acknowledgement is a boolean, not versioned (`config.py:514`). Existing acknowledgements therefore stay valid, and users are not asked again.
  - Recommended: send the note on both channels and update the wording. The note reveals only that the email was longer than the configured limit.
  - Alternative: desktop-only note, with ntfy and its disclosure unchanged.
- **D2.** Legacy rows stay `null`. No in-app path re-analyzes an analyzed message, so they stay "unknown" (nothing rendered) until retention purges them, at most `retention_days`. A backfill would need a new re-analysis feature, and that is out of scope.

## Intentional

- The cut position and size are unchanged. This slice makes the existing limit visible; it does not raise or redistribute it.
- No counts in notifications, so phone and lock-screen text stays content-free beyond today's fields.
- No prompt change. Telling the model the text was cut changes classification behavior and needs the real-email evaluation in #183 before it ships.

## Deferred

Parking predicate: anything that does not make an existing body cut visible to the user is parked.

- **Prompt awareness of truncation:** after #183 provides a labeled set to measure it.
- **Gateway subject and attachment-name cuts:** model-input only, and the inbox shows full values. A general "model input was bounded" signal can follow if evaluation shows it matters.
- **Scheduling extraction truncation** (`service.py:468`): it has strict evidence validation and manual review. Recording its counts is a separate surface.
- **The quote-marker straddle edge.** If the cut lands inside a multi-line Outlook header block, `_split_quoted_history` does not match, and the partial header joins `current_message_text`. Fixing it needs split-before-cut at the provider boundary.
- **Thread-level budgets for the thread-view feature.**

## Verification (settling evidence)

Fail-first: each behavior test must fail on `b65d34a`.

- **Adapters:** for each of Gmail (`extract_body`), the two IMAP paths, and Microsoft 365, test bodies of limit−1, limit, and limit+1 normalized characters, plus an empty body. Assert the exact `body` text (unchanged from today) and `body_source_chars`. HTML-only and multi-part plain bodies are counted after normalization.
- **Quoted history:** a top-posted reply whose quoted history crosses the cut keeps `current_message_text` intact. This pins the contradicted remedy.
- **Store:**
  - counts persist with the summary;
  - the guard rejects one count set, `analyzed > source`, and negative values;
  - delete and purge remove the counts with their row;
  - a v27 database migrates to 28 with `NULL` counts;
  - a v28 database is refused by version-27 code, matching the existing newer-version test pattern.
- **Engine:** `inbox.query` returns the three fields for truncated, untruncated, unanalyzed, and legacy rows.
- **Notifications:** both paths append the fixed line exactly when truncated, and never on fallback or review notifications.
- **Rust:** the typed contract test round-trips the three fields.
- **Desktop:** `inboxBodyTruncation` covers true, false, null, missing fields, and digit grouping. Source wiring renders the badge and note.
- **Gateway identity:** for the same message, the prompt and request body are byte-identical before and after the change.
- **Gates:** `uv run ruff check .`, the full `uv run pytest` (CI's command), and the desktop and Rust suites CI runs.
