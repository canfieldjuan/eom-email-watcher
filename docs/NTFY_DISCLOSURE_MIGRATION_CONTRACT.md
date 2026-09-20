# ntfy disclosure upgrade migration contract

Status: **PROPOSED — contract only; implementation is not authorized by this
document.**

Baseline inspected on 2026-09-19:

- Email Watcher `origin/main`:
  `c032ebff7384907169043a9e211be16011c48403`

The code at that revision is the authority for this contract. The private
configuration, its ntfy topic, OAuth material, and entitlement were not read.

## Root cause

An installed upgrade can inherit a configuration that predates the explicit
ntfy content-disclosure acknowledgement. If that configuration has a valid
`ntfy_topic` and either omits
`ntfy_content_disclosure_acknowledged` or sets it to literal `false`, both
runtime owners become unusable:

- `load_config` reads and validates the whole TOML document, defaults the
  acknowledgement to `false`, and rejects a configured topic unless the value
  is literal `true` (`src/eom_email_watcher/config.py:242-251,334-351`).
- The systemd entry point calls `load_config` before it can take the production
  check lock or run a check (`src/eom_email_watcher/cli.py:182-200`), and the
  installed unit invokes that entry point (`systemd/eom-email-watcher.service:8-18`).
- The packaged desktop checks only whether the config path exists
  (`desktop/src-tauri/src/engine.rs:1314-1322`; `desktop/src-tauri/src/lib.rs:249-259`).
  Presence immediately selects the configured-app path
  (`desktop/src/main.ts:2961-2969`), whose settings request then calls normal
  `load_config` (`desktop/src/main.ts:3079-3094`;
  `src/eom_email_watcher/engine_api.py:4965-4967`).
- Normal desktop settings cannot repair this state. `update_settings` calls
  `load_config` before it parses or writes the document
  (`src/eom_email_watcher/config.py:596-611`), and the acknowledgement is not in
  the desktop settings allowlist (`src/eom_email_watcher/config.py:25-34`).

This is an upgrade migration deadlock. It is not permission to weaken the
disclosure guard. The current disclosure is intentional: ntfy receives
email-derived content, HTTPS protects transit only, and the configured service
can read and retain that content (`README.md:65-93`).

## Decision

Add one secret-free, pre-`load_config` inspection operation and one explicit
acknowledgement operation to the packaged engine/desktop boundary. The first
operation only identifies the single repairable legacy shape. The second
changes only `ntfy_content_disclosure_acknowledged` to literal `true`, after the
operator has read the disclosure and deliberately selected the affirmative
action.

No startup path, settings load, status read, retry, package installation, or
systemd invocation may acknowledge on the operator's behalf.

## Engine contract

### `config.ntfy_disclosure.status`

Request payload: an empty object.

The operation reads the configured file before normal `load_config`. It returns
one of these secret-free results:

```json
{"state":"not_applicable"}
```

```json
{
  "state":"acknowledgement_required",
  "expected_revision":"sha256:LOWERCASE_HEX"
}
```

`expected_revision` is `sha256:` plus the SHA-256 digest of the exact config
bytes read for this decision. It is returned only for
`acknowledgement_required`. The result never contains the topic, URL, file
contents, parsed values, validation error text, or any other configuration
value. Logs and stderr must not contain them either.

The state is `acknowledgement_required` only when all of these are true:

1. the config is a regular, readable, valid TOML document;
2. `ntfy_topic` is a string whose trimmed value matches the existing
   20-to-64-character topic grammar;
3. `ntfy_content_disclosure_acknowledged` is absent or is literal boolean
   `false`; and
4. changing only that member to literal `true` produces a document accepted by
   the complete normal config validator.

The status is `not_applicable` for a missing config, no topic, an already-true
acknowledgement, a non-string or malformed topic, a non-boolean acknowledgement,
malformed TOML, or any unrelated config error. That result grants no consent and
mutates nothing. Normal application handling may report an existing
configuration error later, through its existing path.

