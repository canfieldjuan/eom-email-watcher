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
- The shared admission owner checks configured watcher/monthly service commands
  and executing/main/control process identities before each Store connection.
  Both entrypoints call it early for fast refusal; startup cannot authorize a later open.
  This is a last defense consuming installed command identity; it does not
  catch schema errors downstream or relax the DB reader.
- Remove checkout-dependent service working directories. Preserve timer cadence,
  sandbox, notification delivery, authority and confirmation behavior.
- One deployment owner validates all configured scheduled readers; docs reference it.

## Differences from proven setup

1. Both existing entrypoints share one packaged executable; source-only machines
   retain the locked uv installation when no desktop engine exists.
2. Bundled startup and each database acquisition refuse legacy/mixed readers,
   auxiliary command phases, replaced executing engines and incompatible active workers.
   Input wait does not cache admission. No service is silently cancelled or config migrated.
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
artifacts. An incompatible active worker must finish before desktop migration; paired readers may overlap.

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
| deployment.py:62,100 | validates loaded readers and active executable identity before API/CLI dispatch | public schema28 digest unchanged on refusal; same binary migrates/reads |
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
and remain available to the installer. The initial own/parent MainPID exception
and blanket other-worker refusal are superseded by the loaded/running identity
revision below. Both entrypoints now consume the same executable-identity owner;
compatible readers may overlap, incompatible readers block migration. No
downstream runtime catch is added.

## Contract revision: canonical scheduled-unit directory

Independent post-publication boundary probe on bcf90be deliberately reproduced two
failures: empty/relative XDG_CONFIG_HOME reached API migration without inspecting
normal scheduled readers. I introduced the runtime resolver mismatch; the shell
used a different fallback. Fix at service_unit_directory in deployment.py. Both
packaged installer and initial runtime consumed it; manager admission below now
queries all canonical unit names independently of disk location. Source-only installer calls the same
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


Current correction verification: the four fail-first cases pass after the class
fix. Adjacent tests passed 156 with one Windows-only skip; the equivalent nested
condition cleanup then passed all 56 deployment tests. Ruff, bash syntax and diff
checks are clean. Actual rebuilt v3 bundle passes the retained public schema28
upgrade oracle, legacy loaded-unit refusal with on-disk files absent, unchanged DB
on incompatible active-reader refusal, real installer pairing, migration/CLI read,
and simultaneous bundled API plus monthly dry-run. The four deployment/API/CLI/DB
compiled modules match the candidate source; no native settings changed. Prior
v1/v2 artifacts remain retained. Normal deployment and exact-head CI/review are
NOT DONE, with the substitutions and tested versions recorded in durable evidence.


## Accepted consolidation: artifact kind and effective timer graph

Operator directed "address the blockers" after root note6046531049. This accepts
Rule21's third-round consolidation before a further fix. Both findings originate
in my bcf90be implementation, confirmed by git log -L and the retained exact-head
public reproduction: source shim aborts; both redirected timers are admitted.

Root cause: install-user-services.sh:20-23 guesses bundle ownership from a single
excluded uv path; deployment.py:10-13 guesses scheduled readers from base service
names. Expected provenance/address is not authoritative deployed identity.

Required change surface:
- The installer qualifies Linux native ELF artifacts before paired-protocol probes.
  Shebang console shims are source artifacts regardless of virtualenv, uv, runner,
  path order, symlink or directory spelling. Skip them to find an actual native
  bundle, or retain source-only mode when none exists. Unrecognized executable
  kinds and incompatible discovered native bundles fail closed before publication.
  Remove known-uv-path exclusion rather than adding source-path cases.
- One canonical deployment timer/service/verb mapping derives service readers.
  Before either DB dispatch, inspect loaded timers' effective Unit strings and
  reject redirects/malformed/unknown targets. Explicit not-found timers are absent
  only with confirmed inactive state. Both canonical services still use existing
  loaded-command and running-inode verification. No arbitrary target graph crawl.
