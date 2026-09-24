# COI Watcher workflow plan

Status: proposed working plan, 2026-09-24. Owner: COI workflow lane.

This is the navigation and state log for the Certificate of Insurance (COI)
workflow. It does not replace the accepted
[`CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md`](CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md),
the provider's `CERTIFICATE-EXTRACT.md`, live code, CI, or installed evidence.
Update this file when a milestone is proven or the operator accepts a product
decision. Do not infer completion from a merged PR alone.

## Outcome and boundary

The accepted slice is: a selected retained email attachment matches a stored
Email Watcher rule; the existing Connect v2 dispatcher invokes a pinned
`certificate.extract` provider; Email Watcher validates its result and commits
one replay-safe certificate and its policy rows; the operator sees expiry and
uncertainty in the desktop Expiry Ledger. Invalid output must not create a
partial ledger. The operator can see source identity and review state without
the workflow claiming that coverage is active or verified.

The accepted slice does **not** send reminders, contact a broker, renew a
policy, verify coverage, make a compliance decision, or edit/approve ledger
rows. It also does not authorize a new customer-facing promise, setup flow, or
product label. Those need a separate operator-approved contract.

Contract-grounded flow:

1. Retained mailbox message and PDF -> stored generic rule and immutable fire.
2. Existing Connect v2 queue/dispatch -> selected `certificate.extract` job.
3. Provider result -> strict application validation -> atomic, idempotent local
   certificate/policy ledger settlement.
4. `certificate.expiry_ledger.list` -> desktop Expiry Ledger and review states.

## Milestones and evidence

| Milestone | Finish condition | Current state and evidence |
| --- | --- | --- |
| M0: contract | Provider and consumer agree on result schema, boundaries, and vertical acceptance proof. | Accepted consumer contract revision 2 dated 2026-09-20; provider contract is its named dependency. This establishes intent, not runtime proof. |
| M1: provider and consumer code | Provider exposes `certificate.extract`; Email Watcher dispatches, validates, stores, and lists the result with a desktop ledger. | Code merged: [Invoice Processor #63](https://github.com/canfieldjuan/invoice-processor/pull/63) at `7b52f566520df108be916a22c18433e5b9d62f24` and [Email Watcher #177](https://github.com/canfieldjuan/eom-email-watcher/pull/177) at `e336e0ebf38f413be4437ef5d6f3f2661dacfaaf`. Installed cross-app proof is not recorded here. |
| M2: operator inbox visibility | A person can see confirmation decisions and resulting automation outcomes on the attachment path. | Code merged: [Email Watcher #179](https://github.com/canfieldjuan/eom-email-watcher/pull/179) at `45853036d62c218651905510fc0b77e4e42fb585`; [#181](https://github.com/canfieldjuan/eom-email-watcher/pull/181) at `ac4829fd9b0ba9eb20cdb081af436618ed6795ee`. This is adjacent operator visibility, not proof that the COI flow ran end to end. |
| M3: integrated local proof | Exercise the accepted contract's eight acceptance items through the real Email Watcher engine and Connect v2 provider with a retained deterministic message/PDF. Record exact heads, commands, fixture, outcomes, and failures. | Pending. Merged source and focused tests are insufficient to mark this proven. First check existing test/runtime evidence; only build a missing seam if that proof exposes a blocker. |
| M4: installed/operator proof | On each platform claimed, install the actual builds, configure a selected rule/provider through the supported path, process a retained COI PDF, and observe the persisted ledger and desktop review state. Record versions, setup, logs, and artifacts. | Pending. Do not claim an installed workflow or cross-platform readiness from local source tests. |
| M5: product/release decision | Operator accepts the target customer-facing workflow, setup and review responsibilities, and any reminder/renewal behavior; release evidence supports the exact claims made. | Not decided. This plan does not silently add reminders or set a release date. |

## Definition of finished

**Accepted technical slice finished** means M3's real-entrypoint proof passes
against the accepted contract, including replay/concurrency, malformed-result
fail-closed behavior, missing/ambiguous dates, and visible desktop states. Any
unexercised acceptance item remains explicitly open. M1 and M2 are merged-code
milestones, not substitutes for M3.

**Installed COI workflow finished** means M4 passes for each platform we claim.
The operator can select the intended mailbox/rule/provider, run a real retained
COI attachment through the supported installed path, and inspect the resulting
ledger or a truthful review/failure outcome. This is a separate gate from
source-level integration.

**COI product/release finished** requires M5's explicit operator decisions and
release evidence. Reminders, renewal actions, coverage verification, and
compliance claims are **not** implicitly required or approved by this plan.
If the operator wants any of them in v1, first accept a separate product
contract and update these finish conditions.

## Current state log

- 2026-09-20: Consumer contract revision 2 accepted. Scope is extraction,
  durable expiry ledger, and review visibility, with no automatic reminders.
- 2026-09-22: Provider #63 and consumer #177 were merged. Their merge commits
  are recorded in M1; integrated installed operation remains unproven here.
- 2026-09-23: Inbox confirmation #179 and outcome #181 were merged. Their merge
  commits are recorded in M2; these do not close the COI acceptance proof.
- 2026-09-24: This proposed plan starts a single place for scope, milestones,
  evidence, and finish conditions. Next work is M3 evidence inventory and the
  smallest missing real-entrypoint proof, not another process-hardening slice.

## Update and resume rule

At each verified milestone, append one dated state-log line with exact source
heads, test/runtime evidence, and remaining gaps. Mark a milestone complete
only when its finish condition is demonstrated. Keep a newly found product
choice in M5 until the operator decides it; keep non-blocking hardening out of
the M3 proof path.

After session compaction, read this plan and the accepted consumer contract
before choosing the next slice. Reconcile their state against the current
checkout, owned PR state, and new evidence; do not redo a merged milestone or
treat this working log as more authoritative than code/runtime evidence.