Candidate validation must call the same parser/validator used by
`load_config`; do not copy a partial list of config checks into the migration.
The shared seam may accept candidate text plus the logical final config path so
status inspection does not need to put a candidate file on disk. Topic
classification must use the raw TOML type before any string coercion.

### `config.ntfy_disclosure.acknowledge`

Request payload:

```json
{"expected_revision":"sha256:LOWERCASE_HEX"}
```

No other member is accepted. The operation returns only:

```json
{"acknowledged":true}
```

It succeeds only if the current exact bytes still hash to `expected_revision`
and the current document still has the repairable shape defined above. A stale
revision, already-acknowledged document, removed/changed topic, or other
eligibility change returns a stable conflict and leaves the file byte-for-byte
unchanged. The desktop must refresh status after a conflict; it must not retry
with a newly observed revision without another affirmative operator action.

The operation changes exactly one logical member:

```toml
ntfy_content_disclosure_acknowledged = true
```

If the member was `false`, replace that value in place. If it was absent, add
the member without rewriting or dropping comments, ordering, unknown keys,
tables, array-of-table layout, whitespace-bearing strings, or unrelated values.
Use a round-trip TOML document for the edit, and use the shared complete normal
validator on the resulting serialization before any commit.

Neither operation may return a normal settings object because that object is
not needed to obtain consent and creates an unnecessary path for configuration
data. Engine API failures use fixed messages that name the failure class, never
the rejected value or file content. The existing request allowlist and
one-shot response envelope remain authoritative
(`src/eom_email_watcher/engine_api.py:192-207,5180-5223`).

## Write and concurrency contract

The acknowledgement is one config-wide transaction:

1. Resolve the config path exactly as the existing engine does.
2. Acquire the existing cross-process config lock at `<config_path>.lock`.
   Every config writer must continue to use that same lock namespace; existing
   sender and settings mutations already do so
   (`src/eom_email_watcher/config.py:497-515,519-538,596-611`).
3. Under the lock, read the exact original bytes, compare the revision, recheck
   the repairable shape, edit only the acknowledgement in a round-trip document,
   and run the full hypothetical document through the shared normal validator.
4. Create the candidate in the same directory with owner-only mode `0600`.
   Write and flush all bytes, `fsync` the candidate, and verify immediately
   before commit that the destination still has the expected original bytes.
5. Atomically replace the destination, `fsync` the containing directory, and
   re-run public `load_config` against the final path. Return success only after
   all three steps succeed.
6. Preserve the exact original bytes on every reported failure. A failure after
   replacement must atomically restore the original under the same lock and
   sync the directory before returning. Crash outcomes may be either the exact
   original or the fully validated acknowledged document, never a partial file.
   Temporary/rollback artifacts are owner-only and are removed on every normal
   exit.

The current `_atomic_write` already uses a same-directory temporary, flushes,
`fsync`s the file, and atomically replaces the destination, but it does not sync
the directory or provide post-replace rollback
(`src/eom_email_watcher/config.py:402-420`). The implementation should extend a
shared durability helper rather than add a second ad hoc write sequence.

Two concurrent acknowledgement calls for the same revision have at most one
success. A manual edit or any existing config mutation between status and action
causes the stale action to fail closed. The status operation is read-only; its
revision is evidence for CAS, not consent.

## Packaged desktop contract

`initializeDesktop` must invoke the new disclosure status before the existing
existence-only `config_status` flow. It must not call `startConfiguredDesktop`,
`settings_get`, a mailbox operation, or a watcher check while the result is
`acknowledgement_required`.

Instead, show a dedicated settings-panel disclosure containing exactly this
copy:

