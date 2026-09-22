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
- candidate creation, linking, replacement, and directory sync remain relative
  to that held directory descriptor; immediately before each mutation, the
  current parent pathname is reopened without following links and must still
  resolve to the held directory identity with the expected owner and mode.

A symlink, hardlink, special file, mismatched owner, broader permission mode,
unsafe immediate parent, or identity change yields `manual_repair_required`
and no config read through the unsafe entry. This slice does not silently
`chmod`, `chown`, unlink, or replace an unsafe legacy path. On platforms where
an equivalent native owner/DACL and reparse-point policy is not implemented,
an otherwise normal valid config may continue to `normal_admission`, but the
legacy acknowledgement shape is `manual_repair_required`; the mutation is not
offered. Windows migration proof remains deferred.

The legacy shape is also `manual_repair_required` when the running platform
does not expose Linux `O_TMPFILE`, `linkat(AT_EMPTY_PATH)`, or atomic rename
exchange. A filesystem that rejects any required primitive leaves the config
unchanged and records a secret-free, owner-private unsupported marker. Later
status calls remain `manual_repair_required`; they do not return to
`acknowledgement_required` or invite a retry loop.

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

On non-POSIX hosts, every remaining path-read failure, including a directory,
ACL denial, sharing violation, and I/O error, is also
`manual_repair_required`. Responses use only the fixed classification and never
include the rejected path or operating-system error.

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
2. Acquire the canonical owner-private cross-process config lock in Email
   Watcher's runtime state directory. systemd uses its validated absolute
   `STATE_DIRECTORY`; interactive processes use
   `$XDG_STATE_HOME/eom-email-watcher`, with the existing
   `~/.local/state/eom-email-watcher` fallback. Every config reader and writer
   uses that same lock namespace.
3. Under the lock, read the exact original bytes, compare the revision, recheck
   path identity and the repairable shape, perform the one allowed byte edit,
   and run the full hypothetical document through the shared normal validator.
4. Create the candidate as an unnamed inode on the exact config filesystem with
   Linux `O_TMPFILE`, owner-only mode `0600`, and link count zero. Revalidate the
   held parent and its current pathname before permission changes, every
   short-write-loop iteration, and file sync. A crash at any partial write
   closes the inode without ever creating a secret-bearing pathname.
5. After the unnamed candidate is fully written and synced, `fstat` it and
   create a fixed, owner-private, secret-free transaction marker. The marker
   records the exact original identity and digest plus the candidate device,
   inode, safe metadata, size, timestamp, and digest. It contains no config
   bytes. Sync the marker and its directory while the candidate remains
   unnamed.
6. Revalidate the held parent and current parent pathname, then link that exact
   open inode to the fixed candidate name with `linkat(AT_EMPTY_PATH)`. Linking
   never replaces an existing name. Verify the linked entry has the marker's
   exact device and inode plus the recorded safe metadata and digest, then sync
   its directory entry. A manual fixed-name collision has a different inode,
   survives, and forces manual repair.
7. Recheck the destination identity and exact original bytes, then revalidate
   the held parent and current pathname immediately before `RENAME_EXCHANGE`.
   The exchange makes the candidate the config and places the displaced entry
   at the fixed candidate name. Consent commits only when that displaced entry
   is the marker's exact original inode, safe metadata, and digest.
8. Before deleting a verified original, durably link a fully written unnamed,
   owner-private, secret-free `commit` disposition. Before rolling back any
   other displaced entry, durably link a `rollback` disposition with its exact
   device, inode, mode, link count, owner, size, timestamp, and digest when it
   can be safely read. It repeats the original and acknowledgement descriptors
   but contains no config bytes. A crash cannot expose a partial named record.
9. On a displaced mismatch, atomically exchange the entries back. Verify that
   the captured manual inode and bytes are live again and that the exact
   provenance-owned acknowledgement inode is isolated before removing the
   acknowledgement and transaction files. Exchange response loss is reconciled
   from both namespaces; no result with an unverified acknowledgement live is
   reported as consent.
10. Reconcile exact states before returning: pre-exchange marker states abort;
   a `commit` disposition accepts only the exact committed arrangement; a
   `rollback` disposition accepts only the exact pre-rollback or completed
   rollback arrangement. A manual target is preserved. A fixed candidate
   without valid provenance is never deleted by byte coincidence.
11. If `O_TMPFILE`, `linkat(AT_EMPTY_PATH)`, or exchange is unavailable on the
   exact filesystem, durably write a secret-free unsupported marker, close any
   unnamed inode, remove only an exactly proven linked candidate and
   transaction marker, leave the config unchanged, and make later status calls
   stable `manual_repair_required`.

On POSIX, ordinary settings and watchlist writes use this same exchange and
recovery protocol so the final destination rename cannot erase a concurrent
manual replacement. The marker, disposition, and linked candidate remain on
the exact config filesystem because their atomic exchange and directory
durability depend on that shared filesystem. Windows retains its native
held-source atomic replacement path.

