# Bounded Windows admission diagnostic — accepted for isolated diagnosis

## Root cause and confirmed boundary
The installed desktop repeats the manual-repair refusal when opened through its ordinary shortcut with the observer stopped. Screenshot: admission-control-normal-launch.ppm. No configuration edit occurred.
The exact same installed engine succeeds at config.admission.snapshot -> config.admission.compare(current=true) -> config.admission.snapshot; both tokens are identical. Receipt: admission-control-tokens.json. This rules out token instability in that serial control; it does not prove what happened inside the desktop.
Desktop admission decision owner: desktop/src-tauri/src/lib.rs run_admission_attempt (:418), AdmissionCoordinator::install_attempt (:1127), refresh_with (:1156), and acknowledge_with (:1280). Revalidation delegates through AdmissionWorkers::revalidate (:671) to Engine::compare_admission (engine.rs:2629). The decision sites currently discard the error via Err(_), is_err or is_ok. The originating failure remains UNKNOWN.
The error-discarding decisions trace to 153e0d4bbf8fbc017f6e88a516f38d7ee4fa316a, Gate ntfy delivery on explicit disclosure consent (#176). This identifies the diagnostic gap's introducing commit, NOT yet the introducing commit of the native refusal.

## Required change surface
Apply the adjacent proposed patch only to desktop/src-tauri/src/lib.rs on a separate diagnostic branch. Add a stderr diagnostic containing static decision-stage and EngineError.code only. Retain the Result unchanged using inspect_err at the current decision sites; retain every existing refusal/revocation path. Cover all admission revalidation readers, including the acknowledgement and mutation paths, without creating a second admission rule. Add a formatting regression which requires stage/code and excludes error.message. Run existing fail-closed admission tests and applicable Rust formatting/lint. No claim that a diagnostic fixes the underlying failure.
Then build the diagnostic desktop through normal native Windows CI and exercise it only inside the disposable guest with the same packaged engine, unchanged config and normal native launch. Label the modified desktop as a diagnostic candidate; do not mix its results with qualification of the prior binary. Preserve the original installed candidate and all parity receipts. Record the exact failing stage/code, isolate its origin and introducing commit, and prepare the proper repair contract with fail-first regression before changing behavior.

## Explicit non-scope
No live installation/settings, config edits, package-engine replacement, relaxed checks, timeout adjustment, new retries, bypass, provider/mail/calendar operation, observer modification, schema or API change, silent assertion weakening, timing qualification or issue closure. No product repair is authorized by accepting a diagnostic alone.

## Assumptions/blockers
The runtime cause is not yet known; a timeout is only a hypothesis. Existing stdout/stderr omits the swallowed failure. Source and extra diagnostic tooling were excluded from the accepted qualification scope, so operator acceptance is required before applying/building this patch. The patch is a concrete review draft, not applied or tested code.

## Verification and gap audit
Control reproduction and stable serial token proof DONE. Diagnostic implementation, tests, native build and origin proof NOT DONE. Existing fail-closed regression must remain unchanged and pass; new formatting regression must fail without the diagnostic formatter. Native diagnostic receipt must expose the actual decision error before a repair is proposed. Independent review and existing merge/install gates remain.

## Operator acceptance and review refinement
Juan directly answered “Authorize bounded diagnostic” in this session. This authorizes implementation, verification, a separately labelled native diagnostic build and disposable-guest reproduction. It does not accept a permanent product merge or repair the underlying native refusal.
Oversight reviewed the proposal and requires EngineError.code be restricted to ASCII [a-z0-9_]{1,64}, with the fixed placeholder invalid_code otherwise. Add private-sentinel, empty, non-ASCII, whitespace, control, length-boundary and valid-code twins. Stages are caller-provided static literals. Never log error.message.
The regression must exercise real decision sites in a child test process and assert stderr carries the stage/code while existing fail-closed assertions still pass. Add this failing-before proof before implementing the diagnostic; retain the formatter privacy/boundary tests afterward. Preserve exact-head CI, independent review, and normal packaging gates. No production merge until separately accepted.

## Implementation summary / cold audit / gap audit
Contract accepted. Implementation, fail-first proof and native diagnostic run NOT DONE.
