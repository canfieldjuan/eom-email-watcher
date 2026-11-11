# Scheduled commands confine their own writes — #363

Status: **PROPOSED CONTRACT — not accepted. Implementation is not authorized by this
document.** No production code, test, unit file, package or installation change in this PR.

Base: `5b86ee362fe4012168db79a035a90ca84ca361cd`.

## Operator acceptance

Pending. Acceptance must answer four decisions; each is user-visible behavior.

- **D1 — no bypass.** A Linux host that cannot enforce R2 does not run scheduled commands at
  all (R4). That includes kernels before 6.2 and hosts with Landlock disabled. Mail intake
  stops there until the host is upgraded.
- **D2 — fixed write roots.** The roots in R2 do not follow the configuration. An install
  whose configuration points a written file outside them is refused at start (R3) until the
  file is moved.
- **D3 — metadata is outside the guarantee.** Permission bits, ownership, timestamps and
  extended attributes of files the user already owns can still be changed outside the roots.
  Closing that needs a mount namespace, which a user service cannot build under the Ubuntu
  restriction; the alternative is a root-installed system service, a different deployment
  model.
- **D4 — requests to user services are outside the guarantee.** A confined process can still
  talk to the systemd user manager and the session bus (measured below). Code running inside it
  could ask the manager to start a transient unit, which runs outside the boundary with the
  account's full write access. R2 therefore stops a defect that misdirects the process's own
  writes, not one that gains code execution in it. Closing this needs a confined process tree
  that uses no user-service socket: admission's manager query (`deployment.py:346-350`, reached
  from every `Store.connection`) and desktop notifications (`notifications.py:58-62`) would move
  out of it, and then socket use would be blocked. That is a product restructuring this
  contract does not choose.

## Root cause

| Owner | What it says today |
|---|---|
| Units, `systemd/eom-email-watcher.service:14-18` and `systemd/eom-monthly-hours.service:12-16` | Declare `PrivateTmp=yes`, `ProtectSystem=strict`, `ProtectHome=read-only` and `ReadWritePaths=` for the configuration root and the legacy state root. |
| Host policy, Ubuntu 24.04 default | A user manager has no `CAP_SYS_ADMIN`, so those directives need a user namespace. AppArmor moves that namespace into the `unprivileged_userns` profile, which denies every capability. The mount namespace is never built and the service runs anyway, exit `0`, with no log line. Measured in #363. |
| Tests, `tests/test_systemd_deployment.py:70`, `:199`, `:200` | Assert the directive text is present. `:85` and `:119` assert that default and legacy paths fall inside the two declared roots. Nothing asserts an effect. |
| Engine, `src/eom_email_watcher/cli.py:360-378` | Dispatches `check` (`:375`) and `send-hours` (`:377`) with no confinement of its own. |

The boundary is real, intended product behavior: `tests/test_systemd_deployment.py:85` pins
"grant only runtime config and legacy state parents". Only its enforcement is missing.

A host-side repair is not available. The unit bytes ship inside the engine
(`src/eom_email_watcher/deployment.py:275`) and admission compares them with the installed
files (`:641-654`), so a drop-in or a hand edit is refused. #341 records what any unit change
does to an upgrade.

## Measured inventory and reproduction

Installed service, from #363: the process that receives the AppArmor denial is the same one
that runs the engine in the host mount namespace, with `/` writable, under the label
`unprivileged_userns (enforce)`. `NoNewPrivileges=yes` and `UMask=0077` do take effect.

`reproduction.py` confines a child process with a Landlock ruleset that handles every
content- and namespace-changing right through ABI 3. It grants those rights beneath one
directory, grants write access to the null device, and then attempts 34 operations.
Expectations were written before the run. Host: kernel `7.0.0-42-generic`, Landlock ABI 8.

| Run | AppArmor label | User namespace | Rows as expected |
|---|---|---|---|
| Host namespace | `claude-desktop (unconfined)` | host | 34 of 34 |
| New namespace, identity map `1000 1000 1` | `claude-desktop (unconfined)` | new | 34 of 34 |
| New namespace, identity map, started from the default label | `unprivileged_userns (enforce)` | new | 34 of 34 |