- Update existing public manager smoke fixture for timer properties; keep it
  explicitly substituted. Upgrade proof reuses the retained public schema28 oracle
  and actual bundled overlap, adding redirected timer rejection before DB change.
- Commit the public minimal source-shim and both timer reproductions; sibling
  tests cover arbitrary source dirs, source/native PATH order, malformed native
  candidates, old/valid native protocol, missing/loaded timer state, malformed and
  redirected Unit, inactive/active not-found timers. Canonical systemd declarations
  must agree with the single mapping. Native installer fixtures represent artifact
  kind using a minimal compiled launcher; full bundle proof remains authoritative.

Explicit non-scope: no model/runtime/prompt/schema, timer cadence/sandbox, normal
profile/config/DB, confirmation, downstream schema catch or legacy reader relaxation.
No source console entrypoint rewrite. Linux native qualification is installation
scope; packaged Windows startup still has no systemd checks.

Verification plan: retained fail-before3cases on eeb0b5d; commit those regressions
and show pass after. Run targeted/adjacent packaging/deployment tests, Ruff, bash
syntax and diff checks; rebuild; preserve prior evidence separately, prove bundle
compiled-source equality and public upgrade/redirect/overlap. One consolidation
push, then exact-head independent review/CI after the standing waiting gate.

Assumptions/blockers: source console shims are scripts; supported Linux packaged
engine is an ELF executable (observed retained qualified bundle). Source wrappers
are not a packaged artifact. Custom redirected timer targets are rejected rather
than silently rewritten; normal canonical timer definitions remain supported.
Merge and normal deployment wait for new exact-head CI/review.


Accepted consolidation verification: the committed minimal three-case probe fails
before the fix and passes after. Adjacent 195 passed, one Windows-only skip; Ruff,
bash syntax and diff checks pass. Only formatter and documentation edits followed
that suite; behavior is unchanged. Rebuilt v4 packaged smoke passes. Both effective
timer redirects reject API/CLI with schema28 and DB digest unchanged, even after
actual shell reinstall retains persistent drop-ins. Source shim is never executed;
actual native bundle selected. Existing partial/removed-loaded-reader/old-worker
refusals, migration30/CLI read and actual paired overlap continue passing. Four
bundled modules match source. Substituted manager metadata, development authority,
public DB/config and monthly dry-run remain explicit limitations. Earlier proof
versions retained separately; exact-head review/CI, merge and installed normal
native busy recovery remain NOT DONE.


## Accepted consolidation: database acquisition, execution phases and proof manager

ACCEPTED by the operator on 2026-10-07: "I accept the plan", referring to
PR217 root note6047252859. Record this acceptance before implementation.
The public round4 reproduction gives15 expected failures: twelve unchecked
auxiliary-command cases, two real retained-bundle replaced-inode migrations
(schema28 to30, including waiting for stdin), and packaged Connect admission
failure. Origins: bcf90be's partial ExecStart/startup-path policy and eeb0b5d's
manager requirement with only one proof updated.

Required surface: deployment.py owns the canonical effective execution-phase
and process-identity policy. Check all six auxiliary command arrays and both
MainPID/ControlPID as typed manager metadata; only the canonical paired
ExecStart is admitted. Empty auxiliary arrays are valid; missing/malformed
arrays and active incompatible control processes are refused. The executing
process /proc/self/exe must match the installed binary at database acquisition,
including after stdin wait; recheck identity after manager inspection. Route
Store.connection through that owner before sqlite3.connect. An early
entrypoint check may use the same owner to fail fast, but cannot authorize a
later database open. Preserve read-only protocol/directory/version probes and
compatible paired workers. No manager-admission cache authorizes later opens.

