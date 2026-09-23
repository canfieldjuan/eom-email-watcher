# Automation Confirmation Inbox

## Why this slice exists

Issue #159 asks the desktop inbox to complete a prepared automation confirmation. The engine already projects fire identity, state version, and prepared identity into inbox attachments, and `automation.fire.decide` already makes an atomic expected-state decision. The root cause is that the desktop has no Tauri command or inbox control to reach that operation, so effectful fires can remain `awaiting_confirmation`.

The diff exceeds the 400-line soft cap because this end-to-end UI-to-engine bridge includes separate async decision, Tauri payload, admission, and regression proofs. Splitting those proofs from the controls would leave the safety-critical vertical path unreviewable in one PR.

### Problem-derived contract

- A correct fix must carry the projected fire ID, state version, and prepared identity unchanged from the inbox through a guarded Tauri command to `automation.fire.decide`, with `confirmed` or `declined` as the engine decision.
- Only a fire projected as `awaiting_confirmation` with a valid prepared identity may offer a decision. One in-flight decision per fire prevents duplicate desktop submissions.
- Both success and stale/error outcomes refresh the inbox. A stale decision must be shown as a recoverable refresh, not reported as a successful provider action.
- The engine's atomic decision, dispatch, entitlement, and prepared-identity semantics must not change. The desktop must not directly submit provider work.

## Scope (this PR)

Ownership lane: automation-confirmation-inbox

Slice phase: operator-visible vertical proof

1. Add one admission-guarded Tauri bridge for the existing automation decision operation.
2. Render attachment-scoped confirm/decline controls only for a prepared, awaiting fire, and refresh the inbox after any decision outcome.
3. Add focused bridge and desktop decision tests for exact identity, confirm, decline, stale error, and double-click suppression.

### Files touched

- `desktop/src-tauri/src/engine.rs` - typed result, exact payload bridge, focused bridge test.
- `desktop/src-tauri/src/lib.rs` - admitted Tauri command and registration.
- `desktop/src/automationDecision.ts` - guarded desktop decision runner.
- `desktop/src/main.ts` - attachment controls and inbox refresh/status.
- `desktop/src/styles.css` - attachment-scoped decision layout.
- `desktop/test/automationDecision.test.ts` - decision and source-wiring regression tests.
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

Affected surfaces: desktop inbox attachment card, Tauri engine bridge, existing engine operation.

Risk areas: confirmation safety, stale state, double click, Tauri admission, response/error mapping.

Triggered reviewer rules: R1, R2, R3, R5, R6, R8, R9, R12, R13, R14.

Reachability proof: `inbox_query` renders an attachment fire in the desktop inbox, the operator clicks Confirm or Decline, Tauri invokes `automation.fire.decide`, and the refreshed inbox projection plus status reports the resulting state or stale refresh.

## Mechanism

The desktop decision runner checks the projected state and hash, reserves the fire ID synchronously, forwards the exact identity to an injected submit function, refreshes after either outcome, and releases its reservation. The Tauri command requires configuration admission and calls the existing engine operation under the mailbox operation gate. The UI never constructs a prepared identity or calls provider dispatch directly.

## Intentional

- The desktop identifies the rule by its existing ID and attachment name; this slice does not invent a new rule-description or action-summary contract.
- A successful confirm means the fire is authorized for pending dispatch, not that the provider action has completed.
- The engine's atomic stale/version/identity check is authoritative; the desktop guard only avoids invalid and duplicate local submissions.

## Deferred

Parking predicate: adjacent automation-management UI, presentation polish, and robustness that do not block this confirmation path or its safety. Parked hardening: none.

- A human-readable prepared-action summary or navigation to rule settings needs a separate approved projection and UX contract; this slice shows the existing rule ID and attachment filename.

## Verification

Local: `node --test --experimental-strip-types test/automationDecision.test.ts test/ntfyDisclosureMigration.test.ts` (13 passed); `pnpm build` (built); `pnpm build:sidecar` (`packaged-engine-smoke: ok`); `cargo test automation_decision_bridge_forwards_exact_prepared_identity --lib` (1 passed); `cargo fmt --manifest-path desktop/src-tauri/Cargo.toml --check` (passed); `uv run pytest -q tests/test_connect_v2_engine_api.py::test_automation_confirmation_binds_stable_preparation_and_admits_after_decision` (`. [100%]` after the new inbox assertions); `uv run ruff check tests/test_connect_v2_engine_api.py` (`All checks passed!`); `git diff --check` (passed). Browser fixture check could not run because Chrome lacked a usable sandbox and in-app browser control was unavailable; no bypass flag was used. Exact-head CI remains for PR publication.

## Estimated diff size

Final target: 9 files, about +465 / -8. Over the 400-line soft cap for the indivisible bridge and its regression evidence; exact staged count is checked before PR publication.
