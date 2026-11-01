# EW278 — terminal restart and deliberate replacement proof

## Contract and root cause

This slice applies the [accepted EW278 contract](PR-Invoice-Legacy-Selection-278.md) and its [acceptance](PR-Invoice-Legacy-Selection-278-Acceptance.md). Base: merged PR318, `45cb7769ad61beec6eeccbb1f8075410822d77ec`. Earlier backend and integrity proofs are complete; terminal behavior after actual process reopen and deliberate replacement remain unproven. No product defect is asserted.

## Required surface and verification map

Extend the existing invoice HTTP terminal test and fixture, plus this proof map. Use the existing reference listener and real client for both HTTP400/UNSUPPORTED_CAPABILITY and accepted-then-failed/INTERRUPTED. Preserve those original assertions.

- Fresh interpreter opens only the disposable configuration/store, pumps failed work repeatedly and replays the same public invocation. Full stored job/dispatch snapshots, original error and legacy request identity remain unchanged; no new discovery/client/mailbox access or POST. Stub only entitlement policy in the synthetic child, not the store, queue or request handlers; this is not license qualification.
- After catalog version changes, reusing the failed request for the new selection must conflict. A deliberately selected current capability and fresh request still require its own confirmation before attachment access or a job is created. Explicit confirmation then succeeds through real loopback HTTP. The original failed record remains byte-for-byte unchanged, and replaying it still refuses without another POST.
- Preserve adjacent retryable/uncertain-write controls. Isolated mutations remove the confirmation check at the preparation owner or the terminal replay branch at the invocation owner. The unconfirmed replacement must hit the forbidden mailbox probe; terminal replay must attempt forbidden registration discovery. Product checks are unchanged.
- Run the focused invoice file and directly adjacent terminal/confirmation/retry tests under umask022; Ruff check/format. Required platform CI and independent exact-head review follow publication.

## Non-scope and assumptions

No source, dependency, schema, migration, timeout, retry, entitlement or admission policy changes are planned. If a product defect is reproduced, identify its introducing commit and first owner before an in-scope fix. No real mailbox, model call, live settings/app, guest or installation. No new UI, invoice editor, automation lifecycle or concurrent-rule claims. Those are separate remaining proof slices.

## Implementation summary and actual verification

The existing parameterized HTTP failure test now exercises both outcomes through a real fresh interpreter, full job/dispatch snapshot comparison, repeated pumps and invocation replay, then deliberate current-version recovery. Its existing reference-provider fixture exposes the confirmation-required flag in its manifest so the replacement is held until explicit confirmation. Neither historical failures nor their original requests are changed. The 2.0 result is a scripted generic completion using opaque fixture bytes; this proves admission/recovery, not the provider's 2.0 record schema or typed adoption.

- Initial extended lifecycle cases: `2 passed in 2.99s`.
- Focused invoice file plus adjacent explicit confirmation, lost acknowledgement, provider-busy retry, authoritative accepted-job reconciliation and nonretryable refusal: `30 passed in 16.95s`, no platform exclusions, under umask 022. Ruff lint/format pass.
- Removing only the preparation confirmation guard (`engine_api.py:4763`, `_prepare_or_create_generic_connect_job`, existing owner from `2c6eb559`) causes both cases to hit the forbidden mailbox assertion (`2 failed in 3.10s`).
- Removing only the failed-request replay branch (`engine_api.py:5492`, `_connect_attachment_invoke`, existing owner from `87e4eed2`) initially survived (`2 passed in 3.39s`): the test forbade catalog discovery but omitted the existing registration reconciliation lookup. Inspection isolated that test-probe omission to `_discover_persisted_generic_capability`'s registered lookup. The completed probe covers both SDK lookup paths and records attempted access so API error wrapping cannot hide it. The same mutation then fails both cases on unexpected registration discovery (`2 failed in 2.91s`). The intermediate API error-code failure is also preserved in the packet. This was an uncommitted test-probe correction, not a product defect or an assertion relaxation.

The independent process uses the disposable configuration/store and synthetic entitlement stubs. Neither parent runtime objects nor parent mocks are inherited. No production source was edited for either mutation. No introducing runtime-defect commit is claimed.

## Cold diff audit

`tests/test_invoice_compatibility.py`: the existing fixture's manifest can require confirmation; the original terminal test is extended rather than copied. All original assertions remain. New assertions map to the accepted terminal-after-reopen and deliberate-fresh-replacement invariants. `plans/PR-Invoice-Terminal-Recovery-Proof.md`: records that map, actual results and limits. No product, dependency, API, schema or policy changes.

## Gap audit

DONE locally for this bounded terminal restart and API replacement proof. NOT DONE for exact-head CI/connector/oversight review and EW278 overall: reason-aware inbox wording/render-action proof, saved automation fire/confirmation binding, concurrent rule updates, remaining end-to-end consumer receipt. The API boolean confirmation here does not qualify stale saved-automation confirmations. No installed UI, actual Invoice Processor cutover, model output or license qualification is claimed. Publish separately and stop for review before advancing the next proof slice.
