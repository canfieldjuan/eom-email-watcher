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

The operation inspects the configured path before any normal `load_config`
call. It returns exactly one of these secret-free results:

```json
{"state":"missing"}
```

```json
{"state":"normal_admission"}
```

```json
{
  "state":"acknowledgement_required",
  "expected_revision":"sha256:LOWERCASE_HEX"
}
```

```json
{"state":"manual_repair_required"}
```

`expected_revision` is `sha256:` plus the SHA-256 digest of the exact config
bytes read for this decision. It is returned only for
`acknowledgement_required`. The result never contains the topic, URL, path,
file contents, parsed values, validation error text, or any other configuration
value. Logs and stderr must not contain them either.

On Unix, an existing file is eligible for either `normal_admission` or
`acknowledgement_required` only when all of these no-follow checks succeed:

- every access to the final pathname uses `lstat`/`openat` with no-follow
  semantics, and the opened descriptor's `fstat` identity matches the inspected
  entry;
- directory components are walked from a trusted root with no-follow directory
  opens, so a symlink in an ancestor cannot redirect the final parent lookup;
- the config is a regular file, not a symlink or other special file, has exactly
  one hard link, is owned by the effective user, and has exact mode `0600`;
- the immediate parent is opened as a no-follow directory, is owned by the
  effective user, and has exact mode `0700`; and
- candidate creation, replacement, and directory sync remain relative to that
  held directory descriptor, so a pathname swap cannot redirect the write.

A symlink, hardlink, special file, mismatched owner, broader permission mode,
unsafe immediate parent, or identity change yields `manual_repair_required`
and no config read through the unsafe entry. This slice does not silently
`chmod`, `chown`, unlink, or replace an unsafe legacy path. On platforms where
an equivalent native owner/DACL and reparse-point policy is not implemented,
an otherwise normal valid config may continue to `normal_admission`, but the
legacy acknowledgement shape is `manual_repair_required`; the mutation is not
offered. Windows migration proof remains deferred.

After the path checks, the state is `acknowledgement_required` only when all of
these are true:

1. the config is a regular, readable, valid TOML document;
2. `ntfy_topic` is a string whose trimmed value matches the existing
   20-to-64-character topic grammar;
3. `ntfy_content_disclosure_acknowledged` is absent or is literal boolean
   `false`; and
4. changing only that member to literal `true` produces a document accepted by
   the complete normal config validator.

`missing` means the no-follow path does not exist. `normal_admission` means the
safe file does not require this migration and the unchanged document passes the
complete normal validator; the native gate must still re-run normal admission
before starting workers. A non-string or malformed topic, non-boolean
acknowledgement, malformed TOML, unrelated config error, or unsafe path yields
`manual_repair_required`. None of these reads grant consent or mutate the
config.

Candidate validation must call the same parser/validator used by
`load_config`; do not copy a partial list of config checks into the migration.
The shared seam may accept candidate bytes plus the logical final config path
so status inspection does not need to put a candidate file on disk. Topic
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
unchanged. The desktop reconciles after every completion, conflict, transport
failure, or indeterminate result. It must never blindly retry, and a remaining
repairable revision requires another affirmative operator action.

The operation changes exactly one logical member:

```toml
ntfy_content_disclosure_acknowledged = true
```

The edit has a lexical preservation contract, not only a logical round-trip
contract:

- If the root-level member is literal `false`, a TOML-aware scanner identifies
  that exact boolean token and replaces only those five bytes with the four
  bytes `true`. Every byte before and after that token is identical.
- If the root-level member is absent, insert exactly one root-level assignment
  span, `ntfy_content_disclosure_acknowledged = true` plus the document's
  existing line ending (or `\n` when none exists), at byte offset zero. Every
  original byte remains identical and in the same order after that insertion.
- A regex-only search is insufficient. The locator must distinguish a
  root-level key from comments, quoted strings, dotted/nested keys, and table
  members, and it must reject duplicates or any ambiguous source span.

The candidate produced by that byte surgery must still parse as TOML, must have
the expected root-level value, and must pass the shared complete normal config
validator before any write. Tests compare the original/candidate prefix and
suffix exactly; preserving only parsed values or comments is insufficient.

Neither operation may return a normal settings object because that object is
not needed to obtain consent and creates an unnecessary path for configuration
data. Engine API failures use fixed messages that name the failure class, never
the rejected value or file content. The existing request allowlist and
one-shot response envelope remain authoritative
(`src/eom_email_watcher/engine_api.py:192-207,5180-5223`).

