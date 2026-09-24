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

## Real-document proof first

Start M3 with operator-supplied COIs kept outside Git: one text-bearing PDF
that reports encryption and a structural warning, and one image-only scanned
PDF. Local profiling found one page in each. These are separate admission and
extraction cases, not interchangeable fixtures. A deterministic fixture still
supports repeatable replay, concurrency, and malformed-result regression
tests, but fixture-only green tests cannot close M3.

Before a provider invocation, confirm where the document bytes and extracted
content go; do not send either COI to an unapproved remote model or service.
For each real input, record the exact local file identity in ignored session
state, the selected runtime/provider versions, the actual entrypoint and
result. Compare any completed record's fields and provenance to a private
human-checked reference; compare an unreadable document to its expected error
contract. Publish only non-identifying pass/fail and gap summaries. Never
commit the PDFs, extracted text, customer identifiers, policy numbers, or raw
model output to this repo.

Run the text-bearing COI and the scan through the supported path and record
whether each yields a complete ledger result, a review result, or an explicit
failure. Do not silently treat an image-only scan as a successful text input;
if OCR or another admission step is missing, identify that boundary and keep
the real-world scanned case open. No result establishes active coverage or
policy validity without separate verification outside this contract.

## Milestones and evidence

| Milestone | Finish condition | Current state and evidence |
| --- | --- | --- |
| M0: contract | Provider and consumer agree on result schema, boundaries, and vertical acceptance proof. | Accepted consumer contract revision 2 dated 2026-09-20; provider contract is its named dependency. This establishes intent, not runtime proof. |
| M1: provider and consumer code | Provider exposes `certificate.extract`; Email Watcher dispatches, validates, stores, and lists the result with a desktop ledger. | Code merged: [Invoice Processor #63](https://github.com/canfieldjuan/invoice-processor/pull/63) at `7b52f566520df108be916a22c18433e5b9d62f24` and [Email Watcher #177](https://github.com/canfieldjuan/eom-email-watcher/pull/177) at `e336e0ebf38f413be4437ef5d6f3f2661dacfaaf`. Installed cross-app proof is not recorded here. |
| M2: operator inbox visibility | A person can see confirmation decisions and resulting automation outcomes on the attachment path. | Code merged: [Email Watcher #179](https://github.com/canfieldjuan/eom-email-watcher/pull/179) at `45853036d62c218651905510fc0b77e4e42fb585`; [#181](https://github.com/canfieldjuan/eom-email-watcher/pull/181) at `ac4829fd9b0ba9eb20cdb081af436618ed6795ee`. This is adjacent operator visibility, not proof that the COI flow ran end to end. |
| M3: integrated local proof | Begin with both private real COIs through the supported Email Watcher and Connect v2 entrypoint, then use deterministic fixtures for the accepted contract's eight repeatable acceptance items. Record exact heads, commands, outcomes, and failures without publishing document content. | Pending. Parser-only checks admitted the text-bearing encrypted PDF and rejected the image-only scan with `NO_NATIVE_TEXT`. An extraction-only local-model attempt on the former ended `MODEL_RUNTIME_UNAVAILABLE`; neither input has completed a Connect job or reached the ledger. |
| M4: installed/operator proof | On each platform claimed, install the actual builds, configure a selected rule/provider through the supported path, process a retained COI PDF, and observe the persisted ledger and desktop review state. Record versions, setup, logs, and artifacts. | Pending. Do not claim an installed workflow or cross-platform readiness from local source tests. |
| M5: product/release decision | Operator accepts the target customer-facing workflow, setup and review responsibilities, and any reminder/renewal behavior; release evidence supports the exact claims made. | Not decided. This plan does not silently add reminders or set a release date. |

## Definition of finished

**Accepted technical slice finished** means M3's real-entrypoint proof passes
against the accepted contract, including replay/concurrency, malformed-result
fail-closed behavior, missing/ambiguous dates, and visible desktop states.
Both real COIs must have an observed outcome checked against the accepted
contract: field/provenance comparison for a completed record, or the expected
typed error for an unreadable document. An image-only scan may satisfy the
accepted failure contract while scanned-COI extraction remains an open product
coverage gap; do not call that gap a successful extraction. Any unexercised
acceptance item remains explicitly open. M1 and M2 are merged-code milestones,
not substitutes for M3.

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
- 2026-09-24: The operator supplied two private real COIs for M3. Local
  profiling found a text-bearing encrypted PDF and an image-only scan. The
  current provider parser admitted the encrypted PDF despite the accepted
  provider contract's encrypted-input rejection rule; it rejected the scan
  with `NO_NATIVE_TEXT`. An extraction-only attempt on the first PDF ended
  `MODEL_RUNTIME_UNAVAILABLE` while Ollama had mostly CPU model placement.
  Neither input has completed the real Connect or Email Watcher path. Resolve
  the contract/code mismatch and runtime capacity before claiming a real COI
  success; keep the scanned case open as a separate OCR/admission boundary.

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