Every normal runtime config load is also a transaction recovery boundary. The
public `load_config` path acquires the same config lock, safely opens the held
parent, reconciles any marker, disposition, candidate, or unsupported sentinel,
and only then parses bytes read from the recovered current file descriptor.
Callers that already hold the config lock use the same recovery and same-fd read
primitive without recursively acquiring the lock. The disclosure status and
acknowledgement operations continue to use their internal inspection primitives
so they can classify and repair transaction states without entering the public
loader recursively.

Therefore the systemd service path (`eom-mail-watch check`), human CLI, engine
operations, and runtime construction cannot consume an acknowledgement or topic
while transaction artifacts remain unresolved. A proven committed exchange is
cleaned and admitted. A displaced mismatch is rolled back before parsing, so a
manual concurrent replacement remains live. Ambiguous, tampered, unsafe, or
unsupported recovery returns one fixed configuration error with no path, topic,
operating-system detail, or parsed config values, and no runtime effect occurs.

The durability invariant is **exact old bytes or fully prevalidated new bytes**.
A failure before exchange leaves the exact old config at the path and removes
only files whose marker provenance and identities prove they belong to that
attempt. Once exchange may have succeeded, recovery classifies the exact marker,
disposition, target, and candidate before deleting or restoring anything. A
mismatched displaced entry never grants consent: recovery restores it live and
returns conflict. A directory-sync failure, final-read failure, sidecar
termination, timeout, or unprovable response loss after a verified commit
returns the fixed secret-free `outcome_unknown` class when the process can still
respond. Ambiguous or tampered states preserve every unproven file and surface
`manual_repair_required`; recovery never deletes a manual concurrent write.

The parent pathname is reopened from the filesystem root and compared with the
held descriptor immediately before each mutation. Linux does not provide a
single `linkat` or `renameat2` operation that also asserts that a directory fd
still has one particular pathname. A hostile same-user rename in the
instruction gap after that final comparison remains an unavoidable kernel
primitive limit; no-follow resolution, exact identity checks, and dir-fd
relative mutations minimize that gap and prevent redirection through a
replacement pathname observed before the syscall.

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
- `Admitted { admission_token }`, with config-dependent workers started exactly
  once.

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
3. Only `normal_admission` permits `config.admission.snapshot`. That operation
   recovers a pending disclosure transaction under the config lock, safely
   opens the config once, parses the bytes from that descriptor, and returns
   sanitized settings plus a non-secret `{version, revision, identity}` token
   derived from those same bytes and complete file identity. Unsafe, unreadable,
   invalid, or still-repairable config returns one generic configuration error.
4. Native code immediately calls `config.admission.compare` with that token. A
   same-byte inode replacement is stale because identity participates in the
   token. If snapshot or compare fails, transition to `ManualRepairRequired`,
   retain no settings, and keep workers held. If both succeed, start the queue,
   startup notification pass, and poll scheduler from the snapshot settings,
   then transition atomically to `Admitted { admission_token }`.
5. Expose one secret-free Tauri `config_admission_status` command that refreshes
   through this coordinator and returns only `missing`,
   `acknowledgement_required` plus its revision, `manual_repair_required`, or
   `admitted`. The old filesystem-existence-only `config_status` is no longer a
   startup authority (`desktop/src-tauri/src/engine.rs:1314-1322`;
   `desktop/src-tauri/src/lib.rs:249-259`).
6. `config_initialize` is allowed only from `Missing`. After it creates the
   config, the coordinator restarts at secret-free inspection, performs normal
   admission, and starts workers only on success. The acknowledgement command
   is allowed only from `AwaitingAcknowledgement` and always enters the
   reconciliation sequence above, even when its engine request errors.
7. Every config-dependent Tauri command, including `settings_get`, mailbox,
   Inbox, watcher-check, notification, Connect, and watchlist operations,
   checks the native gate and returns fixed `configuration_not_admitted` unless
   the state is `Admitted`. Frontend visibility is not the authority.
8. Every worker effect request supplies the admitted token at the top level.
   `host.operation_lock`, `watcher.check`, `connect.queue.pump`,
   `notifications.pending`, `notifications.pending_under_host_lock`,
   `notifications.count_under_host_lock`, and `notifications.ack` require it.
   Each compares it inside the same safe-open config load used to construct that
   request's runtime, before its operation lock or effect. Missing or malformed
   tokens are invalid requests, stale tokens are conflicts, and unsafe current
   config is a generic configuration error with zero effect.
9. Successful settings or watchlist mutation takes and compares a new snapshot,
   then replaces the shared worker token before returning. Other operations
   tolerate an omitted token. Status, acknowledgement, snapshot, and compare
   remain unbound so recovery can run while admission is held.