## Write and concurrency contract

The acknowledgement is one config-wide transaction:

1. Resolve the config path exactly as the existing engine does, then hold the
   safe parent directory descriptor and open the config without following
   links as specified above.
2. Acquire the existing cross-process config lock at `<config_path>.lock`.
   Every config writer must continue to use that same lock namespace; existing
   sender and settings mutations already do so
   (`src/eom_email_watcher/config.py:497-515,519-538,596-611`).
3. Under the lock, read the exact original bytes, compare the revision, recheck
   path identity and the repairable shape, perform the one allowed byte edit,
   and run the full hypothetical document through the shared normal validator.
4. Create the candidate in the same directory with owner-only mode `0600`.
   Write and flush all bytes, `fsync` the candidate, and verify immediately
   before commit that the destination is still the same safe inode with the
   expected original bytes.
5. Atomically replace the destination, `fsync` the containing directory, and
   re-run public `load_config` against the final path. Return success only after
   all three steps succeed.
6. Remove unused owner-only temporary files on every normal exit. Never create
   a second long-lived backup containing configuration secrets.

The current `_atomic_write` already uses a same-directory temporary, flushes,
`fsync`s the file, and atomically replaces the destination, but it does not sync
the directory, bind operations to a held no-follow directory descriptor, or
classify a post-replace result as indeterminate
(`src/eom_email_watcher/config.py:402-420`). The implementation should extend a
shared durability helper rather than add a second ad hoc write sequence.

The durability invariant is **exact old bytes or fully prevalidated new bytes**.
A failure before atomic replace leaves the exact old config at the path. Once
replace may have succeeded, a directory-sync failure, final-read failure,
sidecar termination, timeout, or response loss is an indeterminate outcome; do
not claim rollback and do not overwrite the possible valid new config with an
automatic restoration. The operation returns the fixed secret-free
`outcome_unknown` class when it can still respond. Atomic replacement and
prevalidation ensure that reconciliation sees either the exact old document or
the complete validated new document, never a partial candidate.

Two concurrent calls for one revision have at most one replacement. A caller
that acquires the lock after another success observes a stale revision or a
no-longer-repairable document, performs no write, and returns conflict. Repeating
the old request is non-mutating. Product-level idempotency comes from mandatory
reconciliation: acknowledged and loadable means proceed, while an old or newly
edited repairable document presents its current revision and requires a new
click. The status revision is CAS evidence, not consent and not a retry token.

After **every** acknowledgement attempt, including a success response, conflict,
`outcome_unknown`, timeout, broken pipe, or lost response, the native admission
coordinator must:

1. refresh the secret-free disclosure status without reusing the prior result;
2. when it returns `normal_admission`, run normal config admission and proceed
   only if that succeeds;
3. when it returns `acknowledgement_required`, display its current revision and
   require another explicit click before any new mutation; and
4. for `manual_repair_required`, a failed normal admission, or a status failure,
   hold all workers and display a fixed secret-free repair error.

There is no automatic retry in this sequence.

## Native startup admission and packaged desktop contract

The gate is owned by native Tauri startup, not by frontend timing. Today Tauri
calls `settings_with_timeout`, starts the Connect queue, spawns startup
notification delivery, and starts `PollScheduler` during `.setup()` before the
frontend's existence-only check runs (`desktop/src-tauri/src/lib.rs:787-839`).
Moving only `initializeDesktop` would therefore leave config-dependent work
running behind the disclosure screen.

Add one native admission coordinator with these states:

- `Inspecting`;
- `Missing`;
- `AwaitingAcknowledgement { expected_revision }`;
- `ManualRepairRequired`; and
- `Admitted`, with config-dependent workers started exactly once.

The bridge and startup sequence are:

1. Tauri setup constructs the engine, passive delivery/export objects, and the
   admission coordinator. It does **not** call `settings.get`, start or pump the
   Connect queue, deliver pending notifications, construct/start
   `PollScheduler`, or invoke any operation that reaches normal `load_config`.
2. Native code calls only `config.ntfy_disclosure.status`. `missing`,
   `acknowledgement_required`, and `manual_repair_required` transition to the
   matching held state. If a background/autostart launch reaches either held
   repair state, show the main window so the operator can see the required
   action or fixed repair message.
3. Only `normal_admission` permits the coordinator to call normal
   `settings_with_timeout`. If it fails, transition to
   `ManualRepairRequired`, retain no returned config data, and keep every worker
   held. If it succeeds, create/start `ConnectQueueScheduler`, run the one
   startup notification-delivery pass, construct/start `PollScheduler` from the
   admitted settings, and transition atomically to `Admitted`.
