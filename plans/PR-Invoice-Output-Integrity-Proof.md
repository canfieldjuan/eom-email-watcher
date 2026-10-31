# EW278 — retained invoice output integrity proof

## Contract and root cause

This bounded slice applies the accepted [EW278 agreement](PR-Invoice-Legacy-Selection-278.md) and [acceptance record](PR-Invoice-Legacy-Selection-278-Acceptance.md). The first backend proof merged in PR316 at `69b98e958eebb7f5941831b9c27373aa8d4b3fbf`. Oversight explicitly deferred wrong-protocol completed-output coverage to this slice. The demonstrated gap is missing invoice-specific negative proof, not a reproduced product defect.

## Required change surface and verification plan

Extend the existing synthetic invoice test file and this proof map only. Reuse its real loopback reference provider, completed legacy invoice bytes and isolated store. No new transport, dependency or harness is needed.

- Retained replay: a valid completed result is decoded with identical bytes after fresh-process reopen. SQLite rejects persisting a wrong protocol, noncompleted status or missing result on the completed record; each rollback leaves the original record intact. Altered in-memory records independently refuse at `Store.completed_connect_outputs` (`db.py:11299`), isolating its reader guard from the schema and API checks.
- Byte integrity: altered payload bytes, declared hash, size and malformed encoding refuse through the existing result decoder, including after fresh-process reopen.
- Output admission: presentation and export refuse persisted byte corruption and mismatched message, part, job or artifact identity; no export file is created. Schema-invalid record shapes are tested at the schema and direct reader boundaries, not claimed as reachable persisted API states. No provider rediscovery, mailbox access or client construction is allowed after the fixture has completed.
- Preserve request/result bytes and job state across refused reads. Keep the existing positive legacy replay/export test as the permissive-side control.
- Isolated mutation: remove only the protocol check at the store owner. The wrong-protocol regression must fail at that owner; the later API guard cannot stand in for this proof.
- Run the focused file, adjacent retained-output tests and applicable Ruff checks under umask 022. Required platform CI and independent exact-head review remain pending until publication.

## Non-scope and assumptions

No runtime source change is planned. If a failure proves a product defect, reproduce and attribute its introducing commit and owner before repair. No changes to schema, APIs, dependencies, source admission, retention, retries, entitlement or certificate behavior. No live app, settings, mailbox, model, guest or installation. UI recovery, automation, lifecycle restart and concurrency are separate remaining slices.

## Implementation summary and actual verification

Only the existing test file and this proof map change. No product defect was reproduced. The unchanged store protocol guard originated in `ba567790`; no introducing runtime-defect commit is claimed. The new completed-invoice fixture reuses the earlier real loopback fixture, then forbids discovery, mailbox and client access. The fresh interpreter helper validates output bytes and metadata through the actual store reader, not a cached Python object.

The initial test attempted to persist schema-invalid record shapes and failed at SQLite CHECK constraints (`3 failed, 8 passed, 12 deselected in 8.55s`). This was an uncommitted test setup mistake. The final tests assert those stronger schema refusals, original-record preservation and valid reopen, then independently exercise the reader with altered records. No constraints were disabled or assertions skipped. The persisted corruption cases cover same-size altered payload, incorrect hash, incorrect size and invalid base64. The identity cases vary message, part, job and artifact selectors independently.

- Focused invoice file plus adjacent store/output tests under umask 022: `28 passed in 15.49s`, no platform exclusions.
- Ruff initially found nested-context formatting. After that syntax-only correction, the affected shape cases passed again (`3 passed in 2.49s`), with Ruff check and format clean. Other test bodies are unchanged from the focused run.
- Isolated source-copy mutation removed only `or job.protocol_version != 2` at `Store.completed_connect_outputs`. The protocol case failed with `DID NOT RAISE <class 'RuntimeError'>` (`1 failed, 2 deselected in 1.15s`). The working product source was never modified. This proves the required wrong-protocol test reaches the owner rather than passing on a later guard.

## Cold diff audit

`tests/test_invoice_compatibility.py`: adds a completed-result fixture, fresh-process output reader and parameterized negative controls; existing positive replay tests and assertions remain intact. These map to the accepted replay/integrity requirements. `plans/PR-Invoice-Output-Integrity-Proof.md`: records this mapping, actual receipts, initial fixture failure and limits. No schema, source, dependency or public contract changes.

## Gap audit

DONE locally for this bounded retained-output integrity proof. NOT DONE for publication gates and EW278 overall: exact-head CI/connector/oversight review; inbox reason/reselection render-action proof; saved automation and confirmation; remaining lifecycle-restart and concurrency proof; final consumer receipt. No installed/UI or complete cutover qualification is claimed. Stop after publishing this slice for review; no merge or next-slice work in this round.
