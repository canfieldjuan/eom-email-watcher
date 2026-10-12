# Inbox attachment confirmation visibility

## Contract

Operator accepted this product slice in the session. The existing Inbox header
already says "Needs your confirmation". The change belongs in the expanded
message details, where the model assessment currently reads "Action required"
and can say "No action required." beside a pending attachment automation.

### Root cause

`desktop/src/main.ts:1708-1718` renders the email model's assessment under a
message-wide label. That label predates attachment automation (introducing
commit `81333099`). It conflates the email assessment with separately projected
attachment consent. `inboxActionState.ts` already owns the projected action
state vocabulary and header precedence. A second status system is unnecessary.

### Validated inventory before implementation

- Installed proof: `coi-unattended-scheduler-20261011` on Watcher
  `d36d6c67ba6dc04923a388e1a261192cfb176656` and Invoice Processor
  `c69833923d6398da8b1e0cb30cecb1f288793b62`. Actual scheduled intake,
  native confirmation, automatic completion, exact certificate baseline
  fields except extraction time, fresh-process expiry projection, unchanged
  rule/config, released model lease, and reopened native Inbox result passed.
  Window closure was operator-attested; worker PID capture was unavailable.
  This does not establish timer-only or all-processes-exited completion.
- Input alias: approved ordinary COI, sha256
  `1541b98122a2f4f267a9a6433c099244977ebc87d3732df891b8455cbfe37ce0`.
  Its private retained baseline is compared without publishing document text.
- Existing validated harness: `scripts/coi_local_proof.py`, reached through
  the retained `ordinary-native-parity.py` adapter. Same real provider,
  discovery, dispatch, polling, terminal application, persistence and fresh
  consumer ledger query. Harness sha256
  `06e20d127569a12157301e53bd1aedfc1d5f90023a6b253e298550edffc9870c`.
- Native profile: `58bf361ec2d2a082931ffe19fb70dc9a22ffd8d7c10bc7837df137f843cd60d1`;
  Qwen3.5-9B Q4_K_M, llama.cpp `c1d0e7a004015f23bc0233470b747b596f29b264`,
  CUDA exclusive runtime, context 32768, one slot, flash attention, full GPU
  layers, Jinja template with enable_thinking=false and reasoning off.
  Generation temperature 0.0; system/user message boundaries and schema JSON.
  Exact model/runtime/template hashes and argv are in the inventory receipt.
- Prompt owner: certificate `SYSTEM_PROMPT`, `build_user_prompt`,
  `certificate_batches`; prompt source sha256
  `7a22c412929a2d625564ec69d99fee5f02841662cbf4d8160ea7872ed7688c6b`.
- Prospective consumer base: `34505e2756217637cce95f0234d99ba03468cc6e`.
  Before this contract, the same-input native parity passed all extracted
  fields, provenance, one fire/attempt/job, expected terminal/expiry projection,
  replay and restart checks. The isolated provider exited cleanly.

Differences from installed proof: current Watcher main contains later manifest
feature/identity/parameter selection and mailbox/thread reconciliation changes;
provider and frozen native profile are unchanged. The parity run uses a new
isolated data/runtime directory, staged mailbox and automatic isolated rule,
and the installed provider executable instead of its identical extracted
package binary. It is extraction/consumer parity, not another native-consent
or unattended-mail proof. The product change adds presentation only.

Evidence aliases and sha256:
- `validated-inventory.json`: 5297073ef71e5db6e4a1540c136ec5111d1edf002c576b22afd0cbc9adac952d
- `ordinary-native-parity.json`: feabcae2e08d0cd4833c80f1f74b51a5aad6364aefb5b4479a43533d73023913
Both are durable in `inbox-confirmation-visibility-20261012`.

### Required change surface

- `desktop/src/inboxActionState.ts`: expose attachment-only pending
  confirmation from the existing fire-state ranking owner.
- `desktop/src/main.ts::renderInbox`: derive attachment fire states once for
  the existing chip and new details cue. Pending attachment confirmation adds
  label "Automation" with "Confirmation required. Review the attachment
  details below." before the model assessment. While pending, the model line
  label is "Email action"; its assessment text remains the same.
- Focused desktop tests execute the actual production details rendering and
  existing state owner; they must fail before the implementation.

### Explicit non-scope

No model, prompt, capacity, consent admission, native queue, engine, provider,
API, schema, rule, calendar confirmation, ledger, notification or dependency
change. Existing attachment Confirm/Decline controls and the Inbox chip stay.
No new ranking, raw reason rendering, optimistic confirmation, or copied state
classification. No installation/deployment is part of this source PR.

### Assumptions/blockers

The cue reflects durable engine-projected attachment states and grants no
permission. A calendar-only confirmation cannot produce an attachment cue.
Existing decision/event refreshes replace the Inbox projection; the cue is
recomputed on each render and clears when no attachment remains pending.
Current-main native parity passed before the behavior change. Independent
exact-head review and required CI remain merge gates.

### Verification plan

- Fail-first: pending attachment plus action_required=0 must render the cue
  before "No action required." and qualify its label as "Email action".
- Boundary cases: every non-pending fire state; unknown/falsy/malformed state;
  no fires; mixed attachments with pending/failed/completed; calendar-only;
  action_required 1/0/null; hostile suggested action as text; refresh through
  queued/submitted/completed/declined and a remaining second pending fire.
- Adjacent action-state and automation-decision tests; TypeScript/Vite build.
- Render production details with existing CSS and inspect browser screenshots
  at desktop and narrow widths. This is source presentation verification,
  not a new installed/native-IPC proof.
- Cold diff and gap audits; CI and independent review of the published head.