4. Expose one secret-free Tauri `config_admission_status` command that refreshes
   through this coordinator and returns only `missing`,
   `acknowledgement_required` plus its revision, `manual_repair_required`, or
   `admitted`. The old filesystem-existence-only `config_status` is no longer a
   startup authority (`desktop/src-tauri/src/engine.rs:1314-1322`;
   `desktop/src-tauri/src/lib.rs:249-259`).
5. `config_initialize` is allowed only from `Missing`. After it creates the
   config, the coordinator restarts at secret-free inspection, performs normal
   admission, and starts workers only on success. The acknowledgement command
   is allowed only from `AwaitingAcknowledgement` and always enters the
   reconciliation sequence above, even when its engine request errors.
6. Every config-dependent Tauri command, including `settings_get`, mailbox,
   Inbox, watcher-check, notification, Connect, and watchlist operations,
   checks the native gate and returns fixed `configuration_not_admitted` unless
   the state is `Admitted`. Frontend visibility is not the authority.
7. Worker start is serialized with admission and idempotent. Concurrent status
   refreshes, initialization, or acknowledgement completion cannot create a
   second queue pump, delivery pass, or poll scheduler. Closing/hiding the
   window does not release the gate or start work.

This ordering is a tested effect trace: the controlling factor is native worker
construction in `.setup()`, so admission must move ahead of the current calls,
not merely add a frontend panel.

`initializeDesktop` first invokes `config_admission_status`. `Missing` shows the
existing first-run form; `ManualRepairRequired` shows a fixed repair error;
`Admitted` enters the existing configured flow. It must not invoke
`startConfiguredDesktop`, `settings_get`, a mailbox operation, or a watcher
check while acknowledgement is pending.

`AwaitingAcknowledgement` shows a dedicated settings-panel disclosure containing
exactly this copy:

> **Phone notification privacy**
>
> Email Watcher sends the configured ntfy service the notification topic; the
> watched sender's configured label, or the message-supplied display name or
> email address; the email subject; and either the local-model summary with any
> suggested action and deadline, fixed fallback text, or scheduling review text
> that may contain an email-derived summary. Email Watcher does not redact or
> encrypt these fields at the application layer. HTTPS protects them while they
> travel to the service, but the configured ntfy service can read and may retain
> or log them. A long random topic limits who can subscribe or publish; it does
> not hide the content from that service. For confidentiality-sensitive mail,
> close Email Watcher and remove the topic from the private configuration before
> continuing.

The sole affirmative control has this exact label:

> **I understand and allow this email-derived content to be sent to the configured ntfy service**

Rendering or focusing the panel is not consent. Only activation of that control
sends `config.ntfy_disclosure.acknowledge` with the displayed status revision.
The control is disabled while one request is in flight. Every completion and
error uses the native reconciliation sequence; the frontend never assumes that
an error means the write did not commit, never assumes that success means
admission passed, and never retries with the old revision. It enters the normal
desktop only after the coordinator reports `Admitted`.

Rendering the panel, closing the window, declining by inaction, or navigating
away where the host allows it sends no acknowledgement request and therefore
mutates nothing. Once an acknowledgement request is attempted, an error or
unknown response does not prove that the file stayed old: the native
coordinator must reconcile, and the exact old document or fully validated new
document determines the resulting state. It never retries blindly or treats a
prior click as consent for a currently repairable revision. The panel never
displays, copies, or logs the topic or any other config value. It is a narrow
upgrade repair, not an ntfy settings editor. The new panel belongs beside the
existing first-run and normal settings surfaces
(`desktop/src/main.ts:587-636`) but is mutually exclusive with both.

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

1. **Native startup order:** an instrumented setup proves the first engine call
   is disclosure status. While it reports required, missing, manual repair, or
   errors, there are zero calls to `settings.get`, Connect queue start/pump,
   notification delivery, `PollScheduler`, mailbox, Inbox, watcher check, or
   any normal `load_config` path. A valid no-migration config proves normal
   admission precedes each worker and workers start once.
2. **Native command gate:** every config-dependent Tauri command fails with
   `configuration_not_admitted` in each held state, even if invoked directly
   without the frontend. Initialization and acknowledgement re-enter
   secret-free inspection and normal admission before releasing the gate.
