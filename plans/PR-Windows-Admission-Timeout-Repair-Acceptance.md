# Windows admission timeout contract — acceptance record

Juan accepted the contract in PR #312 at exact revision
`230812cd5e30da1135a7342a46210e8555d65cd5`.

Oversight relayed the operator decision verbatim in mailbox `e7b7ab16`
on 2026-10-30 at 17:03:59 UTC, citing Juan's oversight-chat statement
at 17:03 UTC: **“accept 312 at 230812c”**.

The accepted document is [PR-Windows-Admission-Timeout-Repair.md](PR-Windows-Admission-Timeout-Repair.md),
SHA-256 `7a36db8d408fa4538f1df0f95a4de25165b9953373e524c060b52fadfabe4e4c`.
Its reviewed body remains byte-identical. This separate record supersedes only
its historical proposed/acceptance-pending status; it does not amend its scope,
requirements or outstanding implementation and qualification gates.

Oversight's contract PASS is recorded in mailbox `3f74412c`. Its nonblocking
child-internal timing suggestion is not part of the accepted contract. Adopting
that suggestion requires a separately reviewed amendment and Juan's acceptance.

This acceptance-record delta requires review and green required CI before the
guarded contract merge. Measurement follows in a separate PR within the accepted
bounds. PR #311 remains diagnostic-only; its permanent merge is not authorized.
Disposable guest only; Juan's live app, settings and services remain untouched.

No implementation, test, dependency, workflow, configuration or generated file
changes are included in this acceptance record. Code tests are not rerun for
this documentation-only delta; no new runtime verification is claimed.
