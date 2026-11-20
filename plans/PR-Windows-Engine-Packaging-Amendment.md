# Windows admission timeout packaging amendment

Proposed amendment to the [accepted measurement contract](PR-Windows-Admission-Timeout-Repair.md) and its [acceptance record](PR-Windows-Admission-Timeout-Repair-Acceptance.md). Juan accepted the direction of testing a Windows one-folder engine. Independent review and Juan's acceptance of this exact revision precede implementation.

## Root cause

`scripts/build_desktop_sidecar.py:298–317` uses PyInstaller's one-file mode for Windows. Git history traces that choice to `d01031dd99d732096523ba52e844a81974d3a40e`. Every engine launch extracts the embedded runtime before Python can load. On ordinary desktop startup, initial admission activates workers and the frontend then requests admission status. The second comparison can spend nearly its entire request deadline in the bootloader. The five-second deadline at `desktop/src-tauri/src/engine.rs:49` applies to `compare_admission` and starts after stdin dispatch. [PyInstaller bootloader sequence](https://pyinstaller.org/en/stable/advanced-topics.html#the-bootstrap-process-in-detail)

The hosted lifecycle proof reproduced `refresh_revalidation (engine_timeout)` using the shipped engine. Successful and timed-out compares reached the Python child in 1.74 s and 4.61 s on one runner and 2.17 s and 4.68 s on the other. All four overlapping starts in each run lagged the preceding successful comparison. The file trace identified the extraction work, including 163,816,395 requested write bytes before the child started. It reproduced an earlier inspection timeout under heavier observation. Oversight agreed that the additional time accumulated before Python and that the resource responsible for the slowdown is unmeasured.

## Validated inventory

