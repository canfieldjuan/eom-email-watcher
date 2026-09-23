# Automation Outcome In Inbox

## Why this slice exists

After PR #179, an operator can confirm a prepared automation from an inbox attachment, but the desktop only renders automation fires while they await confirmation. `inbox.query` already projects later fire state, reason, and linked job ID. The confirmed action can therefore move through dispatch and settle without an operator-visible outcome on that attachment. The operator asked to continue this vertical path, and issue #147 tracks missing Connect completion/failure visibility. This slice is a narrow inbox outcome proof, not a new notification channel or a claim that Automate is release-ready.

The operator approved continuing the proposed confirmation-to-outcome visibility slice on the existing attachment in the current session. This does not authorize a new alert, rule editor, or provider-specific error copy.

### Problem-derived contract

- Root cause: `renderInbox` skips non-`awaiting_confirmation` fire projections, so durable queue and terminal states produced by the engine are invisible in the attachment UI after the decision refresh.
- Correct fix must touch/change: map each projected fire state to a truthful, bounded, non-sensitive status in the existing attachment card; preserve the confirmation controls only for prepared awaiting fires; prove the real inbox query projects a terminal fire and the desktop consumes that projection. Test pending, submitted, completed, failed, declined, paused, manual-review, source-unavailable, and unknown values without rendering raw reason text.
- Must not change: engine fire/Connect job transitions, rule CRUD, provider dispatch, notification delivery, entitlement semantics, schema, existing capability result rendering, or other inbox controls. No new alert or background poll.

## Scope (this PR)

Ownership lane: automation-outcome-inbox
Slice phase: operator-visible vertical proof

1. Present projected automation lifecycle state on its existing inbox attachment after a decision, with safe fallback for unknown states and reasons.
2. Preserve confirm/decline admission and the refresh-required fence for awaiting fires.
3. Prove the real `inbox.query` projection for settled fires and the desktop source wiring that consumes it, with focused negative and boundary tests.

### Review Contract

Acceptance criteria:

1. `inbox.query` returns the linked fire's settled state and job ID on its source attachment; a focused engine API test drives a real fire through the existing queue and checks the response, without changing the engine path.
2. The desktop render loop appends one lifecycle status per projected fire on the same attachment, while only a prepared `awaiting_confirmation` fire offers Confirm and Decline; focused source-wiring and decision-admission tests check both paths.
3. The status mapping distinguishes queued, submitted, completed, failed, declined, paused, manual-review, and source-unavailable states without asserting provider completion before the fire is terminal; focused tests check each state and an unknown state.
4. Arbitrary `reason` and `job_id` values never become raw HTML or unapproved user-facing text; the status mapper accepts only the fire state and emits authored literals, defaulting to a generic status for unknown values. Tests include malformed and unknown states, and source wiring verifies the raw reason and job ID are not passed to the mapper or inserted into the DOM.
5. Existing refreshes after decision and `watcher://connect-queue` still update the mounted inbox; source wiring and a focused runtime projection test settle the path.

Reachability proof: an attachment fire is confirmed through `automation.fire.decide`, dispatched, settled, and returned by `inbox.query` on that attachment; the desktop render loop consumes each returned fire state through the tested state-only mapper. A live desktop/provider exercise is not claimed.

Affected surfaces: desktop inbox attachment rendering, a pure outcome-status mapper, focused desktop tests, and focused engine API assertions on existing projection tests.

Risk areas: misleading status, raw error disclosure, stale projection, multiple fires on one attachment, and confirmation-control regression.

Reviewer rules triggered: R1, R2, R3, R5, R6, R9, R10, R13, R14.

### Closure Declaration

- Fire-state vocabulary: CLOSED in `src/eom_email_watcher/db.py`'s `AUTOMATION_FIRE_STATES`. The desktop mapper enumerates all currently known states, and any future/unknown state receives a generic status that does not claim completion or failure. The source is the engine projection returned by `inbox.query`, and the out-of-set direction is the safe generic status.
- Reason vocabulary: OPEN. `reason` can reflect a Connect job error code, so a list of seen codes is not complete. This slice does not render or interpret reason at all. The state-only mapper emits authored literals, so missing, malformed, ambiguous, and unrecognized reasons cannot disclose provider data or invent a diagnosis.
- Projection fields and cardinality: `InboxAttachment.automation_fires` and `AutomationFireProjection` in `desktop/src/main.ts`, sourced from `db.py`'s `recent()` attachment projection, bound the fields. The render loop processes each fire separately; empty and multiple-fire attachments are admitted. Unknown or missing fields do not grant decision controls and do not become raw HTML.

