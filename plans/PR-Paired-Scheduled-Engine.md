# Paired scheduled engine (Watcher #214)

## Validated inventory

Durable inventory: `paired-scheduled-engine-20261007/inventory.json`. It retains
`coi_local_proof.py`, the approved-native-coi digest, CUDA Qwen3.5-9B Q4_K_M,
pinned native runtime/template/request settings, matching certificate fields,
replay/restart checks and every source difference from installed revision83aa5cd.
Prior source parity is evidence for its recorded heads, not this deployment.
Installed consumer supports schema28; merged source supports schema30. No normal
DB migration is authorized before its desktop and scheduled readers are paired.

## Root cause

`packaging/engine_entry.py` packages only the API while
`scripts/install-user-services.sh` deploys an independent uv CLI snapshot;
`systemd/*.service` keeps using that snapshot after a desktop update.
Separate database owners produce a partial update and a newer unsupported DB.
The prior source-only local alignment did not remove this release defect.

## Required change surface

- One packaged entrypoint dispatches API or existing CLI with the same modules.
- Installed service CLI is an alias of the bundled engine, never another snapshot.
- Installer verifies the packaged dispatch before changing scheduled units/alias;
  an installed incompatible desktop engine fails closed instead of falling back.
- The bundled API entrypoint checks configured watcher/monthly service commands
  and active workers before request handling, hence before any Store migration.
  This is a last defense consuming installed command identity; it does not
  catch schema errors downstream or relax the DB reader.
- Remove checkout-dependent service working directories. Preserve timer cadence,
  sandbox, notification delivery, authority and confirmation behavior.
- One deployment owner validates all configured scheduled readers; docs reference it.

## Differences from proven setup

1. Both existing entrypoints share one packaged executable; source-only machines
   retain the locked uv installation when no desktop engine exists.
2. Bundled API startup refuses legacy/mixed readers and active scheduled workers
   before reading input. No service is silently cancelled or config migrated.
3. Scheduled services no longer require a development checkout as their cwd.
4. Candidate source uses merged schema30, whereas installed source is schema28;
   inventory lists every upstream difference. Pairing is proven on a public
   schema28 copy before normal deployment. Native settings remain unchanged.

## Explicit non-scope

No model/prompt/runtime, certificate schema, DB schema revision, confirmation,
timer cadence, mailbox delivery setting, unrelated UI or entitlement changes.
No normal DB rollback, historical retry migration or independent CLI update.

## Assumptions/blockers

Linux user services installed by this project are the deployment scope; Windows
has no such timer. Existing overrides must resolve to the paired executable.
Normal installation and real busy proof require reviewed source and compatible
artifacts. An active scheduled worker must finish before desktop migration.

## Verification plan

- Fail-first public subprocess: packaged entrypoint --cli --version must dispatch
  the CLI; existing API-only entrypoint returns invalid_json instead.
- Fail-first installer: incompatible discovered engine must reject before uv,
  service publication or any DB migration.
- Boundary probes: no services, compatible alias, mixed/legacy command, malformed
  unit metadata, missing executable and active worker; validate both services.
- Public schema28 copy: reject mismatch with unchanged digest/user_version; paired
  engine migrates it and the same binary CLI reads it successfully.
- Existing systemd authority/state-path and packaged smoke tests, Ruff/bash syntax.
- Build shared executable; exercise API and CLI from it; reuse approved COI native
  proof and field/replay/restart oracle after compatible installed deployment.

## Implementation summary / cold diff / gap audit

Source implementation complete; normal profile remains untouched.

| Owner | Actual change | Evidence |
|---|---|---|
| packaging/engine_entry.py:1 | delegates to shared deployment dispatcher | CLI fail-first and real packaged alias proof |
| deployment.py:62,100 | validates configured readers and active worker before API/CLI dispatch | public schema28 digest unchanged on refusal; same binary migrates/reads |
| install-user-services.sh:15 | verifies and atomically pairs alias; preserves source-only authority path | incompatible installer fail-first, paired/source-only tests |
| systemd watcher/monthly units | removes checkout-dependent cwd | systemd/state/sandbox tests |
| smoke_packaged_engine.py:132 | existing build smoke reads API-owned DB via bundled CLI | packaged-engine-smoke: ok |
| deployment/systemd/packaging tests | negative, mixed and valid boundary/migration regressions | 127 passed, 1 skipped |
| README two-hour timer | canonical paired/source-only install directions | cold diff audit |