3. **Config classification:** a safe valid topic plus missing/false
   acknowledgement is classified as required. Missing, normal valid, unsafe,
   malformed, and unrelated-invalid inputs reach only their documented
   secret-free states and preserve exact bytes.
4. **Filesystem boundary:** no-follow probes reject a file symlink, parent
   symlink, hardlink count greater than one, FIFO/device, wrong effective owner,
   file mode other than `0600`, parent mode other than `0700`, and inode swaps
   between inspect/open/precommit. No rejected case is read through or mutated.
5. **Full-candidate validation:** an otherwise-invalid config with the legacy
   ntfy shape is manual-repair-only. This proves the migration cannot stamp
   consent onto a still-unloadable document.
6. **Lexical preservation:** for literal `false`, the candidate equals exact
   original prefix + `true` + exact suffix. For an absent member, it equals one
   documented insertion span + every original byte. Comments, quoted decoys,
   dotted/nested keys, CRLF, no-final-newline, tables, arrays of tables, unknown
   keys, and Unicode are covered. Duplicate or ambiguous spans fail closed.
7. **CAS and locking:** stale revision, a manual edit ignoring the lock, and two
   simultaneous actions produce no lost update and at most one replacement.
   Repeating the stale request is non-mutating.
8. **Durability injection:** candidate create/write/`fsync`, validation,
   precommit identity/CAS, replace, directory `fsync`, final-load, process exit,
   and response-loss points prove the old-or-fully-validated-new invariant.
   Pre-replace failures preserve exact old bytes; post-replace uncertainty is
   reported as `outcome_unknown`, never as successful rollback.
9. **Indeterminate reconciliation:** success, conflict, `outcome_unknown`,
   timeout, broken pipe, and response loss all refresh status and normal
   admission. A loadable acknowledged result proceeds, a remaining repairable
   revision needs another click, and every other result holds workers with a
   fixed error. No path automatically retries.
10. **Engine protocol:** both operations reject unknown payload fields and
   malformed revisions; status and every error response are secret-free. The
   tests seed canary topic/config values and assert those canaries appear in no
   response or captured log/stderr.
11. **Rust bridge:** the engine deserializes only the documented secret-free
   states, passes the expected revision unchanged, maps stable errors, owns the
   serialized admission/worker state, and exposes no config values.
12. **Desktop flow:** no invocation is sent on render, close, focus, inaction,
   or navigation; one explicit click sends one revision; an attempted request's
   success, error, or unknown outcome always reconciles through the native
   coordinator; and a current required revision needs another click.
13. **Regression:** normal `load_config` still rejects a topic with missing,
   false, or non-boolean acknowledgement. The existing tests at
   `tests/test_config.py:636-687` remain unchanged in meaning.

The declared fail-first failure is the legacy valid-topic/missing-ack case:
today it raises `ConfigError` before either runtime can offer a repair, and the
new status/acknowledgement protocol and UI do not exist. A different failure or
an unexpected pass stops implementation for diagnosis.

## Boundary matrix

| Current config shape | Status | Affirmative action |
|---|---|---|
| Valid string topic; acknowledgement absent | `acknowledgement_required` + revision | add literal `true` |
| Valid string topic; acknowledgement `false` | `acknowledgement_required` + revision | replace only with literal `true` |
| Valid string topic; acknowledgement `true`; full config valid | `normal_admission` | unavailable; no mutation |
| Topic absent; full config valid | `normal_admission` | unavailable; no mutation |
| Config path missing | `missing` | unavailable; initialization only |
| Empty, too short, too long, invalid-character, or non-string topic | `manual_repair_required` | unavailable; no mutation |
| Acknowledgement string, integer, null, array, or table | `manual_repair_required` | unavailable; no mutation |
| Malformed or duplicate-root-key TOML | `manual_repair_required` | unavailable; no mutation |
| Legacy ntfy shape plus unrelated invalid setting | `manual_repair_required` | unavailable; no mutation |
| Config/ancestor/parent symlink, hardlink, special file, wrong owner, or broad mode | `manual_repair_required` | unavailable; no read-through or mutation |
| Quoted/comment/nested acknowledgement decoy; root member absent | `acknowledgement_required` if otherwise valid | insert root member; decoy bytes unchanged |
| Revision changed after status | previously required | conflict; exact current bytes preserved |
| Candidate validation or any pre-replace write/sync step fails | required | error; exact old bytes remain |
| Replace may have occurred, then sync/load/response fails | reconcile | old or fully validated new; no blind retry |
| Two actions race with one revision | required | at most one success; loser conflicts |