> **Phone notification privacy**
>
> Email Watcher sends the configured ntfy service the notification topic; the
> watched sender's configured label, or the message-supplied display name or
> email address; the email subject; and either the local-model summary with any
> suggested action and deadline, fixed fallback text, or scheduling review text
> that may contain an email-derived summary. Email Watcher does not redact or
> encrypt these fields at the application layer. HTTPS protects them while they
> travel to the service, but the configured ntfy service can read and may retain
> or log them. A long random topic controls subscription and publishing; it does
> not hide the content from that service. For confidentiality-sensitive mail,
> close Email Watcher and remove the topic from the private configuration before
> continuing.

The sole affirmative control has this exact label:

> **I understand and allow this email-derived content to be sent to the configured ntfy service**

Rendering or focusing the panel is not consent. Only activation of that control
sends `config.ntfy_disclosure.acknowledge` with the displayed status revision.
The control is disabled while one request is in flight. A successful response
causes a fresh status read and only then enters the existing configured desktop
flow. Error and stale-revision responses keep the disclosure visible and do not
optimistically update the UI.

Closing the window, declining by inaction, navigating away where the host
allows it, or a failed action mutates nothing. The panel never displays, copies,
or logs the topic or any other config value. It is a narrow upgrade repair, not
an ntfy settings editor. The new panel belongs beside the existing first-run and
normal settings surfaces (`desktop/src/main.ts:587-636`) but is mutually
exclusive with both.

## Scheduler ownership

This migration does not start, stop, enable, disable, install, or edit a systemd
unit. The Linux human CLI remains the systemd entry point, while the packaged
desktop remains a separate UI/sidecar and does not replace the systemd scheduler
or its ntfy delivery (`README.md:208-225`). The existing timer cadence and unit
ownership remain unchanged (`systemd/eom-email-watcher.timer:1-12`).

There is deliberately no **clear topic and continue** action in this slice.
With a topic present, desktop automatic polling is disabled by the existing
settings projection (`src/eom_email_watcher/engine_api.py:4937-4944`). Clearing
the topic from the packaged UI could therefore enable desktop polling while an
already-enabled systemd timer still owns scheduled checks. An operator who
declines disclosure must close the app and edit the private config through the
existing operator-managed path.

## Fail-first tests

The implementation starts with focused tests that fail for the current root
behavior and then pass without weakening `load_config`:

1. **Config classification:** a valid topic plus missing/false acknowledgement
   is classified as required without calling normal startup; all other boundary
   rows below are not applicable and preserve exact bytes.
2. **Full-candidate validation:** an otherwise-invalid config with the legacy
   ntfy shape is not offered the consent repair. This proves the migration
   cannot stamp consent onto a still-unloadable document.
3. **CAS and locking:** stale revision and two simultaneous actions produce no
   lost update and at most one success.
4. **Round-trip preservation:** comments, unknown tables, sender array shape,
   and unrelated values survive both absent-key insertion and `false`-to-`true`
   replacement. Only the acknowledgement's logical value changes.
5. **Durability injection:** candidate write, candidate `fsync`, validation,
   precommit CAS, replace, directory `fsync`, and final-load failures each leave
   or restore the exact original bytes; success is a loadable owner-private
   config.
6. **Engine protocol:** both operations reject unknown payload fields and
   malformed revisions; status and every error response are secret-free. The
   tests seed canary topic/config values and assert those canaries appear in no
   response or captured log/stderr.
7. **Rust bridge:** the engine deserializes only the two documented result
   shapes, passes the expected revision unchanged, maps stable errors, and
   exposes a Tauri acknowledgement command without exposing configuration
   values.
8. **Desktop flow:** acknowledgement-required status prevents the old
   existence-only path and every settings/mailbox/check call; no invocation is
   sent on render, close, or error; one explicit click sends one revision; a
   success rechecks status before normal startup; a conflict requires another
   click after refresh.
9. **Regression:** normal `load_config` still rejects a topic with missing,
   false, or non-boolean acknowledgement. The existing tests at
   `tests/test_config.py:636-687` remain unchanged in meaning.

The declared fail-first failure is the legacy valid-topic/missing-ack case:
today it raises `ConfigError` before either runtime can offer a repair, and the
new status/acknowledgement protocol and UI do not exist. A different failure or
an unexpected pass stops implementation for diagnosis.