Gap audit: NOT DONE. Exact-head review/CI and normal compatible deployment/native
busy recovery are pending. Development bundle proof has no embedded release
mail-provider identities/authority, and systemd metadata is substituted. It is not
an installed normal-profile or public release claim. Model/template/prompt/settings
and existing native COI harness remain unchanged; retained native parity applies
only to its recorded earlier heads.

## Contract revision: both entrypoints open the database

New evidence: CLI-first regression fails with `CLI/migration reached` because my
unpublished deployment dispatcher returned to cli.main before the API-only check.
The API check alone was incomplete. The same startup owner must check either
entrypoint before handing off. Read-only protocol/version probes do not open a DB
and remain available to the installer. Scheduled CLI checks can admit their own
systemd MainPID (including the PyInstaller bootloader parent); other active workers
still block migration. Test API-first and CLI-first partial updates, own-worker
admission and another worker refusal. No downstream runtime catch is added.

## Contract revision: canonical scheduled-unit directory

Independent post-publication boundary probe on bcf90be deliberately reproduced two
failures: empty/relative XDG_CONFIG_HOME reached API migration without inspecting
normal scheduled readers. I introduced the runtime resolver mismatch; the shell
used a different fallback. Fix at service_unit_directory in deployment.py. Both
packaged installer and runtime consume it; source-only installer calls the same
module through its installed interpreter. Remove the shell's independently
constructed unit directory and the startup expression. Read-only directory
metadata does not open a DB. Probe unset/empty/relative/custom absolute paths,
including spaces, and prove installer output equals runtime selection.

Directory correction proven: two fail-first XDG bypasses now reject; both installer
modes use the one service_unit_directory owner. 127 passed, 1 Windows-only skip,
Ruff/diff/bash checks clean. Rebuilt v2 executable passes the existing packaged
smoke and public schema28 proof; empty/relative/custom absolute XDG values resolve
correctly, and the real shell installer consumes the binary's directory result.
Old evidence retained; v2 upgrade evidence is separate. Normal profile untouched.


## Contract revision: loaded reader and running executable are authoritative

Exact-head review and four deliberate failures on 08f8de5 confirm defects in my
initial bcf90be implementation: deployment.py:83 trusts disk presence instead of
loaded manager state; deployment.py:107 equates any foreign active PID with an
incompatible reader; install-user-services.sh:21 compares lexical and canonical
paths. These are identity/authority errors at deployment admission, not database
or model failures. This is own correction round two; consolidate the class once.

Required surface: the one startup owner queries manager LoadState for every
canonical service name, with only explicit not-found plus zero MainPID considered
unconfigured. Loaded units remain checked when their files are removed, renamed,
or placed elsewhere. Missing bus, malformed metadata, and other load states fail
closed. ExecStart/argv must retain the paired binary and command identities.
For active readers compare /proc/MainPID/exe to the current binary by file
identity, including overwritten/deleted old executables. Compatible simultaneous
watcher/monthly/API readers remain admitted; existing check/outbound operation
locks and outbound dedupe continue owning their effects. Remove the own/parent
PID exemption and blanket active refusal. A worker that exits during observation
is admitted only after the manager reports zero MainPID, otherwise fail closed.

The installer uses Bash -ef file identity to distinguish its uv console script,
removing the asymmetric readlink/string comparison. Cover HOME and absolute
XDG_DATA_HOME with symlink, dotdot, and spaces, plus source and desktop candidates.
Update README to the canonical contract. Smoke uses an explicitly substituted
empty manager on Linux, because its private HOME must not inspect normal-profile
units; record the substitution, never present it as installed-systemd proof.

Verification: four fail-first cases retained in review-class-fail-before.json;
manager and process identity negative/positive/mixed boundaries, both dispatch
paths and concurrent paired activations, explicit not-found, malformed/bus errors,
old overwritten executable, worker exit race; isolated shell reinstallation and
adjacent packaging tests. Rebuild and reuse the public schema28 packaged upgrade
proof, with an isolated loaded-manager fixture and paired overlap. Native model
harness, pins, prompts, authority, confirmation, service sandbox, cadence, schema
and normal profile remain unchanged. Retain prior proof artifacts separately.
