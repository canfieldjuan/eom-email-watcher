# Automation Confirmation Inbox

## Why this slice exists

Issue #159 asks the desktop inbox to complete a prepared automation confirmation. The engine already projects fire identity, state version, and prepared identity into inbox attachments, and `automation.fire.decide` already makes an atomic expected-state decision. The root cause is that the desktop has no Tauri command or inbox control to reach that operation, so effectful fires can remain `awaiting_confirmation`.

The diff exceeds the 400-line soft cap because this end-to-end UI-to-engine bridge includes separate async decision, Tauri payload, admission, and regression proofs. Splitting those proofs from the controls would leave the safety-critical vertical path unreviewable in one PR.

### Problem-derived contract

- A correct fix must carry the projected fire ID, state version, and prepared identity unchanged from the inbox through a guarded Tauri command to `automation.fire.decide`, with `confirmed` or `declined` as the engine decision.
- Only a fire projected as `awaiting_confirmation` with a valid prepared identity may offer a decision. One in-flight decision per fire prevents duplicate desktop submissions.
- Both success and stale/error outcomes refresh the inbox. A stale decision must be shown as a recoverable refresh, not reported as a successful provider action.
- The engine's atomic decision, dispatch, entitlement, and prepared-identity semantics must not change. The desktop must not directly submit provider work.

### Review-fix contract (PR #179, exact head 0f06179)

- Root cause 1: the native decision bridge persists `pending_dispatch` but never wakes the Connect queue. When the scheduler has no next wake, it blocks on its channel until an unrelated signal. A successful confirmation must signal the queue after the decision returns; decline or failure must not signal it.
- Root cause 2: after a failed inbox refresh, the old card remains mounted but the decision handler unconditionally re-enables its stale controls. A failed refresh must leave both decisions disabled and offer a retry of the inbox refresh; only a successful refresh may replace that card with current state.
- Concurrent extension of root cause 2 (exact head `92f47e1`): the decision refresh checks only whether the global request generation changed. A queue-event refresh can supersede this request, cause its `loadInbox` to return without rendering, and then fail itself. Generation change is not proof that this decision's inbox projection committed. `loadInbox` must report whether its own query result was rendered; the decision refresh must require that result before enabling decisions.
- Required change surface: `desktop/src-tauri/src/lib.rs`, `desktop/src/main.ts`, focused Rust and desktop tests in those files and `desktop/test/automationDecision.test.ts`. No engine decision, scheduler, database, schema, public API, dependency, or unrelated inbox behavior changes.
- For the concurrent extension, change only `desktop/src/main.ts`, `desktop/test/automationDecision.test.ts`, and this plan. Keep `loadInbox`'s call arguments, caller fire-and-forget behavior, inbox error reporting, queue event scheduling, native wake, and provider behavior unchanged.
- Assumption: the engine's expected-version and prepared-hash transaction remains the authority for races across windows; the desktop prevents only same-card retries of known-stale state.
- Verification: fail-first focused desktop source/runner test and Rust wake-routing test; then targeted test reruns, TypeScript build, Rust format, and exact-head CI after push. No broad local suite duplicated from CI.
- Concurrent-extension verification: fail-first desktop regression for a superseded decision load, then the focused desktop test and build. CI on the updated head owns the cross-platform suites. A successful inbox query with optional capability discovery warning still counts as a committed projection.

### Current-head repair contract (PR #179, exact head `5fd2a82`)

- Root cause, stale-control class: `renderInbox` replaces DOM nodes during a pending decision, but failure cleanup touches only the captured nodes. The currently mounted panel can remain enabled after the decision runner releases its in-flight reservation. Track a refresh-required fence by fire ID, render pending controls disabled, and make a failed refresh retire the currently mounted panel too. Clear the fence only when a full inbox query started after the failed decision commits a fresh page.
- Root cause, truthful status: the decision response is a snapshot from before the subsequent inbox refresh. A fast queue dispatch can settle the fire before the refresh completes; the handler must not overwrite a fresh projection with a claim that provider work has not completed. Keep the refreshed inbox status on successful refresh; when refresh fails, report only the saved decision and need to refresh.
- Root cause, red desktop CI: the scope-retention source test hardcodes `loadInbox(): Promise<void>` while this PR intentionally changed the return to `Promise<boolean>`. Update that direct test contract and retain its effect-scope assertions.
- Required change surface: `desktop/src/main.ts`, a testable fence transition in `desktop/src/automationDecision.ts`, `desktop/test/automationDecision.test.ts`, `desktop/test/gmailLabels.test.ts`, and this plan. Do not change the engine, queue scheduler, provider dispatch, schema, dependencies, CSS, or unrelated inbox controls. The status correction removes a false claim; it does not add a new product promise.
- Verification: reproduce the focused CI failure; add fail-first assertions for the mounted-panel fence and accurate post-refresh status; test the fence transition at older/equal/newer generations and append-only loads; then run the affected desktop test files and build. CI on the new head owns the broad and platform suites.

