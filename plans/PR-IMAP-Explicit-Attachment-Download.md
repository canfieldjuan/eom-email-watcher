# IMAP Explicit Attachment Download

## Why this slice exists

Issue #133 identifies a first-release privacy contradiction: the README says attachment content is downloaded only after an explicit attachment action, while ordinary IMAP analysis fetches `BODY.PEEK[]` and decodes every attachment to build descriptors.

### Problem-derived contract

- Root cause: IMAP body analysis and attachment discovery share one whole-message fetch and one parser result, so descriptor creation depends on already-downloaded attachment payloads.
- Review-follow-up root cause: the selective implementation did not close four representation and resource boundaries. BODYSTRUCTURE sizes were accepted beyond SQLite's signed 64-bit range, section fetches accepted only literal-backed nstrings even though IMAP also permits quoted strings, Connect treated every IMAP descriptor as pre-fetch-compatible even when the adapter's local safety ceiling made retrieval impossible, and normal analysis applied the 50 MiB fetch limit independently to every selected text section instead of to their aggregate actual bytes.
- Correct fix: normal analysis must derive a bounded MIME catalog from server metadata, fetch only non-attachment text sections needed for the analysis body, and build attachment descriptors without requesting or decoding attachment sections. Explicit retrieval must resolve the same stable attachment identity and fetch its bytes on demand, including a bounded MIME-header fetch when a non-root multipart attachment needs its wrapper. Catalog decoding must preserve RFC 2231/2047 filenames, text decoding must preserve the UTF-8 replacement fallback, and empty successful UID FETCH results must remain unavailable rather than permanent failures. BODYSTRUCTURE sizes must fit the persistence range; section responses must accept exactly one identity-matched literal or quoted nstring and reject malformed, ambiguous, oversized, or wrong-section data; normal analysis must debit each actual text-section response from one cumulative fetch budget and pass only the remainder to the next fetch. Export and Connect callers must treat IMAP BODYSTRUCTURE size as provider-reported metadata, exclude locally unfetchable descriptors from capability results and before new invocation provider selection, reject a descriptor that crosses the ceiling under the source lock, block an active job before any required local refetch, validate actual bytes after explicit retrieval, and retain exact descriptor-size equality for Gmail and Microsoft 365.
- Must not change: Gmail or Microsoft 365 adapters, their exact descriptor-size checks, read-only IMAP selection and PEEK semantics, descriptor-only SQLite persistence, sender admission, Connect job artifact integrity, or the existing `mime-<position>` identity presented to callers.
- Root versus symptom: this fixes the coupling at its source; changing README copy alone or discarding decoded bytes after the whole-message fetch would treat only the visible symptom.

## Scope (this PR)

Ownership lane: email-watcher-release-privacy

Slice phase: First Public Release blocker

1. Replace normal IMAP whole-message reads with bounded BODYSTRUCTURE metadata plus selective text-part reads.
2. Preserve stable attachment positions and fetch attachment data only from `attachment_bytes`.
3. Add positive, negative, mixed-MIME, and malformed-response regression coverage.
4. Correct the provider overview and attachment-fetch wording to describe verified Gmail, Microsoft 365, and IMAP behavior.
5. Normalize IMAP’s non-exact descriptor size at explicit export and Connect admission boundaries, then validate the fetched bytes against the destination capability’s real limit.
6. Close persistence-range, quoted-section, and local-fetchability boundaries raised by the current-head review.
7. Bound aggregate actual bytes across all selected text sections during normal analysis.

### Review Contract

