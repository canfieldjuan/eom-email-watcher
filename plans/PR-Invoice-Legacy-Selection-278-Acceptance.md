# EW 278 — acceptance record

Juan accepted [PR #315](https://github.com/canfieldjuan/eom-email-watcher/pull/315)
at exact revision `0cbc6d30608702350afcd5c79620cd2b725df8fc`.

Oversight relayed his decision verbatim in mailbox `5413de84` on
2026-10-30 at 22:29:43 UTC: **“accept 315 at 0cbc6d3”**.
The exact-head oversight delta PASS is [issuecomment-6534529051](https://github.com/canfieldjuan/eom-email-watcher/pull/315#issuecomment-6534529051),
also recorded in mailbox `63df4e71`.

The accepted document is [PR-Invoice-Legacy-Selection-278.md](PR-Invoice-Legacy-Selection-278.md),
SHA-256 `f528d69769fcafd8dea2811cc991bdb0eab696f6cb4a15f28fc044261b07813c`.
Its reviewed body remains byte-identical. This record supersedes only its
historical proposed/acceptance-pending status. It does not accept future edits
or change its behavior, scope, proof requirements or remaining gates.

The acceptance-record delta requires independent review. Contract merge retains
required green CI, exact-head connector review, no unresolved review threads,
clean local/remote parity and a guarded merge commit. Implementation follows
in separate small PRs under the accepted contract and requires its own review.
This commit records acceptance only and starts no implementation or execution.

The relay preserves synthetic fixtures and the loopback reference provider as
the proof surface: no model, real mailbox, guest, provider cutover or installation.
Windows measurement retains its own accepted gates. Neither lane borrows the
other's authority. Existing live-app, settings and watcher holds remain.

No source, test, dependency, workflow, configuration or generated file changes
are included. Code tests are not rerun for this documentation-only delta;
byte identity, the delta scope and whitespace are checked instead.
