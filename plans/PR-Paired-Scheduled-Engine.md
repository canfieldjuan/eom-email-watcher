# Paired scheduled engine (Watcher #214)

Current contract: the accepted positive deployment-description consolidation below
supersedes earlier property-by-property admission and PATH-discovery revisions.
Those revisions remain historical evidence of my rework, not current policy.

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

## Accepted positive deployment-description consolidation

Operator acceptance: discussion_r4213460156 accepts proposal discussion_r4213453141
with four conditions, reaffirmed directly in this session. This contract-only
revision precedes implementation. The hold stays unresolved until oversight
verifies the one consolidation push; operator clearance is required before merge.

### Root cause

My initialbcf90be7dd8c8d363fbc3e09ee4038d1a0063177 split desktop identity between
installer PATH selection and runtime sys.executable, and represented the admitted
unit graph as selected properties. My8db2017/cc7d308 extended that incomplete
representation. Retained positive-graph-reproduction.json on7186dc9 admits an
activation override/alternate fragment/pending reload/changed lmstudio helper,
counts24 manager subprocesses and selects the stale first of two native inodes.
Five published follow-up corrections repaired my initial change or follow-ups.

### Required change surface

- deployment.py owns one immutable description of the selected running native
  engine inode, alias/unit directory and five canonical shipped unit payloads.
  Installation and admission consume it. Bundle resources use systemd/ as the
  payload owner, through the existing sidecar builder.
- A packaged install names the concrete desktop sidecar; its native process owns
  pairing and unit installation using its running inode. Remove PATH native
  selection. Keep the source-only locked uv lane explicitly exempt from native
  database admission, with no implication of paired desktop safety.
- All five unit files, including lmstudio, are pinned: exact FragmentPath, empty
  DropInPaths, NeedDaemonReload=false and byte/digest equality with shipped payloads.
  Fully absent/inactive graph permits initial startup; partial/mixed/modified,
  redirected/transient/stale-loaded/masked graphs refuse before DB migration.
- One bounded systemctl user-manager read per normal admission covers the fixed
  unit set and scalar metadata. One parser owns record identities/types and fails
  on missing/duplicate/malformed metadata. No per-property subprocess API or
  AUXILIARY_EXECUTION_PROPERTIES denylist. Keep fresh checks at each Store open;
  active incompatible processes fail closed, observed exits require typed zero
  re-observation, helper active execution refuses, and self/alias/file identities
  are checked around the snapshot. No cached migration authorization.
- DeploymentError is a distinct Exception, never RuntimeError. Existing stale and
  concurrent handlers must propagate it. The API presents deployment_refused,
  and packaged CLI refusal identifies deployment instead of stale/concurrent state.
- README documents that masked units or a missing user manager intentionally block
  every packaged desktop DB open until the deployment is repaired.
- Deferred post-send stuck-reservation risk is filed before the push at
  https://github.com/canfieldjuan/eom-email-watcher/issues/219. Fresh admission may
  still refuse record_outbound and mark_outbound_ambiguous after an actual send;
  do not retry delivery, cache admission or claim this follow-up is solved here.

### Explicit non-scope

No model/thinking/gateway/prompt/template setting change, 9B promotion, DocSum
work, native runtime correction, schema changes, normal installation before
qualification/merge, new UI flow, arbitrary dependency traversal or unrelated
formatting. Preserve service cadence/sandbox, confirmations, dedupe/locks and
release authority. No automatic email, unit cancellation or reservation recovery.

### Assumptions and blockers

The fixed shipped graph is the admitted graph; overrides require operator repair.
Source-only uv remains exempt by explicit decision. The existing native proof
harness and public schema28 oracle remain authoritative for packaged behavior.
Before removing redundant argv property checks, demonstrate the exact surviving
old argv[0]-name mutation against a new public regression; preserve its invariant
via exact shipped payload/alias identity and prove its replacement owner mutation
also fails. Startup scheduled-alias admission must have its own mutation regression.
Operator clearance and independent exact-head review remain merge blockers.

### Verification plan