## Scope (this PR)

Ownership lane: automation-confirmation-inbox

Slice phase: operator-visible vertical proof

1. Add one admission-guarded Tauri bridge for the existing automation decision operation.
2. Render attachment-scoped confirm/decline controls only for a prepared, awaiting fire, and refresh the inbox after any decision outcome.
3. Add focused bridge and desktop decision tests for exact identity, confirm, decline, stale error, and double-click suppression.
4. Wake the Connect queue on successful confirmation, and retain disabled stale controls with a refresh retry when the inbox cannot reload.

### Files touched

- `desktop/src-tauri/src/engine.rs` - typed result, exact payload bridge, focused bridge test.
- `desktop/src-tauri/src/lib.rs` - admitted Tauri command and registration.
- `desktop/src/automationDecision.ts` - guarded desktop decision runner.
- `desktop/src/main.ts` - attachment controls and inbox refresh/status.
- `desktop/src/styles.css` - attachment-scoped decision layout.
- `desktop/test/automationDecision.test.ts` - decision and source-wiring regression tests.
- `desktop/test/gmailLabels.test.ts` - direct inbox scope test updated for the committed-result loader return.
- `desktop/test/ntfyDisclosureMigration.test.ts` - retain the admission-gate assertion for the new command.
- `tests/test_connect_v2_engine_api.py` - prove the real inbox query projects the prepared and refreshed fire.
- `plans/PR-Automation-Confirmation-Inbox.md` - this contract.

### Review Contract

Acceptance criteria:

1. The real `inbox.query` attachment projection carries the prepared identity to the desktop, whose attachment loop offers controls only when `state === "awaiting_confirmation"` and `prepared_identity_sha256` is a lowercase SHA-256; the focused Python engine test and desktop decision test settle the projection and admission boundaries.
2. A confirm or decline sends the exact projected `fire_id`, `state_version`, and `prepared_identity_sha256` through the Tauri bridge to `automation.fire.decide`; the Rust bridge test and desktop decision tests settle the mapping.
3. A second click while the first decision is in flight cannot submit a second request; the async double-click test settles this.
4. Success and stale/error responses refresh through `loadInbox`; stale/error remains visible and never claims provider work was submitted. The desktop decision tests cover refresh and `main.ts` uses the result only for status.
5. The existing engine expected-state and prepared-identity checks remain unchanged; `tests/test_connect_v2_engine_api.py` already covers stale hash and replay, and this PR does not touch that engine path.
6. `desktop/src-tauri/src/lib.rs` wakes the Connect queue only after a decision returns `pending_dispatch`; its focused Rust test covers confirmed, declined, and failed results, while `scheduler.rs`'s `None => receiver.recv()` branch shows why the signal is required.
7. When `loadInbox` fails after a decision, the old card's Confirm and Decline controls remain disabled and a refresh retry is available; the focused desktop test covers that result and source wiring.
8. The decision refresh accepts only its own `loadInbox()` result when that call rendered a current query page. Early skip, query failure, or superseded-generation returns are false; a rendered page remains true even if optional capability discovery warns. The focused desktop test checks both sides of this return contract and the decision caller's use of it.
9. If a panel rerenders while a decision is in flight, its controls are disabled; if the later decision refresh fails, the mounted panel shows only a refresh retry. A full query started before or during that failure cannot release its fire-ID fence; a full query started afterward can release it only after its page commits. The desktop source test checks the render path, and the runtime fence test checks older/equal/newer and append-only boundaries.
10. When a confirmed decision refresh succeeds, `main.ts` preserves the refreshed inbox status rather than asserting the provider remains unfinished. When refresh fails, the decision status is historical and requests a fresh inbox; the desktop source test checks both branches.
11. The scoped `gmailLabels.test.ts` assertion recognizes `loadInbox(): Promise<boolean>` and still verifies effect-scope checks after capability discovery. The focused test settles the red desktop CI failure.

Affected surfaces: desktop inbox attachment card, Tauri engine bridge, existing engine operation.

