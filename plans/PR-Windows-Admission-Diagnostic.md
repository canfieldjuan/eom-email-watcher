# Windows admission diagnostic

## Contract

Root cause: the admission coordinator discards errors at inspection, worker staging and retained-token revalidation. On the disposable Windows guest, the installed desktop reports manual repair even without the observer, while serial snapshot/compare/snapshot calls accept the unchanged configuration and identical token. The underlying native refusal is still UNKNOWN. The diagnostic gap originates in `run_admission_attempt` and `AdmissionCoordinator` in `desktop/src-tauri/src/lib.rs`, introduced by `153e0d4bbf8fbc017f6e88a516f38d7ee4fa316a` (#176). That is not yet attribution of the runtime failure itself.

Required change surface: `lib.rs` logs the decision stage and a bounded error-code identifier through one formatter. `inspect_err` preserves each Result at inspection, staging, initial revalidation, retained-binding refresh, acknowledgement and mutation activation. Snapshot errors name their inner stage too. Every existing check, refusal, revocation, cancellation, timeout, returned value and UI string stays unchanged.

Explicit non-scope: configuration edits; retries; timeout changes; relaxed admission; APIs/schema; Python engine changes; observer changes; mailbox/provider/calendar operations; live installation or settings; timing qualification; a fix for the underlying native failure. The diagnostic may be built and exercised only as a separately labelled candidate in the disposable guest. Preserve original artifacts, the installed engine identity and package parity gates.

Acceptance: Juan directly answered **“Authorize bounded diagnostic”** in this session. This permits implementation, tests and the isolated native diagnostic build/run. It does not accept a permanent product merge. Oversight's code-sanitizer requirement fits that scope: error codes must match ASCII `[a-z0-9_]{1,64}`, otherwise use `invalid_code`. Never include error.message. Caller stages are static literals.

Assumptions/blockers: the desktop's actual failing stage/code is unavailable until native reproduction with this diagnostic. Timeout remains a hypothesis. Native Windows build, independent exact-head review, and the diagnostic installed run remain gates; do not claim the prior binary's qualification from modified-desktop results.

## Verification plan and results

- Fail-first behavior: `admission_refusals_report_the_deciding_stage_and_code` runs existing fail-closed admission tests in child test processes and checks actual stderr. Before implementation it failed on the missing `inspect (engine_timeout)` diagnostic. The original assertions still execute; no existing assertion changed.
- Fail-first privacy: `admission_diagnostic_refuses_unbounded_or_non_identifier_codes` failed before the sanitizer because the empty code was printed instead of `invalid_code`.
- Formatter tests cover empty, lengths 1/63/64/65, ASCII letters/digits/underscore, mixed valid/invalid text, a private sentinel, whitespace, punctuation, NUL and non-ASCII. Error-message exclusion is a separate exact-line test. The real decision-path regression verifies production uses the formatter.
- Local `cargo test --locked --lib admission`: 16 passed. `initialization`: 6 passed. `mutation`: 19 passed. Filters overlap; these are not a unique-test total.
- `cargo fmt --check`, `cargo clippy --locked --all-targets -- -D warnings`, and `git diff --check` passed.
- CI owns the duplicated broad suites and native Windows build. No Python behavior changed; the broad Python suite was not duplicated locally.

## Implementation summary

Only the accepted contract and `desktop/src-tauri/src/lib.rs` change. The diagnostic preserves original Results using `inspect_err`, including the existing failure paths. No downstream workaround or duplicated admission rule is introduced. The formatter is the sole owner of new diagnostic sanitization.

## Cold diff audit

- `lib.rs:418`: formatter bounds and sanitizes only error-code metadata; static stage and sanitized code form the line. No error message, config content or path is included.
- `lib.rs:440,456,1029,1160,1203,1331,1387`: all approved decision sites retain the original result and branching. Existing checks and refusal assertions are unchanged.
- `lib.rs:3096`: real decision-path regression verifies stderr while running existing fail-closed tests; exact summary and exact diagnostic lines prevent substring false positives.
- `lib.rs:3145,3178`: sanitizer boundaries and private-message exclusion.
- This contract records acceptance, scope and evidence without changing executable behavior.

Boundary-probe: valid identifier twins preserved; empty, oversized, control, non-ASCII and mixed private text receive the fixed placeholder; downstream output consumes only the sanitized code. All focused tests pass.
Effect-trace: admission refusal becomes diagnosable | existing decision-site Results and the shared formatter control stderr | the actual decision-path test failed with missing stderr before implementation and passes afterward, while original refusal assertions still pass.

## Gap audit

NOT DONE: native Windows CI/build, independent exact-head review, diagnostic guest replay and the underlying refusal's origin remain. No merge, live install, promotion, timing claim or issue completion is authorized by this diagnostic contract.

## CI correction within the diagnostic scope

Reproduction: at ba688e1a5085fda8c15a89b9a54bbcffd7a79fa5, the existing native startup source-order guard fails with “staged workers must revalidate their admission token,” matching desktop CI.
Origin: this diagnostic commit changed AdmissionCoordinator::install_attempt in lib.rs so rustfmt split workers.revalidate() across lines. The unchanged source-order guard requires the intact call before workers.activate(). Runtime ordering remained intact; the diagnostic source shape violated its existing guard.
Required fix: bind the result once with `let revalidated = workers.revalidate();`, then inspect that result and branch exactly as before. No test, assertion, gate, timeout, error or activation rule changes. Keep the existing regression as the fail-first guard; do not duplicate it. Run pnpm test, formatting/lint and the relevant native admission/order tests. This correction does not authorize permanent merge or guest use before exact-head green CI and delta review.

Correction results: the existing guard failed before the binding change and passed afterward. Full `pnpm test` passed all 248 tests with no skips using a private `TMPDIR` outside Git. The initial full run had five fixture-generation failures because `/tmp/.git` made the default temporary directory a forbidden evidence destination; the shared evidence guard correctly refused it. No fixture, assertion, guard or host Git marker changed. Native admission tests passed (16), the staged-worker ordering test passed (1), formatting and Clippy passed. Raw before/after logs and the environment-failure log are retained in the correction receipt. Exact-head CI and oversight delta review remain pending.