- Ordinary `ImapGateway.content` never issues `BODY.PEEK[]` or any fetch for a catalogued attachment section; settled by `tests/test_imap.py::test_metadata_and_content_skip_attachment_payload_sections` and inspection of `src/eom_email_watcher/imap.py`.
- BODYSTRUCTURE is the single descriptor catalog, is bounded by MIME depth/part/metadata limits, and fails closed on malformed or unrecognized structure; settled by focused parser boundary tests in `tests/test_imap.py`.
- BODYSTRUCTURE sizes at SQLite's signed 64-bit maximum are admitted while the next integer is rejected as `imap_bodystructure_invalid`; settled by a boundary test in `tests/test_imap.py`.
- Mixed messages fetch admitted text sections and retain stable `mime-<position>` attachment descriptors from server-reported type, filename, and size; settled by the normal-content tests.
- `ImapGateway.attachment_bytes` resolves the stable descriptor position against a fresh catalog and returns decoded bytes only after the explicit call; settled by `tests/test_imap.py::test_attachment_bytes_reuses_stable_mime_position` plus attachment-shape controls.
- A non-root multipart attachment remains a parseable MIME entity with its Content-Type boundary wrapper, and its MIME headers are fetched only during explicit retrieval; settled by `tests/test_imap.py::test_non_root_multipart_attachment_export_preserves_wrapper_and_boundaries`.
- RFC 2231 single/continued filename parameters and RFC 2047 encoded words produce decoded attachment names without fetching attachment bodies; settled by `tests/test_imap.py::test_bodystructure_decodes_extended_and_encoded_attachment_filenames`.
- An unknown declared text charset falls back to UTF-8 replacement, while an empty successful BODYSTRUCTURE UID FETCH raises `MailboxMessageUnavailable`; settled by focused tests in `tests/test_imap.py`.
- Section fetches accept identity-matched literal and quoted nstrings, including escaped and empty quoted values, while wrong-section, ambiguous, NIL, and over-limit responses fail closed; settled by focused tests in `tests/test_imap.py`.
- Normal analysis passes only the remaining 50 MiB aggregate allowance to each selected text-section fetch, so an exact-boundary multipart body passes while the next byte raises `imap_message_too_large`; settled by a focused under-reported BODYSTRUCTURE test in `tests/test_imap.py`.
- IMAP export and Connect admission do not compare decoded bytes to non-exact BODYSTRUCTURE size; they use actual bytes for final capability limits and job artifacts, while Gmail/Microsoft mismatches still fail closed; settled by focused `tests/test_engine_api.py`, `tests/test_connect_engine_api.py`, and `tests/test_connect_v2_engine_api.py` controls.
- Connect capability discovery returns no compatible items for an IMAP descriptor above the adapter's local fetch ceiling, and new generic and legacy invocations reject it before opening the mailbox or selecting a provider; the exact ceiling remains admitted and uses the explicit-fetch path. The generic source-lock recheck rejects a descriptor that crosses the ceiling before fetch or queueing. Settled by focused `tests/test_connect_v2_engine_api.py` and `tests/test_connect_engine_api.py` controls.
- IMAP remains read-only and uses PEEK; settled by existing session and source-flag tests plus `tests/test_imap.py` query assertions.
- README provider and attachment claims match the resulting code paths; settled by direct README/code comparison.
- Reachability proof: the public mailbox gateway `content` and `attachment_bytes` entrypoints are exercised, with analysis text/descriptors and explicit bytes as the observable results.
- Affected surfaces: IMAP MIME discovery, analysis-body fetch, attachment refetch, explicit export/Connect size admission, focused tests, README claims.
- Risk areas: hostile BODYSTRUCTURE parsing, transfer encodings, nested MIME/message parts, server size lies, query injection, identity drift between analysis and later explicit retrieval.
- Triggered reviewer rules: R1, R2, R3, R5, R6, R7, R10, R11, R14.

### Guard-class closure

- Decision-driving set: every BODYSTRUCTURE node reachable within the configured MIME depth and part-count bounds.
- Choke point: one bounded parser/catalog builder classifies each node as an attachment container, multipart container, admitted text leaf, or ignored non-text leaf.
- Safe default: malformed, over-limit, or structurally ambiguous metadata raises a categorized message failure; unrecognized non-attachment leaf types are not fetched for analysis.
- Mixed input: admitted text and attachment siblings are handled independently, and an attachment container prevents all descendants from joining the analysis body.
- Constructed metadata: attachment position, synthesized filename, media type, server-reported byte size, and section selector all derive from the validated catalog rather than raw query text.

### Files touched

- `plans/PR-IMAP-Explicit-Attachment-Download.md`
- `src/eom_email_watcher/imap.py`
- `src/eom_email_watcher/engine_api.py`
- `tests/test_imap.py`
- `tests/test_imap_greenmail_integration.py`
- `tests/test_engine_api.py`
- `tests/test_connect_v2_engine_api.py`
- `tests/test_connect_engine_api.py`
- `README.md`

## Mechanism