The first two runs started from a shell whose own AppArmor label does not transition. The
third started from the default `unconfined` label, and the kernel logged the same
`userns_create` transition to `unprivileged_userns` that it logs for the service. That run is
the installed service's condition. Outcomes were identical in all three.

Refused outside the root (`EACCES` unless noted):

- create, open for write, open with truncation, `truncate(2)`, delete, `mkdir`, `rmdir`;
- rename within the outside tree, `mkfifo`, symlink creation;
- moving a file out of the root or into it;
- hard-linking an outside file into the root (`EXDEV`);
- writing through a symlink that sits inside the root and points outside;
- a sibling directory whose name only shares the root's prefix;
- a temporary file in the default temp directory (`tempfile` finds no usable directory);
- the same write attempted by a child shell.

Still working: every write inside the root, including a private temp directory there; reading
and executing files outside it; writing to the null device.

Not refused, and therefore not claimed by this contract:

- `chmod`, `utime`, `setxattr` and same-owner `chown` on a file outside the root;
- writing through a descriptor opened before confinement;
- writing through a hard link that already existed inside the root before confinement;
- a thread that existed before confinement: the main thread got `EACCES`, the earlier thread
  wrote outside;
- requests to unconfined user services over the session bus or the user manager's API, which
  can start processes or write files outside the domain (D4; measured under "Requests to user
  services" below).

Restriction without `no_new_privs` is rejected by the kernel (`EPERM`).

Two code facts follow from this:

- In the frozen Linux engine, every database connection passes admission
  (`src/eom_email_watcher/db.py:6422`), and admission opens `tempfile.TemporaryFile()`
  (`src/eom_email_watcher/deployment.py:361-362`, used at `:379`). Under confinement the
  default temp directory is refused, so R5 is required, not optional.
- The deployment lease is a read-only directory lock (`deployment.py:219`), and Connect locks
  live beside the database (`src/eom_email_watcher/source_locks.py:73`). Neither needs a root
  of its own.

`landlock(7)` gives ABI 1 as Linux 5.13, ABI 2 as 5.19 and ABI 3 as 6.2. Those kernels were
not tested here.

### Requests to user services

`ipc_probe.py` runs each case in an unconfined child (the control) and in a child confined with
the same ruleset as `reproduction.py`, in the same three conditions as the reproduction. It only
reads a manager property and calls `GetId` on the session bus; it never asks the manager to
start, stop or change anything. No transient unit was started to prove the consequence.

| Case | Unconfined | Confined |
|---|---|---|
| Write outside the root | ok | `EACCES` |
| Child shell writes outside the root | ok | refused |
| Write inside the root | ok | ok |
| Connect to the session bus socket | ok | ok |
| `systemctl --user show --property=Version` | ok | ok |
| `busctl --user call` to `org.freedesktop.DBus` `GetId` | ok | ok |

All three conditions gave these results, including the `unprivileged_userns (enforce)` label.
The probe also connected directly to the manager's private socket; that connection was refused
(`ECONNREFUSED`) in the unconfined control too, so it says nothing about confinement, and
`systemctl --user` reached the manager regardless. Oversight's independent read-only probe on
this host (PR review `5843823011`) got the same result for the manager query and a bus call.

The write rights this ruleset handles do not cover connecting to an existing socket (fourth
row), and the confined tree needs the manager anyway. Admission asks it for the unit graph on
every connection: `database_admission` (`deployment.py:848`) calls `_verify_description`
(`:861`, `:868`), which calls `_observe_graph` (`:820`), then `_manager_snapshot` (`:812`), then
`_manager_output` (`:579`), which runs `systemctl --user show` (`:346-350`).

### SQLite temporary files

`sqlite_tempdir.py` confines a child the same way, then makes SQLite spill a temporary table
and a sort to disk: 20,000 rows of 1,000 bytes with a 512 KiB page cache. Python 3.13.11,
SQLite 3.51.0.

| Case | Result |
|---|---|
| Control: `TMPDIR` inside the root when the process starts | ok |
| Control: `TMPDIR` set in-process before `import sqlite3` | ok |
| `TMPDIR` set after `import sqlite3`, before any connection | `unable to open database file` |
| `TMPDIR` set after a first connection | `unable to open database file` |
| `TMPDIR` never pointed inside the root | `unable to open database file` |
| `TMPDIR` unchanged, `PRAGMA temp_store = MEMORY` on the connection | ok |

SQLite's temp directory is fixed by the time `import sqlite3` returns. Pointing `TMPDIR` at a
private directory afterwards does not reach it. `cli.py:18` imports `db`, which imports
`sqlite3` (`db.py:10`), so the module is loaded before `cli.main` runs and before any
confinement placed there. Without the SQLite clause in R5, a confined run fails as soon as
SQLite needs a temporary file, for example a sort or temporary table larger than its cache.

### Write-site inventory

A syntax-tree scan listed every call under `src/eom_email_watcher` to a write-class API: file
opens for writing, renames, links, directory and temp-file creation, deletes, permission
changes, locks and `sqlite3.connect`. String and datetime `replace` calls were discarded, and
`benchmark.py` was excluded because it cannot run inside `check` or `send-hours`.

By the target expression at each remaining site, every write lands beneath the configuration
file's directory, a token file's directory or the database's directory, or sits in a
Windows-only path. The exception is the default-temp file in admission (`deployment.py:362`),
which R5 covers. The only SQLite connection site is `Store.connection` (`db.py:6425`).

In the pinned `connect-automate` dependency, the disk writers are entitlement installation,
reached only from the desktop operation `connect.entitlement.install` (`engine_api.py:6566`),
and an automation store this repository does not use.

Limits: sites were classified by their target expression, not by tracing every caller, and
third-party libraries other than `connect-automate` were not read. The settling trace in the
verification plan remains the authority.

## Required behavior

### R1 — which executions are confined

On Linux, every process that runs the body of a scheduled command is confined first. Scheduled
commands are the values of `SCHEDULED_COMMANDS` (`deployment.py:23`): `check` and
`send-hours`, with any of their options. This holds for the frozen engine's `eom-mail-watch`
alias and `--cli` form (`deployment.py:994-1014`) and for the source console script, whether a
timer or a person started it.

Confinement happens after argument parsing and before the command loads configuration
(`cli.py:193`), opens the database, or contacts a mailbox, model or notification service. The
paired-entry admission check at `deployment.py:1013` may run before it; that check reads
manager metadata only.

### R2 — the write boundary

After confinement, the process and every process it starts can change file content or
directory entries only beneath these roots:

| Root | Definition | Declared today as |
|---|---|---|
| Configuration root | `DEFAULT_CONFIG.parent` (`config.py:33`) | `ReadWritePaths=%h/.config/eom-email-watcher` |
| State root | `_runtime_state_root()` (`config.py:227-240`) | `StateDirectory=eom-email-watcher` |
| Legacy state root | `~/.local/state/eom-email-watcher`, when it exists and is not the state root | `ReadWritePaths=-%h/.local/state/eom-email-watcher` |
| Private temp | R5, always inside the state root | `PrivateTmp=yes` |
| Null device | open for writing only | — |

"Change" means create, open for writing, truncate, delete, rename and hard-link, and creating
a directory, symlink, FIFO, socket or device node. Outside the roots the kernel refuses each
of these. Reading files, executing files and network access are not restricted.

A process that the user manager or a session-bus service starts at the confined process's
request is not one the confined process started, and is outside this boundary (D4).

The roots do not depend on the configuration file's contents, so editing the configuration
cannot widen them. A `--config` path elsewhere is still read, but its directory does not
become a write root.

### R3 — configured write targets are admitted before any work

Before any mailbox, model or notification contact, the command resolves every configured path
a scheduled command can write and refuses to start if one lies outside the roots: exit status
`2`, with a message that names the configuration key. Resolution follows symlinks, so a link
inside a root that points outside is refused.

The set of such keys is one closed list at the confinement owner. A test fails when a
path-valued configuration key exists that the list classifies as neither read nor written.

### R4 — fail closed

On Linux, when any condition below holds, the command exits with status `2`, prints one
`error:` line naming the cause on stderr and nothing on stdout, and performs no
configuration, database, mailbox, model or notification access.

| Condition | Why it refuses |
|---|---|
| Landlock is missing or disabled | Nothing would enforce R2. |
| Landlock ABI below 3 | `truncate(2)` outside the roots could not be refused. |
| A root is missing or is not a directory | The boundary would be undefined. |
| The process has more than one thread | Earlier threads stay unconfined (measured). |
| `no_new_privs`, the ruleset, a rule or the restriction is rejected | Confinement is not in force. |
| The private temp directory cannot be created | R5 cannot hold. |

No flag, environment variable or configuration key skips confinement or turns a refusal into
a warning. On other platforms this contract does not apply and behavior is unchanged.

### R5 — private temp

Each confined run gets its own empty directory inside the state root, mode `0700`. `TMPDIR`
and Python's `tempfile` point at it for the run and its children. It is removed when the run
ends, on success and on error. A later run removes directories left by dead runs and never
one that belongs to a live run.

SQLite's temporary storage obeys the same boundary. Its temp directory is fixed before
confinement can run (measured above), so `Store.connection` (`db.py:6421-6430`), the one
connection owner, sets `PRAGMA temp_store = MEMORY` on every connection it opens, beside the
pragmas it already sets. The setting does not depend on whether the process is confined, so
the desktop engine and the scheduled commands use one SQLite behavior, and a connection
opened before confinement cannot reintroduce the failure.

### R6 — nothing else changes

On a host where the scheduled path already writes only inside the roots, stdout, exit status,
locks, admission, schedule, network destinations and stored results are unchanged.

The one change outside confined processes is R5's SQLite clause: every process that opens the
database, the desktop engine included, keeps SQLite temporary tables and sorts in memory.

`systemd/` stays byte-identical in the implementation PR. The engine's shipped unit payload
therefore does not change and installed admission is unaffected.

### R7 — claims match the guarantee

The README (`README.md:409` says "sandbox") and any other product text describe R2 and the
limits listed under "Not refused" above, and nothing stronger. In particular, no text says the
boundary holds against code running inside the scheduled process: D4 lets such code reach
unconfined user services.

### R8 — the units stop declaring what is not enforced (deferred)

`PrivateTmp=`, `ProtectSystem=`, `ProtectHome=` and `ReadWritePaths=` are removed from both
units, or kept only with an effect proof on a real user manager. The three text assertions at
`tests/test_systemd_deployment.py:70`, `:199`, `:200` are replaced by effect assertions.

This is a unit payload change. It is not implementable until #341's adoption path exists, and
it lands as one payload change together with #342's unit edit, in its own PR.

## Concurrency model

- Confinement is applied once per process, by the main thread, while the process has exactly
  one thread. It cannot be undone.
- Every child process inherits it (measured), including the manager query during admission
  and the desktop notification helper (`src/eom_email_watcher/notifications.py:58-62`).
- Descriptors opened earlier stay usable (measured). At confinement the process holds no
  writable descriptor to a path outside the roots, other than the stdio it was started with.
- The engine is a PyInstaller one-file build (`scripts/build_desktop_sidecar.py:304`) and
  was observed as two processes in the unit's control group (#363). Product code runs in the
  child, which is confined. The bootloader parent, which unpacks, waits and cleans up, is
  not.
- `check` and `send-hours` may overlap, and a second `check` exits on the production lock.
  Each process confines itself independently and each has its own temp directory (R5).
- A run killed by a signal may leave its temp directory; the next run removes it (R5).

## Mechanism

`mechanism: a kernel Landlock ruleset applied by the process to itself | the strongest
available to an unprivileged user service on this platform | justified`.

- A mount namespace would also cover metadata (D3). It needs `CAP_SYS_ADMIN`, which the
  AppArmor restriction denies to a user service.
- An AppArmor profile that grants `userns` to the service executor would reopen privileged
  user namespaces to anything able to start a user unit, and would repair one host, not the
  product.
- The input class Landlock misses is listed under "Not refused" above, with a test on each
  side in the verification plan.
- Requests to user services (D4) are not something a stronger mechanism at this owner closes.
  The confined tree needs the manager for admission, so closing them needs the restructuring
  described in D4.

SQLite temporary storage: `mechanism: PRAGMA temp_store = MEMORY on every connection, at the
single connection owner | the strongest available at that owner | justified`.

- Confining before `sqlite3` is loaded, with `TMPDIR` set first, would work only for the
  frozen dispatcher (`deployment.py:994-1014`), which imports `cli` lazily. The source console
  script `eom-mail-watch = "eom_email_watcher.cli:main"` (`pyproject.toml:21`) loads `sqlite3`
  while importing `cli`, before any product code runs. Any future earlier import would also
  undo it.
- `PRAGMA temp_store_directory` is deprecated by SQLite.
- Allowing writes to `/tmp` or `/var/tmp` would reopen the shared temp directories that the
  units meant to hide with `PrivateTmp=yes`.
- Setting it only in confined processes would make `db` ask the confinement owner a
  process-wide question, test a different SQLite behavior from the desktop's, and fail again
  if a connection were ever opened before confinement. Setting it on every connection has none
  of these.
- The cost is memory, in every process that opens the database: temporary tables and sorts stay
  in RAM. The base uses temporary tables of message and account keys on normal paths
  (`db.py:6039-6046`, `body_state.py:285`, `reconcile_state.py:109`), and migrations build
  indexes (`removal.py:212-238`), which sort. No plain `VACUUM` exists; `VACUUM INTO`
  (`thread_migration.py:131`) writes its target directly. The live database on the development
  host is about 3 MB.

## Required change surface and single owner

- New `src/eom_email_watcher/confinement.py`: the only owner of the roots, the ruleset, the
  R3 key list, the private temp directory and the R4 errors.
- `src/eom_email_watcher/cli.py`: one call, in `main`, for scheduled commands only. No other
  call site.
- `src/eom_email_watcher/db.py`: in `Store.connection`, the SQLite clause of R5, on every
  connection, with no dependency on the confinement owner. No other connection site exists.
- New tests. Four test files call `cli.main` or a scheduled body in-process
  (`tests/test_cli.py`, `tests/test_outbound.py`, `tests/test_engine_api.py`,
  `tests/test_ntfy_disclosure_migration.py`). The implementation must keep those from
  confining the test runner without adding a path that a shipped entry point can take.
- Packaged proof: an addition beside `scripts/smoke_packaged_engine.py`.
- `README.md`: R7.

Not touched: `systemd/`, `desktop/`, Windows code, `uv.lock`. No new dependency.

## Explicit non-scope

- Unit file edits (R8, gated on #341, shared with #342).
- Restricting reads, network destinations or metadata.
- The desktop engine API and non-scheduled CLI commands (`setup`, `doctor`, `recent` and the
  rest).
- Windows and macOS.
- AppArmor, sysctl or any other host policy.
- The LM Studio helper (#342).
- Installing or promoting a build on any machine.

## Verification plan

Fail-first at the base. Every confined step runs in a separate process.

1. **Boundary, both sides.** With real roots under a temporary home, each refused operation
   from the reproduction is refused and each inside-root operation succeeds. Boundary cases:
   the root directory itself, a sibling sharing its name prefix, a symlink out, a move in
   each direction, a hard link in, `truncate(2)`, and a child process.
2. **Limits stated, not hidden.** The five "Not refused" cases are asserted as they are, so a
   kernel change that closes one shows up as a test change, not as a silent claim. For D4, a
   confined child reads a manager property and makes a session-bus method call, and both
   succeed. The test never asks the manager to start or change anything.
3. **Reachability in the shipped entry.** The real scheduled entry runs as a separate process
   against local stand-in mailbox and model servers. A child process the body starts observes
   refusal outside the roots and success inside. Removing the single confinement call makes
   this fail while a valid control run in the same harness still passes.
4. **R3.** A written path outside the roots, directly and through a symlink, is refused
   before any network contact. The same path inside the roots is accepted. The key-list test
   fails when an unclassified path key is added.
5. **R4.** Each row of the table produces exit `2`, the stated message, empty stdout and no
   access. No environment variable or flag changes the result.
6. **R5.** Two overlapping runs keep separate directories. A directory left by a killed run
   is removed by the next one. A live run's directory survives another run starting.
7. **SQLite temp.** In a confined process, a spill through `Store.connection` (a temporary
   table and a sort larger than the page cache) succeeds. Removing the pragma makes the same
   run fail with `unable to open database file`. Every connection `Store.connection` opens,
   in a confined or an unconfined process, reports `temp_store` as `2` (memory).
8. **R6.** `git diff --stat <base> -- systemd` is empty, the engine's unit payload hashes are
   unchanged, and `uv run pytest` and `uv run ruff check .` pass.

Settling evidence, on a host with `kernel.apparmor_restrict_unprivileged_userns=1`:

- The frozen engine from the candidate package runs `check` under a real `systemd --user`
  manager with the shipped, unchanged units. The process label is
  `unprivileged_userns (enforce)`.
- A write outside the roots from inside that process tree is refused; a normal run completes
  with the same summary as an unconfined run on the same inputs.
- A syscall trace of that run lists every refused write-class call. A refusal on the
  product's own path blocks. A refusal inside a helper program is listed with its effect on
  the helper's result.
- The same trace lists every connection the confined tree makes to the user manager or the
  session bus (`systemctl`, `notify-send`), so D4 is observed on the real path.
- If the trace shows the scheduled path needs a root this contract does not name, the
  implementation stops and this contract is amended first.

## Evidence aliases (SHA-256)

Local folder `codex-evidence/eom-email-watcher/363-scheduled-confinement-contract/`:

- `reproduction.py`: `08b9f3aa0066a380a61d8dddbe944776115296c58edcf6d0b6b1c33e606ccd68`
- `reproduction-host.json`: `9e791e4412c70d988960f71ed19b735e0bc98323b3482430dd1e2ceecf6d666b`
- `reproduction-userns.json`: `dde40ee8dc02bb171012a455eaea31285a00230d85ccf0812d78f0c535d2615f`
- `reproduction-userns-default-label.json`: `aacec800804938eaf0c055bb620a429d318bb98c48443a611d4d6fd7054e3e72`
- `sqlite_tempdir.py`: `91e885cab82130113b5fd7372333231af8fe801ba0b9446c14bfeb5d9058df84`
- `sqlite-tempdir.json`: `027a962242f91d8728d6976e93d87b306863a9ba543ba9912da6101406766b88`
- `ipc_probe.py`: `9d07857fad9b46c5eadce4b4f3344e872096ed302cab73ad227a1be65e0d8a7f`
- `ipc-probe-host.json`: `adf3b73697ceda7588ab1350237ca2e0610f2251237a8f11f3730ccc242d7e58`
- `ipc-probe-userns.json`: `44242f403872e82a118fb3219a738056c39bbf01342119641a680afdafb0f592`
- `ipc-probe-userns-default-label.json`: `f451140677b5b5c0cd812228ebdcd1ee2dac60672d9cf2e7237d2d897648b3f0`

## Revision log

- `dacf251`: first proposal.
- `42a3fdf`, revision 2: a confined run cannot create SQLite temporary files, because SQLite
  fixes its temp directory when `sqlite3` is imported (measured). Adds the SQLite clause to R5,
  its mechanism, the `db.py` change surface, verification item 7, the write-site inventory and
  two evidence files. No requirement was removed or weakened.
- Revision 3, from oversight's PR review `5843823011`:
  - New limit and decision D4: a confined process can still ask unconfined user services for
    work. It is now in "Not refused", R2, R7, the mechanism, verification item 2 and the
    settling trace, with a measured probe and four evidence files.
  - `temp_store = MEMORY` now applies to every connection, not only confined ones (oversight's
    recommendation). This removes the `db` dependency on the confinement owner, adds the
    memory cost to R6, and replaces item 7's third assertion.
  - No requirement was removed or weakened. D4 states an existing limit; it does not narrow
    what R2 refuses.