### Boundary-change enumeration

- Boundary path/seam: the new status mapper accepts only the engine-projected state; the existing decision admission guard is unchanged.
- Replaced-path behaviors: previously non-awaiting fires were skipped; now they receive status-only rendering.
- Guard-relevant fields: only `state` reaches the status mapper; `reason` and `job_id` stay out of presentation. Known states select specific text, and unknown values use a generic fallback.
- Caller x input shape: one or several fires per attachment, known/unknown/malformed state, reason absent/present/malformed, job ID absent/present.

### Deployed-config probing

- Deployed/default config values: N/A; the status mapper has no configuration.
- Explicit value probe: test each currently known terminal and nonterminal state.
- Absent value probe: reason and job ID are not passed to the mapper or inserted into the status DOM node.
- Default-session/default-context probe: test unknown and malformed state fallback.
- Side-effect ordering: status rendering is read-only; decision and queue side effects remain in their existing paths.

### Files touched

- `desktop/src/automationOutcome.ts`
- `desktop/src/main.ts`
- `desktop/src/styles.css`
- `desktop/test/automationDecision.test.ts`
- `desktop/test/automationOutcome.test.ts`
- `plans/PR-Automation-Outcome-Inbox.md`
- `tests/test_connect_v2_engine_api.py`

## Mechanism

The inbox already receives every fire row with its state, reason, and job ID. A pure mapper will turn the closed fire-state set into safe status text and never interpolate raw reason or job ID. `renderInbox` will render that status in the existing attachment card. It will still offer the existing decision controls only to a prepared awaiting fire and will retain the current refresh-required fence. No engine or queue code changes are expected.

## Intentional

- This slice observes durable state already produced by the engine. It does not claim that a queued or submitted job has completed.
- No new ntfy, email, operating-system notification, polling timer, or rule editor is introduced.
- No raw provider error or arbitrary reason string is shown. Unknown values get a generic safe status.

## Deferred

Parking predicate: queue capacity/deadline tuning, cancel/retry controls, rule authoring, notification channels, and presentation polish not required for this inbox outcome proof are parked by default.

- Issue #147's deadline, depth, cancel, and external notification concerns remain separate.
- A rule editor and a human-readable prepared-action summary require their own approved product-shape contract.
- Provider-specific failure reasons and any further customer-facing status vocabulary require separate product-shape approval.

Parked hardening: none.

## Verification

- Fail-first: `node --test --experimental-strip-types --test-name-pattern='inbox renders attachment-scoped decisions' desktop/test/automationDecision.test.ts` failed as expected because no outcome status was wired before the non-awaiting skip.
- `node --test --experimental-strip-types desktop/test/automationDecision.test.ts desktop/test/automationOutcome.test.ts` passed 14 tests.
- `uv run --locked pytest -q tests/test_connect_v2_engine_api.py::test_automation_confirmation_binds_stable_preparation_and_admits_after_decision tests/test_connect_v2_engine_api.py::test_inbox_keeps_completed_automation_result_when_newer_retry_fails tests/test_connect_v2_engine_api.py::test_capability_effect_authority_drift_fails_before_provider_post` passed all selected tests.
- `uv run --locked ruff check tests/test_connect_v2_engine_api.py` passed.
- `pnpm build` from `desktop/` passed TypeScript and Vite build.
- Pending: plan sync check, cold diff audit, local PR review, exact-head CI and review after publication.
- A live provider/installed-app proof is not claimed by synthetic local tests and remains a separate acceptance step.

## Estimated diff size

| File | LOC |
|---|---:|
| `desktop/src/automationOutcome.ts` | 25 |
| `desktop/src/main.ts` | 7 |
| `desktop/src/styles.css` | 10 |
| `desktop/test/automationDecision.test.ts` | 4 |
| `desktop/test/automationOutcome.test.ts` | 21 |
| `plans/PR-Automation-Outcome-Inbox.md` | 114 |
| `tests/test_connect_v2_engine_api.py` | 37 |
| **Total** | **218** |
