# EW 278 — backend compatibility proof, first implementation slice

## Contract and root cause

This slice implements evidence under the [accepted EW278 contract](PR-Invoice-Legacy-Selection-278.md) and its [acceptance record](PR-Invoice-Legacy-Selection-278-Acceptance.md). It does not amend either. Baseline is merged main `9de50091ac7ba9ec080eff60daeb9a19182fc8fa`; installed connect-automate was verified at the locked `a6bbb347ae6a4cb30aeb531153ef2c33ee828fed`.

The gap here was invoice-specific backend proof, not a reproduced backend behavior defect. The existing decoder, exact matcher, versioned rule writer, terminal status persistence and retained output owners passed these cases. No product source was changed or introducing runtime-defect commit inferred.

## Required change surface and verification map

Only `tests/test_invoice_compatibility.py` and this evidence summary are added.

| Accepted invariant | Proof in this slice | Limit |
| --- | --- | --- |
| Selection identity | Full credential-free 1.0/2.0 snapshot round-trip, stale interactive refusal before mailbox/POST, provider version/instance changes, mixed-version catalog matching and duplicate exact-identity refusal for both versions | Automation-fire scheduling refusal is a later proof slice. |
| Explicit recovery | API rule creation/reselection through versioned rules.put, original selection retained across fresh interpreter reopen, stale expected-version refusal | API-only; no desktop rule editor claim, no concurrent-thread race claim. |
| Terminal provider errors | Real loopback HTTP 400 UNSUPPORTED_CAPABILITY and accepted-then-failed INTERRUPTED status through the shared client and existing engine owners; original request identity retained and repeated pump does not POST | Uses the reference server, not an installed Invoice Processor; terminal behavior across a process restart and full user-path recovery remain pending. |
| Historical output | Synthetic InvoiceRecord 1.1 bytes returned over HTTP, fresh-process retained request/result check, original capability identity, rejection of substituted 2.0 context, opaque presentation and exact export without discovery | No typed invoice adoption; certificate behavior unchanged. |

The HTTP fixture reuses ReferenceProvider's listener, registration, authentication helper, multipart parser and GET framing. Its test-only handler supplies scripted invoice responses; it is not a provider implementation or a new transport. The synthetic null-field legacy payload was independently validated against Invoice Processor `src/invoice_processor/schema.py` at `8e953b2`, schema blob `35ab9c8892d5a3d7a5e71fcf32e91ab72cb36e6d`. Production tests do not fetch or import that repository.

## Verification and origin discipline

- Initial adjacent baseline passed. The new selection tests passed before any runtime edit.
- The first HTTP attempt failed in the newly written test helper: `_prepare_or_create_generic_connect_job` returns a content callback, but the helper passed that callback as bytes. The helper now calls it before returning its data; the original HTTP cases pass. This was an uncommitted fixture defect, not a product failure or a weakened assertion.
- Focused plus adjacent run: `45 passed in 18.79s`, with no platform exclusions. Includes the new file, fixed-adoption and reference-provider tests, and existing stale-version, nonretryable refusal and output presentation/export regressions.
- Ruff check passes. No broad local suite duplicated CI; native/platform results remain CI gates.
- Mutation in an isolated source copy removed exact capability-version matching at `_discover_selected_generic_capability`. The stale selection test failed with “stale invoice selection reached mailbox or POST”; production source was untouched. This guards the invariant rather than merely echoing the implementation.

## Duplicate-match review regression

The coverage omission originated in proof commit `6549346be924c218db0d7de5aed50195a8ee9ebe`, in `test_mixed_catalog_matches_exact_saved_invoice_version` at `tests/test_invoice_compatibility.py:128`. Mixed versions prove exact selection but cannot exercise two entries with the same identity. The existing product owner, `engine_api.py:3183`, `_discover_selected_generic_capability`, already rejects ambiguity; its guard originated in `2c6eb559` and is unchanged.

Reproduction and isolation: in a separate source copy, changing only that owner's `len(matches) != 1` to `len(matches) == 0` left the original proof file green (`10 passed in 4.67s`). The added parameterized public-API regression supplies two exact identities for each of invoice 1.0 and 2.0, differing only in their display label. It requires `capability_unavailable`, forbids mailbox and client construction, and asserts no job was persisted. With the isolated mutation, both cases fail at the forbidden mailbox boundary (`2 failed in 1.10s`); with unchanged product source, the focused/adjacent run passes (`45 passed in 18.79s`, no exclusions), as do Ruff lint and format checks.

This closes the accepted duplicate-selection proof requirement without a new product rule, downstream patch, skip or weakened assertion. Wrong-protocol completed-output integrity remains in the next slice.

## Explicit non-scope, implementation summary and cold diff audit

This test-only slice changes no runtime owner, API, dependency, schema, retry policy or existing assertion. Synthetic stores and loopback HTTP only; no model, real mailbox, guest, installation, live settings or provider cutover. The new tests exercise existing owners and preserve original identities; no invoice-specific matcher or decoder is introduced. This summary links to the accepted contract instead of creating a competing rule.

## Gap audit

NOT DONE for EW278 as a whole: inbox refusal/reselection wording and render/action proof; saved automation-fire refusal/confirmation and repeated-pump behavior; remaining process-restart, integrity and concurrency controls; exact-head CI/review and final consumer receipt. This slice supplies backend evidence only. Any defect in the remaining paths must be reproduced and attributed before an owner fix. No installed/UI or full cutover qualification is claimed.