## Boundary matrix

| Current config shape | Status | Affirmative action |
|---|---|---|
| Valid string topic; acknowledgement absent | required + revision | add literal `true` |
| Valid string topic; acknowledgement `false` | required + revision | replace only with literal `true` |
| Valid string topic; acknowledgement `true` | not applicable | unavailable; no mutation |
| Topic absent; acknowledgement absent/false/true | not applicable | unavailable; no mutation |
| Empty, too short, too long, or invalid-character topic | not applicable | unavailable; no mutation |
| Non-string topic | not applicable | unavailable; no mutation |
| Acknowledgement string, integer, null, array, or table | not applicable | unavailable; no mutation |
| Malformed or duplicate-key TOML | not applicable | unavailable; no mutation |
| Legacy ntfy shape plus unrelated invalid setting | not applicable | unavailable; no mutation |
| Revision changed after status | previously required | conflict; exact current bytes preserved |
| Candidate validation or any write/sync/final-load step fails | required | error; exact original restored |
| Two actions race with one revision | required | at most one success; loser conflicts |

Boundary tests use only placeholder values such as `YOUR_NTFY_TOPIC`; they do
not read the host's private config.

## Linux installed proof

Before merge, prove the vertical slice with the exact Debian artifact built
from the candidate commit. Record the commit, package SHA-256, installed package
version, and binary paths. The proof runs under a disposable Linux user or
isolated owner-private home/config; it must not inspect or change the operator's
real config, tokens, entitlement, units, or timer.

1. Install the exact Debian artifact and use its packaged
   `eom-email-watcher-desktop` plus packaged `eom-mail-engine`; the package is
   expected to contain both binaries (`desktop/README.md:115-128`).
2. Point the packaged desktop and a disposable user-systemd watcher service at
   the same synthetic config. Include a valid placeholder topic, omit the
   acknowledgement, include zero watched senders, and add comments and unrelated
   fields that can be compared afterward. No real mailbox, model, or ntfy
   request is allowed.
3. Observe the precondition: the systemd check fails at the existing disclosure
   guard, while the packaged UI opens the exact disclosure panel instead of
   entering the broken configured-app flow. Confirm no config byte changed.
4. Close once without accepting and relaunch. Confirm the file is unchanged and
   the panel returns.
5. Activate the exact affirmative control once. Confirm the final config is
   mode `0600`, normal `load_config` succeeds, only the acknowledgement changed
   logically, comments/unrelated fields remain, and neither process output nor
   captured journal contains the canary topic or other config values.
6. Restart the unchanged installed application. Confirm it bypasses the
   migration panel and reaches normal settings. Run the disposable systemd
   service again and confirm it passes config admission and exits through the
   zero-sender/inactive path without sending a notification. Confirm the timer
   enablement state was not changed by the UI.
7. Repeat with a stale revision and with a non-boolean acknowledgement. Confirm
   both are non-mutating and the latter is never presented as consent-remediable.

An extracted sidecar test is useful deterministic evidence, but it does not
replace the installed UI click, restart, shared-config systemd recovery, file
mode, and journal checks above.

## Non-goals

- weakening or bypassing the existing literal-`true` startup guard;
- silently acknowledging during install, startup, status, retry, or settings
  save;
- displaying, returning, hashing into logs, or otherwise disclosing the ntfy
  topic or any other config value;
- adding, editing, clearing, generating, testing, or sending to an ntfy topic;
- changing notification payloads, ntfy transport, retention, encryption, or
  issue `#137`'s future opaque/wake-only design;
- repairing malformed TOML, malformed topics, non-boolean acknowledgement, or
  unrelated config errors;
- changing systemd ownership, cadence, installation, or desktop polling;
- changing mailbox, model, Connect, Automate, entitlement, or account behavior;
- Windows installed proof in this slice; and
- generalizing the UI into a configuration-file editor.
