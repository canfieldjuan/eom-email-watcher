# Scheduled commands confine their own writes — #363

Status: **PROPOSED CONTRACT — not accepted. Implementation is not authorized by this
document.** No production code, test, unit file, package or installation change in this PR.

Base: `5b86ee362fe4012168db79a035a90ca84ca361cd`.

## Operator acceptance

Pending. Acceptance must answer three decisions; each is user-visible behavior.

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
  wrote outside.

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

### R6 — nothing else changes

On a host where the scheduled path already writes only inside the roots, stdout, exit status,
locks, admission, schedule, network destinations and stored results are unchanged.

`systemd/` stays byte-identical in the implementation PR. The engine's shipped unit payload
therefore does not change and installed admission is unaffected.

### R7 — claims match the guarantee

The README (`README.md:409` says "sandbox") and any other product text describe R2 and the
limits listed under "Not refused" above, and nothing stronger.

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

## Required change surface and single owner

- New `src/eom_email_watcher/confinement.py`: the only owner of the roots, the ruleset, the
  R3 key list, the private temp directory and the R4 errors.
- `src/eom_email_watcher/cli.py`: one call, in `main`, for scheduled commands only. No other
  call site.
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
2. **Limits stated, not hidden.** The four "Not refused" cases are asserted as they are, so a
   kernel change that closes one shows up as a test change, not as a silent claim.
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
7. **R6.** `git diff --stat <base> -- systemd` is empty, the engine's unit payload hashes are
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
- If the trace shows the scheduled path needs a root this contract does not name, the
  implementation stops and this contract is amended first.

## Evidence aliases (SHA-256)

Local folder `codex-evidence/eom-email-watcher/363-scheduled-confinement-contract/`:

- `reproduction.py`: `08b9f3aa0066a380a61d8dddbe944776115296c58edcf6d0b6b1c33e606ccd68`
- `reproduction-host.json`: `9e791e4412c70d988960f71ed19b735e0bc98323b3482430dd1e2ceecf6d666b`
- `reproduction-userns.json`: `dde40ee8dc02bb171012a455eaea31285a00230d85ccf0812d78f0c535d2615f`
- `reproduction-userns-default-label.json`: `aacec800804938eaf0c055bb620a429d318bb98c48443a611d4d6fd7054e3e72`
