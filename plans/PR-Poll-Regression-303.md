# Polling regression coverage — #303

## Root cause

The polling implementation in `7c12dded82ceccd6e3dbcacfc6bee677d85d4ca0`
added correct behavior without discriminating tests for all accepted clauses.
Owners: `service.py:2275` `_finish_active_result`, `:2594` `_check_active`,
`:2905–2965` `_poll_sent_folder`; `lib.rs:2398–2405` manual command completion;
`scheduler.rs:613` manual wake notification and `:889–896` non-pending test.
The old worker test could send before registration, and the non-pending twin
staged a deadline earlier than even an incorrectly accepted observation.

## Required change surface

Tests for #303 items 1–8 at the actual service owners, using existing provider
fixtures and disposable SQLite state; include normal/recovery and drained/pending
siblings. Extract the existing manual command completion body into one testable
owner, retaining delivery invocation, error propagation and scheduler forwarding
(item 9). Add a test-only wait-registration observation under the existing gate
mutex (item 10). Stage a later deadline for every non-pending control (item 11).
The behavior specification remains [C1–C5](PR-Poll-Backlog-145.md); no duplicate rule.

## Explicit non-scope

No behavior, schema, dependency, deadline, cadence, provider, configuration or UI
change. No install, live settings or promotion. No new runtime diagnostic/harness.
The waiter observation exists only under cfg(test). Keep #145 installed proof separate.

## Assumptions / blockers

Issue #303 is the accepted work order. An equivalent/redundant mutation will be
reported as such, never made artificially reachable or called a killed mutant.
In particular recovery_incomplete is set only during recovered Gmail previews:
recovered and the dry-run output guard independently force unknown.

## Verification plan

Reproduce the surviving mutants with the old tests, then prove the new focused
tests kill changed mapping, lost manual forwarding, missing wake notification and
non-pending admission mutants. Restore every mutant; preserve raw output outside
the worktree. Run adjacent Python service tests, Rust scheduler/manual command
regressions, Ruff, rustfmt and Clippy. CI owns duplicated broad suites. Keep
native/installed claims separate. Map assertions to C1 (state), C2 (deadline/wake
and manual/error behavior), C4 (preserved completion counts) and C5 (cursor safety).

## Implementation summary

Added owner-level service twins using existing IMAP/Gmail fixtures and real
SQLite cursors. Extended existing complete, backoff and truncated-preview Gmail
assertions. The service implementation itself is unchanged.

Moved the existing blocking manual-check completion body, unchanged, into
`run_manual_check`. The command invokes it with the original delivery closure;
admission, asynchronous execution, error mapping and queue wake remain in place.
Tests invoke that same owner with controlled delivery outcomes and inspect the
real scheduler deadline and returned counts. No second cadence rule was added.

Added a cfg(test)-only waiter-registration sender under the existing gate mutex.
The notifier must acquire that mutex, so the test knows notification occurs after
wait registration. The test always stops/joins its worker, including mutant failure.
The non-pending twin now stages a deadline later than even the incorrect idle
candidate and has a same-clock positive pending twin.

## Assertion-to-contract / issue map

| #303 item | Regression / assertion | Accepted clause |
|---|---|---|
| 1 | Inbox recovery twins: normal/recovered crossed with drained/pending; returned state, recovery call and saved cursor | C1, C5 |
| 2 | Existing truncated Gmail preview: incomplete/reason and unknown state; cursor and store remain unchanged | C1, C5 |
| 3 | Sent pending with drained Inbox versus both drained; provider call count and stored position | C1, C4 |
| 4 | Unavailable Sent with drained/pending Inbox; no Sent change request or position | C1 |
| 5 | Unknown Sent scope with drained/pending Inbox; unknown dominates | C1 |
| 6 | Failed initial position public path plus available-without-position direct poll-owner result and no provider call | C1, C5 |
| 7 | Sent recovery drained/pending crossed with drained/pending Inbox; unknown, one recovery, saved recovery cursor | C1, C5 |
| 8 | Existing complete/backoff Gmail paths retain default unknown; backoff error and cursor assertions preserved | C1, C5 |
| 9 | Real manual completion owner: delivery called once, pending deadline advances with/without delivery failures, returned check/counts unchanged; non-pending/error retain deadline and error | C2, C4, C5 |
| 10 | Registered waiter returns before its sleep slice only after manual notification; always joined | C2 |
| 11 | Non-pending observations cannot alter a later staged deadline, while the same-clock pending twin does | C2 |

## Reproduction and mutation proof

The original poll-state tests passed after corrupting the Sent pending mapping.
The original manual non-pending test passed after removing the admission guard.
These reproduce coverage gaps, not production defects. The new assertions kill
both mutants at the same owners.

Eight Python mutations are killed by state assertions: Inbox recovery, Sent
aggregation, Sent pending, unavailable Sent, unknown Sent, missing position,
Sent recovery and default result state. Three Rust mutations are killed: removing
manual observation, removing notification (registered waiter times out), and
removing non-pending admission (the deadline changes).

Removing only `recovery_incomplete` from the aggregate is **equivalent**, not a
killed mutant: the variable is true only in a recovered Gmail preview, so the
`recovered` clause and dry-run result guard independently force unknown. The real
truncated-preview test passes under that mutation; those guards remain unchanged.
No assertion is weakened and no runtime state is invented to claim a kill.

## Cold diff audit

- `tests/test_service.py`: new mapping fixtures call the real service, scope and
  poll owners. Existing Gmail paths gain state assertions; previous cursor,
  backoff and preview assertions remain. This covers C1/C4/C5 without mocking
  away the mapping being tested.
- `desktop/src-tauri/src/lib.rs`: unchanged completion body has one named owner,
  invoked by the existing command. Tests cover actual deadline mutation, exact
  returned check/counts and error propagation (C2/C4/C5). Admission and operation
  exclusion are untouched.
- `desktop/src-tauri/src/scheduler.rs`: production logic is unchanged. Only the
  cfg(test) registration observer and regression assertions are added/changed
  (C2). The observer does not notify, alter the deadline or bypass the real wait.
- This plan links to the canonical behavior contract and records the test-only
  work order, origin, limitations and evidence.

## Gap audit

NOT DONE — implementation and local proof complete; exact-head CI and independent
review remain. Local results: service tests under umask 022, 219 passed; native
scheduler tests, 23 passed; manual completion tests, 2 passed; Ruff, Clippy with
warnings denied, rustfmt and whitespace checks passed. No install or platform
latency claim. Broad duplicated suites are left to CI.

Initial verification errors were confined to new test scaffolding: overlong
lines and an unsupported Store.state keyword. The fixture now calls the existing
public read API; no runtime behavior or existing assertion was changed to pass.

