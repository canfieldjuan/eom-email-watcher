# COI Watcher workflow plan

Status: working plan, 2026-09-24; updated 2026-10-05. Owner: COI
workflow lane.

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

### Accepted input baseline (operator decision, 2026-10-04)

The operator approved the recovered encrypted and scanned candidate PDFs as the M3 test
baseline. Their current private hashes and retained supported-entrypoint outcomes identify
that baseline. Historical byte-for-byte continuity is not established and is not claimed.
This decision replaces the requirement to recover authoritative identities for the original
historical pair; it does not waive the expected-error checks, approved ordinary real-model
comparison, repeatable acceptance items, or separate M4 installed proof.

## Milestones and evidence

| Milestone | Finish condition | Current state and evidence |
| --- | --- | --- |
| M0: contract | Provider and consumer agree on result schema, boundaries, and vertical acceptance proof. | Accepted consumer contract revision 2 dated 2026-09-20; provider contract is its named dependency. This establishes intent, not runtime proof. |
| M1: provider and consumer code | Provider exposes `certificate.extract`; Email Watcher dispatches, validates, stores, and lists the result with a desktop ledger. | Code merged: [Invoice Processor #63](https://github.com/canfieldjuan/invoice-processor/pull/63) at `7b52f566520df108be916a22c18433e5b9d62f24` and [Email Watcher #177](https://github.com/canfieldjuan/eom-email-watcher/pull/177) at `e336e0ebf38f413be4437ef5d6f3f2661dacfaaf`. Installed cross-app proof is not recorded here. |
| M2: operator inbox visibility | A person can see confirmation decisions and resulting automation outcomes on the attachment path. | Code merged: [Email Watcher #179](https://github.com/canfieldjuan/eom-email-watcher/pull/179) at `45853036d62c218651905510fc0b77e4e42fb585`; [#181](https://github.com/canfieldjuan/eom-email-watcher/pull/181) at `ac4829fd9b0ba9eb20cdb081af436618ed6795ee`. This is adjacent operator visibility, not proof that the COI flow ran end to end. |
| M3: integrated local proof | Begin with both private real COIs from the accepted input baseline above through the supported Email Watcher and Connect v2 entrypoint, then use deterministic fixtures for the accepted contract's eight repeatable acceptance items. Record exact heads, commands, outcomes, and failures without publishing document content. | Complete under the accepted baseline. [Proof PR187](https://github.com/canfieldjuan/eom-email-watcher/pull/187) merged at `8d832259583b5e59f507fe5cc706e9f8d86f0a59` after exact-head review, four green checks and thread reconciliation. The approved ordinary real-model comparison and repeatable evidence remain retained; corrected terminal replay and rendering receipts are identified in the 2026-10-05 entry below. Historical byte continuity remains unproven. |
| M4: installed/operator proof | On each platform claimed, install the actual builds, configure a selected rule/provider through the supported path, process a retained COI PDF, and observe the persisted ledger and desktop review state. Record versions, setup, logs, and artifacts. | Linux proof passed on 2026-10-05: actual installed apps processed the approved COI from a real Gmail message, persisted one policy row matching the approved reference, and displayed Expired / Extracted / Available before and after desktop restart. Isolated application state; no Windows/macOS, reboot or unattended-operation claim. Receipt and exact package sources are in the dated entry below. |
| M5: product/release decision | Operator accepts the target customer-facing workflow, setup and review responsibilities, and any reminder/renewal behavior; release evidence supports the exact claims made. | Desktop COI setup for a Linux operator pilot accepted on 2026-10-05 in issue #192. Implementation and installed setup proof pending; public release and broader product behavior remain undecided. |

## Definition of finished

**Accepted technical slice finished** means M3's real-entrypoint proof passes
against the accepted contract, including replay/concurrency, malformed-result
fail-closed behavior, missing/ambiguous dates, and visible desktop states.
Every real COI input must have an observed outcome checked against the accepted
contract: field/provenance comparison for a completed record, or the expected
typed error for an unreadable document. At least one real COI must complete a
ledger record whose fields and provenance match a private human-checked
reference; expected failures add coverage but cannot close M3 on their own.
An image-only scan may satisfy the
accepted failure contract while scanned-COI extraction remains an open product
coverage gap; do not call that gap a successful extraction. Any unexercised
acceptance item remains explicitly open. M1 and M2 are merged-code milestones,
not substitutes for M3.

**Installed COI workflow finished** means M4 passes for each platform we claim.
The operator can select the intended mailbox/rule/provider, run a real retained
COI attachment through the supported installed path, and inspect at least one
successfully persisted ledger result with its desktop review state. Truthful
review or failure outcomes for other inputs are additional coverage, not a
substitute. This is a separate gate from source-level integration.

**COI product/release finished** requires M5's explicit operator decisions and
release evidence. Reminders, renewal actions, coverage verification, and
compliance claims are **not** implicitly required or approved by this plan.
If the operator wants any of them in v1, first accept a separate product
contract and update these finish conditions.

## Accepted M5 setup slice (2026-10-05)

The operator approved [issue #192](https://github.com/canfieldjuan/eom-email-watcher/issues/192)
with "I approve". The next slice puts COI rule setup in the existing Expiry Ledger
view for a Linux operator pilot:

- Select one connected mailbox, one already watched exact sender, an optional
  subject filter and one discovered compatible `certificate.extract` provider.
  Rules match PDF attachments; mailbox/sender scope is never silently broadened.
- Explain that saving creates an enabled rule. Confirmation for each matching
  attachment is on by default; the operator may explicitly choose automatic
  extraction. Existing inbox confirmation and provider effect gates still apply.
- Inspect, edit, pause and resume saved COI rules through the existing versioned
  generic operations. Stale edits require refresh. A missing/changed pinned provider
  is visible and is never replaced automatically.
- Keep certificate rows read-only. The operator checks values against the source
  and handles uncertainty/unreadable files manually. Extraction and expiry labels
  do not establish human approval, active coverage or compliance.

The consumer contract's **M5 desktop setup** section owns implementation and proof
requirements. This acceptance does not authorize public release, Windows/macOS
claims, reminders, renewal/broker contact, ledger correction, OCR or model changes.
Another outbound proof email requires separate explicit approval.

## Current state log

- 2026-10-05, M3 review closure and installed Linux M4 proof:
  - PR187's reviewed head was `e482914f421ea7cb663a309e7b95fdeee194adf3`;
    all four CI checks passed and no unresolved threads or change requests remained
    before merge. A claimed failed-replay provider-identity bypass was contradicted
    through the real HTTP decoder; alternate Git-environment invocation hardening
    is explicitly deferred to [issue190](https://github.com/canfieldjuan/eom-email-watcher/issues/190).
    Final M3 evidence alias `acceptance-evidence-v8.json`, SHA-256
    `1f93ddea01735b06601bfe2effeedf5e7801acf0ade268fa306f37d0165e126f`,
    retains actual terminal replay and complete rendering receipts. Older queue-only
    replay claims are superseded, not relabeled as executed terminal replays.
  - The operator explicitly approved one prepared self-addressed test email carrying
    the unchanged approved real COI. Installed Watcher operations configured the
    mailbox baseline, watchlist and certificate rule, then retrieved and analyzed that
    actual Gmail message. The installed provider completed one certificate job and fire;
    the ledger persisted one policy row. No mailbox state, analysis, extraction or
    rendering result was substituted for this run.
  - Installed package versions were Email Watcher `0.1.0` and Invoice Processor `0.1.0`.
    Package source heads were respectively `4a4642408024539690e8821ac1c890f5deee3562`
    and `85ae9de619d545fbf5f09fb82188666c7cbc8273`. Watcher production/build inputs
    were unchanged between its package head and PR187's reviewed head; the intervening
    corrections affected proof tools/tests. The frozen `qwen35-9b` runtime used its
    approved launch profile with context 32768 and one slot.
  - All twelve reference/input/completion checks passed. Comparison included page,
    bounding box, exact text and token extents; only unstable provenance span IDs were
    excluded. The actual installed Tauri window showed one row with Expired, Extracted
    and Available states before and after a real desktop process restart, and the
    installed ledger API response was identical. Screenshots and raw outputs remain
    private. Owned desktop, provider and model processes were stopped afterward.
  - Durable evidence alias `installed-workflow-receipt.json`, SHA-256
    `2b996b1c4ba7ff365c9ae491f31da75a834880eb9d0f3e0a83a45e91bbb3f77f`,
    binds package receipts, private output/screenshot hashes, reference comparison,
    observed states and limits. Alias `package-source-equivalence.json`, SHA-256
    `af536b2bf24c829b3a2d96c3b3042998f413e69954692f517de8165f091dd4f9`,
    records the production/build-input comparison. M5 product/release decisions remain
    open; this proof does not enable reminders, renewal actions or compliance claims.

- 2026-10-04, operator baseline decision: following the explicit recommendation to accept
  the recovered PDFs while preserving the missing historical-hash limitation, the operator
  replied "lets do it". The Accepted input baseline section above is now the authoritative
  finish-condition adjustment; issue #188 records the decision. No runtime evidence is
  relabeled or rerun by this approval. PR187 has new proof-tool findings; M3 review remains
  pending while the installed Linux M4 preparation begins.

- 2026-10-04, identity-gate correction (before operator baseline decision): my recovery entry below promoted matching historical
  paths and metadata into proof of original-file continuity. It cannot establish that
  continuity without authoritative historical identity evidence. The recovered candidate
  runs and their current hashes remain valid supplemental evidence; M3 stays **OPEN**.
  No finish condition is relaxed. The separate encryption finding is contradicted by the
  merged provider source and its readable-encrypted-PDF regression, linked in the corrected
  audit below. Proof-tool identity, authority, evidence-location and projection corrections
  are committed in PR187 at `c3296c75a62f3d7d6b8bcbb35cbea73e8a26f9ab`: focused proof
  and authority tests passed, and fresh generated ordinary/encrypted/scanned integration
  runs passed with pinned provider instance/versions, approved release authority and full
  projection identity checks. Current correction manifest: alias
  `coi-m3/acceptance-evidence-v5.json`, SHA256
  `414c2bfefabb887cfc410cf2e7a7e4ab980835369d93fc78221b006ef4989f45`.
  The remaining identity decision is tracked in
  [issue #188](https://github.com/canfieldjuan/eom-email-watcher/issues/188).

- 2026-10-04, candidate-input recovery and proof (completion claim corrected above): earlier
  session records named the native and scanned COI paths. Both files were still present; filenames, byte sizes, timestamps
  and native-text profiles matched the historical records. Private copies and current
  SHA256 identities are retained with the recovery trace. No historical digest was retained,
  so byte-for-byte continuity with the earlier profiling cannot be independently established.
  Both recovered real inputs traversed real discovery, HTTP `certificate.extract`, native
  admission, consumer settlement, repeated pump and a new consumer process on consumer
  `ba08c37136e291dcdb8df114cd08ea65ae41e4bc` and provider
  `85ae9de619d545fbf5f09fb82188666c7cbc8273`. Each returned `DOCUMENT_UNREADABLE`, produced
  zero ledger rows and passed all seven integration/replay/restart checks. Mailbox retrieval
  was staged and the deterministic fixture model boundary declared; these are unreadable-input
  admission proofs, not extraction successes. The approved ordinary real-model/reference
  proof remains at its recorded revision, with unchanged application/extraction source.
  Historical evidence chain (completion status superseded above): alias
  `coi-m3/acceptance-evidence-v4.json`, SHA256
  `b3f7eb3b169ab5440cbdc92c7e22fee641d50b7e7479e5c8c5ad13c705f7db83`.
  Evidence covers the recovered candidates, approved ordinary COI and repeatable cases.
  The original-file identity gate, PR review and merge remain open. Scanned extraction, installed
  proof and product/release decisions remain separate gaps. Owned proof processes are stopped.

- 2026-10-04, review correction (before original-input recovery): my preceding completion
  statement accounted for the
  approved ordinary COI and generated fault fixtures but omitted the two original private
  inputs required above. At this checkpoint the operator could not locate them, and no retained
  supported-entrypoint outcomes identified those exact files. Their checks were **unverified**;
  M3 remains **open**; the recovery above does not establish historical content continuity.
  The generated cases supplement them and do not waive or replace this gate.
  Recover authoritative original identities before closing this gate; candidate runs alone
  do not authorize substituting another COI.
  The approved ordinary comparison and existing repeatable evidence remain retained at their
  recorded revisions.
  The scan-error wording is also corrected at its source: the
  [provider input contract](https://github.com/canfieldjuan/invoice-processor/blob/85ae9de619d545fbf5f09fb82188666c7cbc8273/docs/contracts/CERTIFICATE-EXTRACT.md#L49-L53)
  defines public `DOCUMENT_UNREADABLE`, and the
  [certificate Connect error boundary](https://github.com/canfieldjuan/invoice-processor/blob/85ae9de619d545fbf5f09fb82188666c7cbc8273/src/invoice_processor/connect/service.py#L477-L496)
  maps internal `NO_NATIVE_TEXT` to it. The generated scan result matches that contract;
  this correction does not change provider behavior or add certificate OCR.

- 2026-10-04, final reference approval: the approved ordinary real COI completed the real
  provider/consumer path on consumer `144cadaf8c0e7aed10fbb4af6604d4211e510104` and
  provider `85ae9de619d545fbf5f09fb82188666c7cbc8273`, using frozen local model
  `qwen35-9b` on runtime build `b1-c1d0e7a`. Persistence, repeated pump and a new consumer
  process preserved the expected record. The operator confirmed one full printed coverage
  heading that the earlier reference retained only as its first line. A reference derived
  from the approved PDF's native catalog then matched all delivered fields, policy rows,
  page/bbox/exact source text and token ranges; only parser-generated span IDs are excluded
  from the older-reference comparison. The earlier reference and failed attempts remain
  retained. Final eight-item map and private evidence digests: alias
  `coi-m3/acceptance-evidence-v2.json`, SHA256
  `e6b3b5c5d16198af90a8e4cbdd354fc5b165f2241d1f3e0dc308ec6728ab149f`.
  Proof PR187 is ready for review. Application code and the frozen model profile are
  unchanged by this reference approval. At this checkpoint the ordinary-input and repeatable
  evidence
  passed, while the original encrypted/scanned checks were unverified; their later recovery
  is recorded above. Review and merge remain pending. Installed Tauri/platform proof remains
  M4, and product/release decisions remain M5.

- 2026-10-04: M3 proof in [PR187](https://github.com/canfieldjuan/eom-email-watcher/pull/187),
  consumer head `144cadaf8c0e7aed10fbb4af6604d4211e510104`, provider merged head
  `85ae9de619d545fbf5f09fb82188666c7cbc8273`. Real discovery, HTTP dispatch,
  provider parsing/validation, consumer settlement and engine listing passed for a generated
  ordinary COI (two policy rows, explicitly substituted model), an encrypted empty-password
  PDF and a scanned PDF (both `DOCUMENT_UNREADABLE`, zero ledger rows). Replay and a new
  consumer process preserved every result. Concurrent settlement, malformed-result classes,
  and date-uncertainty regressions passed; the current source renderer displayed all four
  expiry/review states from a real saved engine response. This does not establish installed
  Tauri behavior. The eight-item evidence map is alias `coi-m3/acceptance-evidence.json`,
  SHA256 `57c8f5ea3b77447fc43842c6b20b7cc7a3da6fb2fadda732b3c71e755c54f8ad`.
  Current-head approved real-document replay remains unverified: CPU fallback was interrupted
  and a subsequent GPU launch could not allocate memory while an unrelated Ollama workload
  occupied the device. That workload was left running. M3 remains open until the actual local
  model produces a persisted record that passes private field/provenance comparison. The
  model profile is unchanged; M4/M5 and deferred attribution/recall work remain separate.

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
- 2026-10-02: Re-checked the M3 blockers against Invoice Processor `main`.
  Certificate code changed only in Invoice Processor #85 (2026-09-27, value
  parsing).
  - Encrypted input (historical finding, resolved by
    [Invoice Processor #110](https://github.com/canfieldjuan/invoice-processor/pull/110)):
    the earlier audit found that the shared reader lacked an encryption check. At tested
    provider `85ae9de619d545fbf5f09fb82188666c7cbc8273`, the
    [certificate entrypoint](https://github.com/canfieldjuan/invoice-processor/blob/85ae9de619d545fbf5f09fb82188666c7cbc8273/src/invoice_processor/connect/service.py#L510)
    requests `reject_encrypted=True`; the
    [PDF reader](https://github.com/canfieldjuan/invoice-processor/blob/85ae9de619d545fbf5f09fb82188666c7cbc8273/src/invoice_processor/extract/pdf.py#L433)
    checks encryption before reading pages or creating a model. The
    [readable encrypted PDF regression](https://github.com/canfieldjuan/invoice-processor/blob/85ae9de619d545fbf5f09fb82188666c7cbc8273/tests/test_certificate_connect.py#L167)
    proves the library can read the generated empty-password PDF while the real certificate
    job returns the error required by the canonical provider input contract linked above.
    This regression passed again during the 2026-10-04 reconciliation. Invoice behavior is
    unchanged. The original audit is not current provider behavior.
  - Scanned input: Invoice Processor now accepts OCR input for invoices
    (`application/vnd.local-connect.ocr-pdf`), but `certificate.extract` still
    builds its catalog from native text only. Its parser raises internal
    `NO_NATIVE_TEXT`, which the certificate Connect boundary maps to public
    `DOCUMENT_UNREADABLE`. OCR admission for certificates would be a provider
    contract change; it stays an open coverage gap here.
  - Model runtime: the CPU-placement failure predates the shared per-user model
    runtime in connect-contracts ADR-0011 (Linux, on `main`). Its Windows
    amendment is proposed in connect-contracts #58, and the host itself is not
    built yet. Until Invoice Processor attaches to it, an M3 run must record
    the runtime it actually used and its device placement.
  - Inputs: under the accepted contract the encrypted COI should fail
    `DOCUMENT_UNREADABLE`; an unreadable image-only scan has the same public
    error. The earlier `NO_NATIVE_TEXT` wording confused an internal parser code
    with the capability error. Neither can show a
    completed ledger record, so M3 also needs one ordinary, unencrypted,
    text-bearing real COI, chosen by the operator and kept outside Git.
  - Finish conditions tightened: M3 and M4 now require at least one real,
    successfully persisted ledger result; expected failures add coverage only.

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