One script-only proof-support owner supplies the typed explicit empty-manager
fixture to both smoke_packaged_engine.py and connect-packaged-deb-proof.py.
This fixture is not a production bypass. Reuse the existing packaged upgrade,
CLI/overlap and Connect proof lane; commit minimal public regressions for all
execution phases, both readers, typed control PID, both entrypoints and
replacement before/after startup. Keep independent fixture expectations.

Non-scope: DB schema/version/migration logic, model.py, prompts, model/host
policy, dependencies, installed normal config/services, DocSum files and merge
holds. Freeze source and actual rebuilt bundle; prove negatives leave public
DB unchanged and positives retain migration/CLI/overlap. Run affected tests,
Ruff/format/bash/diff checks and existing packaged smoke/Connect entrypoint.
CI owns duplicated broad suites. One consolidation push after cold diff and
source/bundle equality. Exact-head CI/review/normal deployment remain pending.


Consolidation verification: tracked fail-first regressions rejected no auxiliary
commands, did not bind the executing process at Store.connection, and could not
query an isolated Connect manager. The retained native bundle additionally
migrated after replacement during admission and stdin wait. Corrected supported
Connect operation reproduces the old manager failure before the fix; the original
probe used an unsupported operation name, corrected in evidence rather than
production. All retained round4 cases now pass (15 passed), adjacent tests pass
262 with one Windows-only skip; Ruff, bash syntax and diff checks pass.

Rebuilt development bundle passes existing smoke. Public schema28 fixture stays
unchanged for every auxiliary phase on both services through API and CLI, both
replacement barriers, legacy/redirected readers and incompatible workers.
Compatible migration30, CLI reading and active paired overlap still work.
The existing Connect proof environment and engine_request reach the supported
entitlement status operation with expected development authority state. Four
compiled modules equal candidate source. No release-provider interoperability,
normal installation or native inference is claimed by the substituted proof.

Cold diff: deployment.py owns all execution phases and process identities;
db.py adds only the owner call before SQLite connect. One script-only manager
fixture replaces smoke's duplicate and serves existing Connect isolation.
Deployment/Connect regressions cover mixed, malformed, falsy, compatible and
replacement cases. README and this canonical contract describe database
acquisition. There is no schema, model, runtime, dependency or normal-profile diff.
Gap audit: implementation and local packaged proof complete; exact-head CI/review,
merge and later normal compatible installation/native busy recovery remain pending.


## CI correction: isolated script-test import context

Exact published cc7d308 Windows packaging fails collecting test_desktop_packaging:
smoke_packaged_engine imports the shared proof helper without scripts on sys.path.
I introduced the import at cc7d308: scripts/smoke_packaged_engine.py:14. Runtime
script execution supplies that directory automatically; spec/runpy test loaders
need the same context. Adjacent Linux verification was masked by other test
modules mutating sys.path during collection. This is my verification omission.

Required surface: one test import-context owner in conftest supplies the scripts
directory before collection. Remove duplicated path mutation from all script-test
readers, including the temporary COI insertion. Add clean-process isolated
collection probes for each reader, and run the failed packaging file alone before
an adjacent gate. No production/proof helper/model/bundle/module change. This
correction changes test bootstrap, not database admission or thinking policy.
Fail-first must report ModuleNotFoundError: packaged_proof_environment in isolated
desktop packaging collection; afterward all isolated readers collect successfully.
Reuse committed runtime/frozen proof evidence because production source is unchanged;
record the no-relevant-diff comparison and defer duplicated broad suites to CI.


CI correction proven: the clean-process desktop collection regression failed
with the exact Windows ModuleNotFoundError, then passed after one conftest owner
replaced five per-reader sys.path mutations. All six isolated collection probes
and affected proof/packaging tests pass:167 passed, one Windows-only skip.
Ruff and new/bootstrap format checks pass. Cold diff is test bootstrap/readers,
regression and this contract only. No production/proof/bundle/dependency diff
against cc7d308; reuse its native/module equality receipts for that unchanged
source. Windows execution itself remains CI-owned and pending after publication.