10. Worker start is serialized with admission and idempotent. Concurrent status
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

## Revisioned mailbox startup integration

When the admitted desktop is combined with revision-stamped mailbox startup,
normal navigation remains disabled until `loadMailAccounts()` seeds the active
mailbox revision. The startup attempt captures the admission generation that
authorized it. After the asynchronous seed returns, it may set
`configurationReady` and start Inbox, health, autostart, and sender effects only
if that exact admission generation is still current. A newer held generation
must keep the desktop fail closed; a newer admitted generation gets its own
seed attempt. This composes the disclosure admission gate with mailbox event
ordering without changing disclosure copy, acknowledgement semantics, or any
buyer-visible output.

The regression proof exercises a superseded startup: an older admitted
generation begins mailbox seeding, a newer admission generation arrives before
the seed resolves, and the older attempt cannot enable navigation or launch
configured effects. The existing successful and failed mailbox-seed cases
remain required.

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
   precommit identity/CAS, exchange, commit/rollback disposition durability,
   rollback exchange, each directory `fsync`, final-load, process exit, and
   response loss prove the old-or-fully-validated-new invariant. Atomic
   replacement immediately before exchange, death after exchange or during
   rollback, rollback response loss, repeated recovery, candidate/disposition
   tamper, and topic-removing manual replacement never grant consent or hide or
   delete the manual entry.
   A process killed immediately after an exchange with a concurrent manual
   topic removal is followed directly by the exact systemd/CLI check path,
   without a prior status call; recovery restores the exact manual bytes before
   config parsing, no old topic reaches notification delivery, and ambiguous or
   tampered recovery stops with the generic configuration error.
9. **Indeterminate reconciliation:** success, conflict, `outcome_unknown`,
   timeout, broken pipe, and response loss all refresh status and normal
   admission. A loadable acknowledged result proceeds, a remaining repairable
   revision needs another click, and every other result holds workers with a
   fixed error. No path automatically retries.
10. **Engine protocol:** disclosure and admission operations reject unknown
   payload fields and malformed revisions or tokens. Snapshot settings contain
   no topic, config path, or secret. All seven worker effects reject missing
   tokens and a stale token produces zero store, mailbox, notification, or queue
   effect. Non-POSIX directory, permission, sharing, and I/O errors are generic
   manual repair or configuration errors. Status and every error response are
   secret-free; canary topic/config values appear in no response or captured
   log/stderr.
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
| `O_TMPFILE`, `linkat(AT_EMPTY_PATH)`, or rename exchange unavailable; or persisted unsupported marker present | `manual_repair_required` | unavailable; no config mutation |
| Fixed candidate present without a valid marker for its exact device and inode | `manual_repair_required` | unavailable; candidate preserved |
| Quoted/comment/nested acknowledgement decoy; root member absent | `acknowledgement_required` if otherwise valid | insert root member; decoy bytes unchanged |
| Revision changed after status | previously required | conflict; exact current bytes preserved |
| Candidate validation or any pre-replace write/sync step fails | required | error; exact old bytes remain; partial unnamed inode has no pathname |
| Filesystem rejects unnamed creation, linking, or exchange as unsupported | `manual_repair_required` after attempt | config unchanged; exact linked candidate removed; no retry loop |
| Process exits during any candidate short write | reconcile | unnamed inode disappears; no named secret; exact old config remains |
| Process exits after marker durability but before link, or after link before exchange | reconcile | exact old config remains; only exact-inode artifacts are cleaned |
| Process exits immediately after exchange | reconcile | exact candidate retained; displaced original and marker cleaned only after exact proof |
| Manual target replacement after exchange | reconcile current manual target | manual target preserved; exact displaced original and marker securely removed |
| Parent pathname is renamed or replaced at a mutation boundary | conflict/error | held and current identities differ; no config mutation; exact owned artifacts cleaned |
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

## Exact-head Windows package failure investigation

Root cause presently proven: the Windows package job at head
`423756727a723620cb32d3db59788e062914d398` reaches the packaged
`config.initialize` request and exits 2, but the smoke probe discards the
structured response and child diagnostic. The underlying initialization
failure is not yet determined. This is an observability defect in the release
gate, not permission to bypass or weaken it.

Required change surface: `scripts/smoke_packaged_engine.py` may report only a
bounded, validated engine error code from a failed v1 response. Its adjacent
tests in `tests/test_desktop_packaging.py` must prove that a valid code is
visible and malformed output, paths, token values, and stderr remain hidden.
The following exact-head Windows job supplied `configuration_error`. That
diagnostic result narrows but does not identify the underlying failure.

### Current-head repair contract

The next package job reported `configuration_error` from packaged
`config.initialize`; it did not identify whether publication occurred. Do not
claim that a post-publication failure caused this job until native evidence
distinguishes the two cases. Preserve the failing smoke gate.