Risk areas: confirmation safety, stale state, double click, Tauri admission, response/error mapping.

Triggered reviewer rules: R1, R2, R3, R5, R6, R8, R9, R12, R13, R14.

Reachability proof: `inbox_query` renders an attachment fire in the desktop inbox, the operator clicks Confirm or Decline, Tauri invokes `automation.fire.decide`, and the refreshed inbox projection plus status reports the resulting state or stale refresh.

## Mechanism

The desktop decision runner checks the projected state and hash, reserves the fire ID synchronously, forwards the exact identity to an injected submit function, refreshes after either outcome, and releases its reservation. The Tauri command requires configuration admission and calls the existing engine operation under the mailbox operation gate. The UI never constructs a prepared identity or calls provider dispatch directly.

On successful confirmation, the native command signals the existing Connect queue scheduler after the engine returns `pending_dispatch`; no signal is sent for decline or an error. If inbox refresh fails, the existing card becomes decision-inert and presents only a refresh retry until a fresh projection replaces it.

For concurrent inbox loads, `loadInbox` returns whether this call committed a current query page to `renderInbox`. The decision refresh consumes that specific result; a later request merely incrementing the global generation cannot falsely re-enable stale controls.

The current-head repair adds a fire-ID refresh-required fence. New panels consult it and the in-flight reservation; failure cleanup rerenders the mounted inbox from that state. A full query only releases a fence when its generation began after the failed decision and its page committed. Successful decision refresh leaves the status from that fresh query intact; an unrefreshed result reports only the saved decision and need to retry.

## Intentional

- The desktop identifies the rule by its existing ID and attachment name; this slice does not invent a new rule-description or action-summary contract.
- A successful confirm means the fire is authorized for pending dispatch, not that the provider action has completed.
- The engine's atomic stale/version/identity check is authoritative; the desktop guard only avoids invalid and duplicate local submissions.

## Deferred

Parking predicate: adjacent automation-management UI, presentation polish, and robustness that do not block this confirmation path or its safety. Parked hardening: none.

- A human-readable prepared-action summary or navigation to rule settings needs a separate approved projection and UX contract; this slice shows the existing rule ID and attachment filename.

## Verification

Local: `node --test --experimental-strip-types test/automationDecision.test.ts test/ntfyDisclosureMigration.test.ts` (13 passed); `pnpm build` (built); `pnpm build:sidecar` (`packaged-engine-smoke: ok`); `cargo test automation_decision_bridge_forwards_exact_prepared_identity --lib` (1 passed); `cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check` (passed); `uv run pytest -q tests/test_connect_v2_engine_api.py::test_automation_confirmation_binds_stable_preparation_and_admits_after_decision` (`. [100%]` after the new inbox assertions); `uv run ruff check tests/test_connect_v2_engine_api.py` (`All checks passed!`); `git diff --check` (passed). Browser fixture check could not run because Chrome lacked a usable sandbox and in-app browser control was unavailable; no bypass flag was used. Exact-head CI remains for PR publication.

Review-fix loop: fail-first desktop test returned 6 pass / 1 expected source-branch failure, and fail-first Rust compilation returned E0425 for the missing wake helper. After the fix, the focused desktop pair returned 14 passed; `pnpm build` passed; `cargo test automation_decision_wakes_queue_only_after_confirmation --lib` and `cargo test repeated_queue_wakes_are_coalesced --lib` each returned 1 passed; `cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check` passed. The four CI jobs were green on the original PR head only; the updated head must be checked separately.

Concurrent-extension loop: fail-first desktop test failed as expected on `Promise<void>` rather than a committed-result return. After the fix, the focused desktop pair returned 15 passed and `pnpm build` passed. All four CI jobs were green on head `92f47e1`; the next head must be checked separately.

Current-head repair loop: the red desktop CI job reported 68 passed / 1 failed at `gmailLabels.test.ts:508`; the focused local reproduction failed at the same assertion. The two new fail-first desktop tests failed on the missing fire-ID fence and false provider-completion claim; the runtime fence test failed on the missing helper export. After the repair, the affected desktop files returned 45 passed before the helper extraction, the final `pnpm test` returned 72 passed, `pnpm build` passed, and `git diff --check` passed. Head `5fd2a82` still has red desktop CI; the next head requires its own CI and review.

## Estimated diff size

Updated PR diff: 10 files, +706 / -18. Over the 400-line soft cap for the indivisible bridge, wake/refresh correction, and regression evidence; exact count is checked before push.