Boundary tests use only placeholder values such as `YOUR_NTFY_TOPIC`; they do
not read the host's private config.

## Linux installed proof

Before merge, prove the vertical slice with the exact Debian artifact built
from one clean candidate commit, and with the separately installed systemd CLI
snapshot produced from that same checkout by
`scripts/install-user-services.sh`. That installer exports locked production
dependencies, force-installs the repository as a `uv tool`, verifies
`~/.local/bin/eom-mail-watch`, then copies/enables the user units
(`scripts/install-user-services.sh:24-39,83-100`). The service itself executes
that stable path (`systemd/eom-email-watcher.service:8-18`); it does not execute
the Debian sidecar.

The proof runs under a disposable Linux user with an isolated owner-private
home/config. It must not inspect or change the operator's real config, tokens,
entitlement, units, service snapshot, or timer.

1. Check out the full candidate revision in a clean detached worktree. Record
   that full revision before either build/install. Build the Debian artifact
   there, record its SHA-256 and package metadata, install it, and record the
   installed paths and SHA-256 digests of both
   `eom-email-watcher-desktop` and packaged `eom-mail-engine`. The package is
   expected to contain both binaries (`desktop/README.md:115-128`).
2. From the same unchanged candidate worktree, run
   `scripts/install-user-services.sh` as the disposable user. Record the full
   source revision, the resolved `~/.local/bin/eom-mail-watch` path and SHA-256,
   the snapshot interpreter path, installed distribution location, and a
   deterministic digest of the installed `eom_email_watcher` package tree.
   Byte-compare the migration-owning installed modules with the same files at
   the recorded candidate revision. This pins the otherwise separate service
   snapshot to the audited source; a checkout hash beside an unverified tool
   install is insufficient. README documents that this installer snapshots the
   current source and locked dependencies and that changing a checkout later
   does not update it (`README.md:298-326`).
3. Use `systemctl --user show` in the disposable account to record the loaded
   service `ExecStart` and prove it resolves to that exact recorded
   `eom-mail-watch` binary. Hash it again immediately before and after the
   service runs. The proof fails if a source-checkout CLI, Debian sidecar, or a
   different tool snapshot services the unit.
4. Point the packaged desktop and disposable user-systemd service at the same
   synthetic config. Its parent is owned by the disposable user at `0700`; the
   file is singly linked, owned by that user, and `0600`. Include a valid
   placeholder topic, omit the acknowledgement, include zero watched senders,
   and add lexical canaries, comments, CRLF/formatting cases, and unrelated
   fields. No real mailbox, model, or ntfy request is allowed.
5. Observe the precondition: the recorded systemd CLI snapshot fails at the
   existing disclosure guard, while the installed native startup gate holds
   settings, Connect queue/pump, notification delivery, and polling at zero and
   opens the exact disclosure panel. Confirm no config byte changed.
6. Close once without accepting and relaunch. Confirm the file is unchanged,
   the workers remain held, and the panel returns.
7. Activate the exact affirmative control once. Confirm the final config is
   mode `0600`, normal `load_config` succeeds, the lexical diff is exactly the
   one allowed token replacement or insertion span, and neither process output
   nor captured journal contains the canary topic or other config values.
8. Restart the unchanged installed application. Confirm it bypasses the
   migration panel, completes normal native admission, and starts each worker
   once. Run the disposable service again and confirm the exact recorded CLI
   snapshot passes config admission and exits through the zero-sender/inactive
   path without sending a notification. Confirm the timer ownership and
   enablement state were not changed by the UI.
9. Inject or simulate response loss after replacement. Confirm relaunch/reconcile
   admits a loadable acknowledged file without a second mutation. Repeat with a
   stale revision, unsafe file mode, file symlink, hardlink, and non-boolean
   acknowledgement; each remains non-mutating and never reaches the consent
   action.

The evidence record therefore contains one candidate revision, Debian package
digest and metadata, installed desktop and sidecar paths/digests, installed CLI
path/hash, snapshot package-tree digest, snapshot/source byte comparisons, and
the loaded systemd `ExecStart`. Any revision or digest mismatch fails proof.

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
- following, replacing, or automatically repairing an unsafe config path,
  ownership, hardlink, parent directory, permission mode, or Windows DACL;
- changing systemd ownership, cadence, installation, or desktop polling;
- changing mailbox, model, Connect, Automate, entitlement, or account behavior;
- Windows acknowledgement mutation or installed proof in this slice; and
- generalizing the UI into a configuration-file editor.
