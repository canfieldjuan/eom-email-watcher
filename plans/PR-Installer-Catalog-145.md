# Windows fresh-install catalog refusal

Operator accepted the bounded repair proposal on 2026-10-28:
“Authorize bounded installer repair”. This is separate from polling behavior.

## Root cause

The production installer from main `efce16e60732caafb9454d1610e3d626de5958fd`
refuses a fresh standard-user installation with “Unable to write package file
catalog”. Both silent and interactive attempts create an empty catalog before
any cutover phase. `scripts/windows_installer_template.py:catalog_macro`,
introduced by `21ed141c`, tests NSIS's inherited error flag after FileOpen without
clearing it first. Native clean/stale/open-failure twins must establish causality
before changing that owner; a real failed open must remain a refusal.

## Required change surface

- The generated catalog-open boundary, preserving file inventory and validation.
- Existing native catalog fixture/tests: exercise the actual generated macro
  with clean and set incoming error state, and an actually unwritable target.
- Existing installer invocation helper and native proof: ordinary fresh setup
  without `/UPDATE`, then verify its catalog and receipt against packaged bytes.

## Explicit non-scope

No polling, cutover policy, permissions, schema, timeout, package parity waiver,
live app/settings, or unrelated installer redesign. Do not compensate by using
`/UPDATE` for a fresh installation. Keep existing upgrade proofs intact.

## Assumptions/blockers

Native execution is required. The dedicated Windows VM and candidate artifact
are isolated and authorized. Rebuilt production installers remain subject to
exact-head CI/review and merge gates; the old candidate cannot qualify by proxy.

## Verification plan

Fail first with the real macro and a set incoming error flag, while the clean
control succeeds and a directory at the catalog filename refuses. Then repeat
all twins after the origin fix, run adjacent packaging/catalog tests and Ruff,
and execute ordinary fresh and existing upgrade installation in native CI.
Retain exact compiler/source/executable hashes and raw results outside worktrees.

## Implementation summary

Native fail-first confirms the owner: clean input succeeds, inherited error
state refuses with exit 2 despite a writable path, and a directory at the
catalog path refuses. After resetting the incoming flag immediately before
FileOpen, both writable twins succeed with the exact catalog and the real open
failure still refuses. No later workaround or earlier special case is needed.

The local probes use Ubuntu's privately extracted NSIS 3.09 compiler and execute
its Windows binaries in the isolated standard-user guest. They do not substitute
for CI's pinned build compiler or the rebuilt full installer. The native tests
reuse CI's existing qualified_compiler binding to the actual Tauri build.

## Cold diff audit

- `windows_installer_template.py:catalog_macro`: reset unrelated incoming state
  only at FileOpen; actual failure still aborts. Native three-way reproduction.
- `windows_catalog_retry_fixture.py:catalog_error_state_source` and
  `run_catalog_error_state`: execute the real macro, retain source/compiler and
  executable identity plus results; no replacement rule.
- `prove_windows_cutover.py:run_installer`: optional ordinary invocation, keeping
  update mode as the existing default. The fresh-install regression opts out.
- `test_windows_cutover_round9.py`: writable-state twins assert exact catalog;
  bad-path twin asserts refusal/no output; fresh ordinary setup verifies receipt
  hashes and packaged engine identity. Native CI owns the full installer proof.

Local adjacent Python verification: 182 passed, 44 platform skips. Ruff passed.
The native macro twins above executed on Windows; the Linux skip count is not
reported as Windows proof. No broad suite was duplicated locally.

Independent review evidence should include
`boundary-probe: writable clean/inherited inputs accepted; actual open failure refused`
and `effect-trace: ordinary install false refusal | catalog_macro FileOpen error
boundary | native inherited-state failure before, exact catalog success after`.

## Gap audit

NOT DONE: native cause isolation, origin repair and local regressions are done;
exact-head rebuilt native installer proof and independent review remain. Issue
145's installed polling runs remain blocked until this separate repair qualifies.