The adapter requests UID plus bounded BODYSTRUCTURE metadata, parses its IMAP data items into a validated MIME catalog, decodes standards-based filename parameters, and records server section selectors internally. The catalog admits only sizes that SQLite can persist. Normal content retrieval requests only selected text sections with `BODY.PEEK`, debits each actual response from one aggregate allowance, decodes one exact UID-and-section-matched literal or quoted nstring according to catalogued transfer metadata with the prior unknown-charset fallback, and builds descriptors without payload bytes. Explicit attachment retrieval repeats the catalog lookup for the message, selects the stored attachment position, and requests that section before decoding it; non-root multipart attachments additionally fetch their bounded `.MIME` header section and reassemble the MIME entity. Engine callers first exclude an IMAP descriptor that exceeds the adapter's local fetch ceiling, use the smallest admissible size only for the remaining pre-fetch capability probe, then apply capability limits and artifact identity to the actual fetched bytes; other providers retain exact descriptor equality.

## Intentional

- Public attachment IDs remain `mime-<position>` so persisted descriptors and Connect callers keep their current contract.
- Descriptor byte size is the server-reported BODYSTRUCTURE octet count because exact decoded payload size cannot be learned without downloading the attachment. Some servers include MIME-part overhead in that count, so it is metadata rather than an exact downloaded-payload length invariant.
- Unknown non-text leaves stay out of analysis; they are not speculative body fallbacks.
- Gmail, Microsoft 365, database persistence, and provider-side Connect protocol code are outside this fix.
- Connect job construction and cryptographic artifact checks remain unchanged; only pre-job IMAP size interpretation changes.
- Completed and active Connect job reconciliation semantics remain unchanged; an active job may query its selected provider, but cannot refetch an attachment that exceeds the local IMAP ceiling.

## Deferred

Parking predicate: adjacent provider features, UI polish, and hardening without a concrete issue #133 failure path are parked by default.

Parked hardening: none.

## Verification

- Baseline: `uv run pytest -q tests/test_imap.py`
- Focused: `uv run pytest -q tests/test_imap.py`
- Provider regression: `uv run pytest -q tests/test_gmail.py tests/test_microsoft365.py tests/test_db.py`
- Explicit consumer regression: `uv run pytest -q tests/test_engine_api.py tests/test_connect_engine_api.py tests/test_connect_v2_engine_api.py`
- Full Python suite: `uv run pytest -q`
- Static: `uv run ruff check .`
- Diff: `git diff --check`
- Real-server proof: `bash scripts/test-imap-greenmail.sh`
- Review follow-up: focused multipart-wrapper, filename-decoding, unknown-charset, and expunged-message regressions in `tests/test_imap.py`
- Current-head fail-first: persistence maximum-plus-one, quoted section nstrings, over-limit discovery, generic invocation, legacy invocation, and aggregate actual text bytes all reproduced before the fixes.
- Current-head focused: `uv run pytest -q tests/test_imap.py tests/test_connect_v2_engine_api.py tests/test_connect_engine_api.py -k 'aggregate_actual or sqlite_maximum or quoted_nstrings or quoted_section_fetch or mismatched_literal_length or local_fetch_ceiling or actual_download_size'`
- Current-head consumer suites: `uv run pytest -q tests/test_imap.py tests/test_connect_engine_api.py tests/test_connect_v2_engine_api.py tests/test_engine_api.py`
- Current-head full Python suite: `uv run pytest -q`
- Current-head static: `uv run ruff check .`
- Current-head real-server proof: `bash scripts/test-imap-greenmail.sh`
- Format: `uv run ruff format --check src/eom_email_watcher/imap.py tests/test_imap.py tests/test_imap_greenmail_integration.py`

## Estimated diff size

| Metric | Estimate |
|---|---:|
| Files changed | 9 |
| Added lines | 2,018 |
| Deleted lines | 107 |
| Total changed LOC | 2,125 |

This exceeds the 400-line target because standard-library IMAP does not parse BODYSTRUCTURE: the bounded parser, selective fetch consumer, MIME compatibility behavior, exact export and Connect caller correction, adversarial unit probes, and real-server proof form one release-blocking privacy boundary. Splitting parser admission, retrieval, compatibility, or caller semantics would leave an intermediate PR that either still downloads attachments during analysis or breaks explicit export and Connect for IMAP.