Reproduce each accepted finding before implementation. Commit minimal public
regressions for whole graph, explicit engine selection, Store subprocess count,
typed refusals reaching API and not swallowed by stale/concurrent handlers.
Mutation-probe the scheduled alias startup and old argv[0] check, then their
post-consolidation equivalents. Cover canonical/all-absent success; mixed/partial,
redirected/modified/drop-in/stale/masked/missing manager refusal; all five units;
timeout/output limit/malformed/duplicate/missing metadata; compatible active
readers, active helper, incompatible PID exit, self/alias/file replacement races.
Reuse the shared proof-manager fixture and existing smoke/Connect package proof.
Rebuild/freeze native resources/source; original public schema28 stays unchanged
on negatives and valid paired migration/API+CLI overlap works. Incremental adjacent
suites plus required lint/format/bash checks, one consolidation push. No hold
resolution before pushed class proof; oversight verifies and operator clears merge.

### Implementation summary / cold diff audit / gap audit

NOT DONE: implementation, fail-before/pass-after, mutation proof, rebuilt native
proof and new-head CI/review. Acceptance and deferred issue are recorded here
before source/tests/config edits. The following receipt will record actual evidence.

### Implementation clarification: one read includes an observed exit

The accepted one-read condition is literal. A mismatched observed PID refuses
that admission, even if it exits before /proc inspection. A later independent
admission observes typed zero and can proceed. Remove the old in-check second
manager observation; do not waive a missing process or cache authorization.
This is the sole exit-race difference from the earlier two-observation proof.
The regression must show refusal then fresh-zero success, one read in each.
Native installation is explicit --engine ABSOLUTE_SIDECAR; source-only install
is explicit --source. Neither mode searches PATH for a desktop. Both modes
consume deployment.py's canonical shipped payloads; native pairing stays there.

Native publication ordering: preflight the existing manager graph and active
identities before writing. Existing canonical base files may be an older release,
so preflight validates graph identity while the post-reload check pins new bytes.
After writing, reload and positively verify the shipped graph before enabling
timers. A persistent activation override must not be enabled by a failed install.
Both phases use the one graph/reader validator; each check makes one system read.


### Consolidation implementation receipt

Implemented the accepted one-owner class consolidation. deployment.py owns the
immutable native deployment, five shipped payloads, fresh bounded manager parser,
installation preflight/post-reload checks, file/alias/current-image barriers and
startup admission. The builder snapshots the same payload owner; the installer
names --engine explicitly or selects the exempt --source lane. Removed the
per-property subprocess API, directive denylist and PATH-first engine selection.
DeploymentError now inherits Exception and reaches the API as deployment_refused;
existing stale/concurrent handlers propagate it without individual patches.

Evidence:18fail-first expected; both original mutation survivors caught before
removal, replacement startup/payload/type and duplicate-owner mutations caught.
Local required suite2369passed2skip; later proof-environment and actual Store
process-count regressions78passed1skip then37passed. Ruff/bash/diff clean. Native
v6 build/smoke, four compiled modules and all five frozen payloads equal source.
All34 native graph negatives preserve the public schema28 database; real native
installation, migration30, paired API/CLI overlap and typed current-image refusal
pass. The explicit isolated manager is a proof substitution; Windows runs in CI.

The consolidation also corrected its own proof-fixture missing HOME at that
owner, and the retained schema-oracle helper's unclosed SQLite connection. A
public reproduction showed copying schema28 over a schema30 path with a live
read connection still reads30 from its WAL; closing the helper fixes isolation.
Production was unchanged for these proof corrections. Durable aliases below
retain raw evidence, source/bundle pins and the per-file cold diff audit.

Durable consolidation-v6-receipt.json, sha256 e0a25519fc49f8bc54da89ee7d5ccba9f32cae5799bfcd2f170b50fd40d265a5.
Alias mutation-survivors-before.json, sha256 01d6921443b3d7ba69aa8b07c11b4e58f73f4612e9f70da7d32fe93aec6cac57.
Alias mutation-survivors-after.json, sha256 023adc947491cbac4c13f8e5d268fae2d0738cfc82bff49c29a61dae084605dc.
Alias freeze-receipt-v6.json, sha256 d7ceb9c0841a21464727e5ccbe09a1ee2c6707d50a9160f52b99ea1f20cbf944.
Alias packaged-schema-proof-v6.json, sha256 9d66b720ec872113e499e0f048ed1fc766f9d168171bc6758c9f8af70020774f.
Alias cold-diff-audit-v6.md, sha256 714135eaadc1ea39ce5336ff0bd07e7d03b7751c01533f7c8c4aae5b6cb404a8.

Gap audit: implementation/local/native proof DONE; new-head CI and oversight
verification/operator merge clearance NOT DONE. Hold remains unresolved.