Problem-derived roots and required changes:

- The root TOML token scanner treats the first three bytes of a four-quote
  multiline terminator as the closing delimiter. It must consume the complete
  valid terminator, including the five-quote boundary case, then find a later
  root-level false boolean;
  malformed or ambiguous input remains manual repair.
- Windows replay deletes any safe file at the reserved candidate name when
  the original target remains. It must delete only a candidate whose recorded
  provenance still matches and leave a replacement untouched on ambiguity.
- Native certificate-ledger and Gmail-label commands load configuration or
  perform effects without holding the configuration admission permit. All five
  commands must retain that permit across their worker await, without changing
  the command results or Gmail-label behavior.
- A non-POSIX first-run create may publish the receipt-bearing file and then
  fail in cleanup or load. Such a result must be `outcome_unknown`, not an
  ordinary configuration failure that forces a conflicting retry; an existing
  pre-publication file remains `conflict`. The host may reconcile only the
  exact initialization receipt and settings already supplied in the request.
- The documented v1 engine envelope omits the admission token and its
  snapshot/compare operations. Document the implemented trusted-host contract
  and required-operation set without changing the wire version or behavior.

Verification: fail-first scanner, Windows replay, initialization, and native
command-gate tests; then targeted Python and desktop tests, Rust formatting and
targeted tests, and exact-head CI. Preserve the consent guard, secret-free
responses, and all unrelated product behavior. The native Windows package
check remains the final platform proof.

### Exact-head Windows admission-read repair

At head `2d606a1e34c603557f19f6f2a3dd2e5722db59c4`, the
`windows-operation-lock` job fails while `_read_admission_config` compares
the opened descriptor with the path inspection. The packaged first-run smoke
now reports `outcome_unknown` after publication, consistent with its following
runtime load hitting the same reader. The job does not expose the individual
stat fields, so the exact mismatching field is not yet proven.

Root cause: the non-POSIX admission reader uses `_safe_file_version`, a
POSIX-oriented tuple containing mode, uid, and gid, for path-versus-descriptor
comparisons. The Windows publication reader already has `_windows_stat_version`
for the platform's stable file identity, link count, size, timestamps, and file
attributes. A representation difference in POSIX-only stat fields therefore
rejects an otherwise unchanged Windows file before its bytes can be admitted.

Required change surface: use the existing Windows version comparator for all
three non-POSIX admission-read comparisons in `config.py`. Keep each regular
file, single-link, and non-reparse check, the same-descriptor read, and the
post-read path recheck. Add a fail-first regression in `tests/test_config.py`
for one safe file whose path and descriptor differ only in POSIX-style mode or
ownership representation. Re-run the existing same-byte atomic-swap rejection
tests in `tests/test_ntfy_disclosure_migration.py`, the focused Windows job, and
the packaged Windows smoke. Do not change POSIX reads, admission tokens,
consent semantics, the public error envelope, or the package gate. The native
jobs, not the Linux simulation, decide whether this actually fixes Windows.

### Native Windows first-run isolation after head 272c45e

At head `272c45ec93fcfe1d46b87af4f74414e4766a8483`, the required
`windows-operation-lock` job passes, but `windows-package` still exits at
packaged `config.initialize` with public code `outcome_unknown`. The
`_initialize_config_non_posix` boundary intentionally translates failures
after publication into that code, so the smoke result alone does not identify
whether cleanup, lock release, or the follow-up load raised. The previous
admission stat repair therefore has native read coverage but not packaged
first-run proof.

Problem-derived contract for this diagnostic phase: exercise real non-POSIX
first-run initialization in the Windows package job's existing Python test
step, with the same isolated state-home inputs as the packaged smoke. The
native test must assert the created config loads and retains its timezone;
an unexpected exception should preserve its native pytest traceback so the
root cause can be identified before changing production behavior. Limit this
phase to this contract and `tests/test_desktop_packaging.py`, already in the
fix-mode allowlist. Do not change the public error envelope, smoke failure
gate, Windows publication, config storage, consent behavior, dependencies,
or CI workflow. A passing direct test would narrow the defect to the packaged
environment; a failing test would supply the underlying exception. In either
case, repair and native packaged proof remain required before merge.

Diagnostic-only phase non-scope, superseded for the named current-head repair
classes above: do not change initialization semantics, the engine API,
configuration storage, Windows publication, ntfy consent, dependencies, or
the CI admission gate merely to make the smoke pass. The failed operation and
its exit status remain a failure.

Diagnostic-only phase verification: declare a fail-first test where a packaged engine returns a
nonzero exit and a valid v1 `configuration_error` envelope. It must currently
fail because the error code is omitted. After the diagnostic fix, run the
focused packaging test file and Ruff lint; the exact-head Windows package job
is the platform proof.

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