| Evidence alias | Revision or SHA-256 | Use |
| --- | --- | --- |
| [Shipped one-file engine](https://github.com/canfieldjuan/eom-email-watcher/actions/runs/43688624140) | Main `e0ec48dc816bb32ad496fc765ecbb785fc004fab`, engine `aedcd11df8ded8a220c5d55373ee64d1c60a9d522b806e158c8be239034412d3` | Baseline installer, engine and payload identity. |
| [Lifecycle result](https://github.com/canfieldjuan/eom-email-watcher/actions/runs/44238992893) | Head `5e6e3109f1aa4b780516afe3ac180746f1b0b2d5`, result `155a2fc4fbd7f4f281fac22bacdb0dddc759650f53dbad8b576acd7a1bbbd6ca` | Installed NSIS diagnostic, synthetic Inbox fixture, process and image WPR profile, PID joins and same-launch successful comparisons. |
| [File trace result](https://github.com/canfieldjuan/eom-email-watcher/actions/runs/44238045085) | Head `108482f9d20814e0cf2aad218f6ec28224184957`, result `dff648799bda2431a102b9ff4414e20d722177ddd91124076fc06296ffd16780` | Extraction operations and bytes, with observer timing caveat. |
| Oversight startup trace review | Receipt `893a8d02`, report `3c723af95988d5d4525251f206dc1f874c8d835cd7b204027f9ff82e73595bd7` | Supports the diagnosis on both heads and calls for the same concurrent workload and deadline in the candidate. |

Use the retained synthetic seed, original installer and scope of the process-and-image proof. Both arms run the same engine entry point, pinned Python, locked dependencies, fixture bytes and desktop request logic. The synthetic fixture exercises configuration admission locally.

## Required change surface

1. **Build and install the Windows engine as a one-folder payload.** Select the layout in the existing sidecar build owner in `scripts/build_desktop_sidecar.py`. Keep the installed `eom-mail-engine.exe` entry point and its receipt location so the desktop, the installer and existing installation verification resolve the same executable. Stage the runtime tree in its PyInstaller-relative layout. Use Tauri's structured Windows bundle-resource mapping and the existing NSIS template pipeline to include the complete tree in install, update and uninstall. Keep the release and qualification builds on the same packager. [Tauri resource mapping](https://v2.tauri.app/develop/resources/#embedding-additional-files)
2. **Preserve installed identity and lifecycle guarantees.** Derive the installed paths from the actual Tauri/NSIS payload via `scripts/windows_installer_template.py`. Retain `desktop/src-tauri/windows/cutover.ps1` as the owner of executable ownership, process quiescence and receipt publication, and `src/eom_email_watcher/windows_cutover.py` as the source-lock receipt reader. The existing engine digest names the executable. It cannot demonstrate parity of newly separate runtime files. The controlled build, installer extraction and installed verification must bind executable and runtime files by relative path and digest using the existing packaging inventory owner. Verify that installation and upgrade take ownership of those files under the existing program-directory protection and that the engine loads the shipped runtime from that tree. Establish how independent runtime files affect the existing receipt and source-write guarantee before implementation sign-off. An executable-only parity check would miss a missing or substituted dependency with unchanged executable bytes.
3. **Update the existing packaging proof consumers.** Carry the complete staged and installed tree through `scripts/build_windows_cutover_qualification.py`, `scripts/windows_cutover_parity.py`, `scripts/smoke_packaged_engine.py` and the Windows packaging job in `.github/workflows/ci.yml`. Trace the scripts that launch the installer or copy its engine and give them the same package representation. Preserve Connect data equality, executable ownership checks, and smoke coverage in both release and labelled qualification artifacts. A Windows smoke run of the executable requires its adjacent runtime directory.

## Explicit differences and scope

- This amendment adds Windows packaging and its direct consumers to the previously accepted desktop measurement surface. Windows loads runtime files installed beside the executable and stops unpacking them into a per-launch temporary directory. Linux keeps the current packaging route.
- The proof compares the retained one-file baseline and the reviewed one-folder candidate in disposable hosted Windows environments. This extends the original local guest measurement lane using the hosted setup that reproduced the refusal without access to the live installation. Both arms run through NSIS. Diagnostic source changes and evidence remain separately identified and the published app omits diagnostic instrumentation.
- Reuse the host's production request path and the light process-and-image observer. The DLL-load marker is comparable across layouts. Attribute it to the engine process or its verified Python child as appropriate. Python child creation is recorded only where it occurs.
- Admission, stale-token detection, cancellation, deadlines, ordinary concurrent startup, source-lock protocol, installer privilege model and user-data ownership keep their existing contracts. The current deadline bounds a startup refusal and is the baseline needed for the comparison. A longer deadline would allow the same extra bootloader work to pass and would invalidate the proof of the packaging change. Live app, settings, records and packaged Inbox qualification are outside this amendment.

## Assumptions and blockers

- CPU, disk, filter-driver and antivirus contributions remain unmeasured. The repair removes the per-launch extraction work observed before Python, so it can be tested without attributing that contention to a particular resource.
- The operation identities of the other three overlapping starts were absent from the prior capture. Identify them in the baseline through the existing desktop launch owner using operation labels and PID/creation identity. Reuse those labels for the candidate. If shorter lifetimes reduce natural overlap, also replay the same four recorded operations through the existing request path with coordinated dispatch on a synthetic fixture. This isolates the packaging change from a reduced workload. The ordinary startup result and the coordinated comparison are reported separately.
- If preserving installer ownership or source-lock receipt guarantees requires changing installation identity policy or receipt format, document the gap and review that change before implementing it. Proof hashes establish byte parity. The installed program boundary continues to rely on the installer and the receipt verifier.

## Verification plan

| Requirement | Evidence |
| --- | --- |
| Source and installed identity | Check exact source, build flags, executable SHA-256, full runtime inventory, NSIS destinations and installed relative path/digest parity. Use the pinned bundler and SDK as the sources of truth. Probe a missing or mismatched runtime file while leaving the executable unchanged and require the inventory verification to reject it. |
| Fail before, pass after | On matched disposable Windows runners reproduce the originally observed `refresh_revalidation (engine_timeout)` with the one-file package and synthetic fixture. Install the one-folder candidate through NSIS and repeat normal startup and the four-request workload. An unreproduced baseline is inconclusive for the repair claim. Keep the original proof and before/after traces. The one-file baseline is the regression control. |
| Timing and result | Use the light lifecycle profile, host request marks and PID/creation identity. Record the process start and Python DLL load for all four matched launches. Each load must occur by the end of the existing five-second admission window measured from its own stdin dispatch, as a proof benchmark for concurrent startup. Require successful revalidation to complete within its actual host deadline. Preserve the successful serial comparison as a control and report measured headroom. Verify both the process outcome and the host response. |
| Negative and adjacent behavior | Prove stale admission and real timeout still fail closed, and run existing request cancellation and process-tree checks. Exercise fresh install, one-file to one-folder upgrade, and uninstall, with the retained one-file artifact proving recovery from the candidate in the disposable environment. Verify executable, runtime and receipt identity, existing source-write checks, preservation of configuration/data, and cleanup of candidate-owned files with unrelated files preserved. |
| Exact candidate | Run applicable packaging, path, smoke and lifecycle checks plus required CI on the implementation revision. Reuse the installed proof at that revision before calling the Windows startup repair complete. Packaged Inbox remains a separate native proof. |

## Implementation summary and cold diff audit

This revision adds the amendment document. The diff records the measured origin, the proposed package boundary and the fail-before/pass-after proof. The accepted measurement contract and its acceptance record retain their original bytes. No product build or native execution is part of this document change.

## Gap audit

NOT DONE

Independent review of this amendment and Juan's acceptance of its exact revision are pending. Implementation, exact-candidate CI and Windows proof are future deliverables. The startup trace review is complete.
