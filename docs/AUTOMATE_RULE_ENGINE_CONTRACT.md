# Automate rule engine contract

Status: **proposed; contract only, no implementation.** Implementation begins
only after this document is reviewed and accepted.

This document is the behavioral contract for the Local Connect rule engine: a
user-defined trigger layer inside Email Watcher that generalizes the one
automation that exists today (`email.schedule_event`) into rules a person can
write, such as "this vendor, a PDF that looks like a bill, hand to Invoice
Processor" and "this sender, any PDF, hand to Document Summarizer".

Every claim about existing behavior below cites a file and line at the heads
listed in section 0. Findings are sorted **confirmed / contradicted /
could-not-determine**. Nothing is marked confirmed without a citation.

---

## 0. Ground truth this contract is anchored to

**Revision 2 (2026-09-09).** Revised after the local review of the first
draft (`f500a1f`). The review confirmed every finding it raised; the
changes are listed in section 13. This document is a design contract for a
separately authorised Automate slice. It is not part of, and does not count
as progress on, the Local Connect progress dashboard, which reports both
Automate rows as *planned* until a rule engine exists in code.

Two revisions of `eom-email-watcher` matter here. The first draft was read
against `29fd046` (#122). Since then #123 ("Wake and render the durable
Connect queue") and #124 ("Record Invoice Processor queue proof") merged;
this revision is anchored to `c10a37c` and keeps the `29fd046`
observations as dated history where they no longer hold.

Read from `origin/main` after `git fetch` on 2026-09-09. Local checkouts were
not trusted: the `eom-email-watcher` checkout was on a benchmark branch 63
commits behind main with four modified files, and `doc_sum` was on a test
branch.

| Repo | origin/main head read | Subject |
|---|---|---|
| eom-email-watcher | `c10a37c06b8fc75a0f84ce5efe75945435312730` | Record Invoice Processor queue proof (#124); first draft read `29fd046f7ba1ef8cead5d742836378989359d3d1` (#122) |
| invoice-processor | `74f8bca0d1e0b9c0bae177ef6ee4c9ebce890086` | Roadmap: Slices 6 and 7 shipped; Slice 8 not yet chosen (#33) |
| document-summarizer | `3babb35a205300dfa11926328271166e08ad4a16` | Add combined profile release acceptance gate (#50) |
| connect-contracts | `3005d82a7be885fba36f8688b5967a5b56a0abea` | Define Windows Local Connect placement (#8) |

The `document-summarizer` repo is on this machine at `~/Desktop/doc_sum`
(remote `canfieldjuan/document-summarizer`), not under the name the task
prompt used.

The commit-title check the prompt asked for reproduces exactly:
`git show --stat --format='%h %ad %s' --date=short 41267c6` shows one file,
`docs/CONTRACTS.md | 467 +`, and no code.

---

## 1. What contradicts the documentation (lead findings)

### 1.1 Contradicted

**C1. "A queue, so a batch does not hit a busy provider: closed"
(`Local Connect Status.md:53`) was not true at `29fd046`, and is true at
`c10a37c` only while the desktop host process is running.**

Dated history (`29fd046`): the storage, the engine pump operation, and the
source-lock coordination existed, but nothing invoked the pump; the only
reference to `connect.queue.pump` outside its definition was its entry in
the operation table (`engine_api.py:3667`), and a job deferred to `waiting`
after `PROVIDER_BUSY` advanced only when a person clicked again.

Current (`c10a37c`, #123): the Tauri host starts a
`ConnectQueueScheduler` at setup (`desktop/src-tauri/src/lib.rs:802-803`).
It runs one thread, `email-watcher-connect-queue`, that calls
`engine.pump_connect_queue()` (`scheduler.rs:91-101`), which sends
`connect.queue.pump` with `{"limit": 25}` (`engine.rs:1066`); the thread
sleeps until the engine-supplied `next_wake_unix_ms`, retries after 30 s on
error, and gives each pump request a 30-minute timeout
(`scheduler.rs:17-18`). The pump (`engine_api.py:3101`) expires overdue
waiting jobs, takes due lane heads, and runs each claimed job with
`wait_for_terminal=False` (`engine_api.py:3090`): a job that is still active
after the submit or GET raises retryable `JOB_TIMEOUT`
(`engine_api.py:2475-2480`) and the pump moves on, so no pump call blocks
on provider completion. #124 recorded a live two-invoice proof of this loop
against Invoice Processor `74f8bca` (`docs/CONTRACTS.md`, "Exact-current
operational proof").

Still true at `c10a37c`: the timer-driven install never pumps. The unit runs
`eom-mail-watch check` (`systemd/eom-email-watcher.service:11`, timer
`OnBootSec=2min`, `OnUnitActiveSec=2h`), and neither `cli.py` nor
`service.py` references the pump. A job left `waiting` with no desktop open
advances only when the desktop next runs, and the two-hour admission window
(`db.py:34`, `CONNECT_QUEUE_ADMISSION_WINDOW`) can expire it first. A rule
engine that runs unattended without a desktop must therefore own a pump
call of its own (section 6.5); with the desktop open it must not duplicate
the host's.

**C2. The State 03 diagram (`Local Connect Product Brief.md:107-116`) draws
Email Watcher automatically handing work to Summarizer or Invoices. No such
path exists.** The service loop that runs the one automation never imports
or calls the Connect consumer (`service.py:1-79` imports; no `connect`
reference anywhere in `service.py`). The automation's effect is the
watcher's own Microsoft Graph calendar adapter
(`service.py:52-54`, `827-835`), not a Connect capability. Connect
invocation is reachable only through the desktop operation
`connect.attachment.invoke` (`engine_api.py:3061`, table entry `3660`), which
requires an explicit provider and capability selection in the request
(`3082-3084`). Automate and Connect share a licence gate; they are not
composed anywhere in code.

**C3. "Permission to act unattended: closed" (`Status.md:52`) overstates
what exists.** The second feature string is enforced (section 2.3) but it is
defined in exactly one place in the ecosystem:
`src/eom_email_watcher/entitlement.py:35`. It appears nowhere in
`connect-contracts` (`git grep` over origin/main finds only
`connect.capability_exchange` at `adr/0003:52`, `adr/0004:132`,
`tests/test_contracts.py:26`, `tools/entitlement_issuer.py:31`), nowhere in
`document-summarizer` (`src-tauri/src/connect/entitlement.rs:28` declares
only `connect.capability_exchange`), and nowhere in `invoice-processor`
(`src/invoice_processor/entitlement/document.py:43`, same). The claims
schema types `features` as any array of pattern-matching strings with no
enumeration (`entitlements/v1/claims.schema.json:27-37`). See 1e-Q1.

**C4. Resolved by #123.** At `29fd046`, `docs/CONTRACTS.md:858-859` still
said "engine pump ... remain the later slices" two merged PRs after the pump
landed in #121. #123 changed the status line to "issue #117 complete.
Durable storage, engine pump, Tauri host/UI behavior, and the exact-current
Invoice Processor operational proof are implemented and recorded below."
Kept as dated history; no longer a contradiction.

**C5. "Follows a list of senders you choose, across multiple accounts"
(`Brief:138`) describes a shape the code does not have.** The sender list is
one global allowlist in `config.toml` (`config.py:98`, `101-102`), not per
account. Multiple accounts may be retained but exactly one is active for
polling (`db.py:1891` unique partial index on `active = 1`;
`db.py:2261-2266`), and one check polls that one account
(`service.py:1441-1443`, `1091-1095`). Rules that scope to an account
(section 4.1) therefore scope to "the account that was active when the
message arrived", carried on every message row (`db.py:1957-1958`).

**C6. "The bytes are fetched from your mailbox at the moment you act on that
one file" (`Brief:150-152`, `342`).** The storage claim holds
(`db.py:2001-2011` stores descriptors only). The "when you act" half no
longer holds: the queue pump re-fetches attachment bytes with no user action
on every retry (`engine_api.py:2942-2959`, `2604-2618`). The correct claim is
"fetched on demand for a job you started, never stored".

**C7. `docs/CONTRACTS.md:812` lists "a workflow builder, generic rules
engine" as explicit non-scope of the calendar contract, and `1301-1302`
excludes "workflow/automation rules" from the queue contract.** Neither
document is wrong; but the brief (`318-321`) and `Status.md:51` present the
rule engine as the one open item, and no contract for it exists in any of
the four repos (searched `docs/`, `plans/`, `adr/` in all four; the only
Automate mentions in invoice-processor are the signing deferral,
`docs/contracts/SLICE-7.md:20,91,244`, and a consumer-side deferral,
`SLICE-5.md:426`). This document is that contract.

**C8. Issue #117's text cites a `connect_jobs` table at `db.py:83`.** The
table is `connect_attachment_jobs` (`db.py:92`). Minor, but the rule engine
must name the real table.

**C9. `adr/0004-connect-entitlement-activation-v1.md:132`: "Activation
grants only `connect.capability_exchange`."** Email Watcher's install path
admits any signed envelope whose feature list includes
`connect.capability_exchange` (`entitlement.py:213-226` evaluates each
requested feature against the same file), and the watcher then reads a
second feature from that same installed file (`service.py:112`). A licence
carrying both features installs and grants both. The ADR sentence was true
of the v1 scope and is now stale as a description of what activation
grants.

### 1.2 Confirmed

**K1. A provider processes one job at a time and refuses the rest with a
retryable `PROVIDER_BUSY`** (`Brief:209-210`, `Corrections.md:26`,
`adr/0001-connect-v0.md:91-93`). Invoice Processor:
`src/invoice_processor/connect/server.py:385`, `connect/store.py:222`,
`connect/errors.py:61` (409, retryable). Document Summarizer:
`src-tauri/src/connect/provider.rs:667-673` and `806-812`. Nothing on any
provider queues.

**K2. The two-key gate is enforced in the service loop** (`Brief:252-254`,
`Corrections.md:14`). `service.py:112` and `:140` (proposal authorization,
before and after token validation), `:179-181` and `:205-207` (write
authorization), `:947` (human decision). The check requires every listed
feature to be active in one installed file (`entitlement.py:213-226`,
`all(...)`). Test: `tests/test_service.py:542`
(`test_watcher_requires_full_scheduling_automation_authorization`).
Nuance the brief omits: a write that was already `write_authorized` is
still performed after entitlement loss (`service.py:797`,
`require_active_entitlements=False`), by design
(`docs/CONTRACTS.md:498-499`: "gates new capability use, not revocation").

**K3. The automation halts instead of guessing** (`Brief:260-262`). Intent
mapping at `service.py:260-267`: `new_meeting` proceeds to `proposing`;
`reschedule` and `cancellation` halt in `manual_review`; anything else halts
in `ambiguous`. The full state set is fourteen states (`db.py:329-333`), not
the three the brief names; see section 2.4.

**K4. The write fires only from `write_authorized`, reached only by a person
confirming one proposal by content hash** (`Brief:264-268`).
`db.py:4277-4284` requires `awaiting_confirmation`, the expected
`state_version`, the exact `current_payload_id`, and the exact
`proposal_sha256`; `4310-4324` returns an expired or past-start proposal to
`proposing` instead of authorizing; `4326-4333` records `write_authorized`
with a fresh `transaction_id`. The write itself is `service.py:827-835`.

**K5. "Connect actions locked" is the established visible-but-inert
pattern** (`Brief:102`, `234-235`). `desktop/src/main.ts:1474-1478` renders
the span when discovery returns `connect_entitlement_required`
(`desktop/src/connectAvailability.ts:9`; engine side
`connect.py:1050-1051`).

**K6. Connect results are stored against the message** (`Brief:279`).
`connect_attachment_jobs` is keyed by `message_id` and `part_id`
(`db.py:92-95`) and carries the output descriptor and `result_json`
(`db.py:124-146`); rows are deleted with the message except reconciling or
provider-owned tombstones (`db.py:296-317`).

**K7. Attachment bytes are re-fetched on demand through the mailbox port**
(`Brief:277`). `mailbox.py:82-84` (protocol), `gmail.py:396`,
`microsoft365.py:642`, `imap.py:1057`; consumer call sites
`engine_api.py:1938`, `2955`, `3115`, `3342`.

**K8. ADR-0001's no-callback, no-queue, no-retry, one-job position is still
the accepted contract position.** ADR-0002 defers "a broker or workflow
engine" (`adr/0002:154-157`) and adds no callback; ADR-0003, 0004, 0005 do
not touch job direction. The consumer-owned queue in Email Watcher polls and
re-POSTs from the caller side; it does not invert the direction. Section 8
says whether a superseding ADR is warranted anyway.

**K9. Both current providers declare zero parameters and no external
effects.** Invoice Processor: `connect/manifest.py:47-50` (`accepts`
`application/pdf` up to 32 MiB, `produces` `text/plain` and
`application/vnd.local-connect.invoice+json`, `parameters: []`,
`effects {external: false, confirmation_required: false}`). Document
Summarizer: `src-tauri/src/connect/contracts.rs:15-17`, `connect/v2.rs:171`
(`parameters: vec![]`), `v2.rs:336-337` (both effects false), input bound
from `DOC_SUM_CONNECT_MAX_BYTES` (`provider.rs:162`).

### 1.3 Could not determine

**U1.** Whether the installed production entitlement on this machine
carries `connect.automations`. The file is under `~/.config/local-connect/`
and was not opened; a memory note from 2026-09-08 says it does. Not needed
for the design.

**U2.** Whether Invoice Processor's Connect job path calls the same
`Ledger.ingest` as its CLI (`Brief:291-292`). Out of scope for this
contract; the engine treats every completed job as opaque output.

**U3.** Whether the attendee-warning copy quoted in `Brief:340` matches the
desktop at head. `desktop/src/main.ts` was not read for that string.

---

## 2. Phase 1 findings, in the order the prompt asked

### 2.1 The existing trigger, end to end (1a)

**Who invokes the pass.** Two hosts, one lock.

- systemd: `systemd/eom-email-watcher.timer` fires every two hours
  (`OnUnitActiveSec=2h`) and runs `eom-mail-watch check`
  (`systemd/eom-email-watcher.service`, `ExecStart`). The CLI takes a
  native lock at `<database>.check.lock` (`cli.py:73-77`) and calls
  `run_watcher_check` (`cli.py:180-198`).
- desktop: `desktop/src-tauri/src/scheduler.rs:133-134` wakes on its own
  interval and calls the engine's `watcher.check` (`engine.rs:1325`), with a
  thirty-minute engine timeout (`scheduler.rs:15`). The engine takes the
  same lock path (`engine_api.py:270`, `1589-1596`).

Two passes cannot overlap. A desktop click on a Connect action does **not**
take that lock (`engine_api.py:3061-3245` takes only the lane and source
locks, `3148-3153`), so a click can run concurrently with a pass.

**Order within a pass** (`service.py:1438-1460`):

1. `process_scheduling_writes` (drain authorized calendar writes);
2. `process_scheduling_automations` (recover `detected`/`extracting` runs);
3. `process_scheduling_proposals`;
4. `Watcher.check` (the mail pass proper);
5. steps 2 and 3 again, excluding run ids attempted in step 2/3;
6. deliver `automation_review` notification intents (`1461-1470`).

**Where the trigger is evaluated.** Inside the mail pass, per pending
message (`service.py:1314-1361`): fetch content (`1339`), persist attachment
descriptors (`1341`), run the analysis model (`1342-1350`), and if
`analysis.category == "scheduling"` compute the automation principal
(`1352-1356`). `_scheduling_automation_principal` returns `None` unless the
provider is Microsoft 365, both licence features are active
(`service.py:110-113`), the account has an address, the proposal grant is
`ready`, and the tokens validate (`114-147`). `mark_analyzed` then admits the
run in the **same** `BEGIN IMMEDIATE` transaction as the analysis state
change (`db.py:5405-5421` at `c10a37c`; `5340`, `5374-5386` @29fd046; `924-975`), inserting `automation_runs`
in state `detected` with `ON CONFLICT DO NOTHING` on the unique key
`(provider, account_id, source_message_key, automation_id,
automation_version)` (`db.py:354-356`, `942-944`). That is the
cannot-lose, cannot-duplicate admission the calendar contract requires
(`docs/CONTRACTS.md:575-581`).

**The record it predicates over.** Three pieces, and only one is stored:

- `PendingMessage` (`db.py:1111-1128`): `message_id`, `provider`,
  `account_id`, `provider_message_id`, `thread_id`, `sender`,
  `sender_name`, `subject`, `received_at`, attempt bookkeeping.
- `MessageContent` (`mailbox.py:64-68`): `body`, `attachment_names`,
  `attachments` as `AttachmentDescriptor(part_id, attachment_id, filename,
  media_type, byte_size, position)` (`mime.py:27-33`). The body is held in
  process memory only; the descriptors are persisted (`db.py:2001-2011`).
- `Analysis` (`model.py:62-65`): `category` from the closed set
  `invoice | scheduling | customer_request | automated_notice |
  informational | other` (`model.py:112`), `priority`, `summary`,
  `action_required`, `suggested_action`, `deadline_text`, `deadline_iso`,
  `confidence`. Persisted on the message row (`db.py:5350-5358`).

The trigger predicate today is literally one comparison,
`analysis.category == "scheduling"` (`service.py:1354`), and the automation
identity is one constant pair, `email.schedule_event` version 1
(`db.py:30-31`).

**Licence gate.** Section 1.2 K2. Also `engine_api.py:885-889`
(`_automation_entitlement_active`) hides proposal previews in the inbox when
the pair is not active (`1599-1612`), except runs already past the
confirmation boundary.

**State machine.** `db.py:329-333`:

```text
detected -> extracting -> ambiguous | manual_review
                      \-> proposing -> awaiting_confirmation | manual_review
awaiting_confirmation -> proposing | declined | write_authorized
write_authorized -> writing -> completed | failed | unresolved
unresolved -> reconciling -> completed | unresolved
any pre-submit state -> source_unavailable
```

(`docs/CONTRACTS.md:585-596` for the transition list; `service.py:260-267`
for the intent mapping; extraction gets exactly one retry, `db.py:424`
`attempt_no IN (1, 2)`; second rejection lands in `manual_review`.)

Halting states, i.e. the automation stopped and asked: `ambiguous`,
`manual_review`, `source_unavailable`, `awaiting_confirmation` (waits for a
person), `unresolved` (waits for reconciliation). Terminal: `declined`,
`completed`, `failed`.

**Human handoff.** The existing calendar notification query selects only
`ambiguous`, `manual_review` and `source_unavailable`
(`db.py:5530-5531` at `c10a37c`), delivered by `service.py:1013-1045`
through `send_review` and acknowledged through the existing intent path.
It does not emit a run-transition notification for `awaiting_confirmation`,
`unresolved`, `completed`, `declined` or `failed`; the general message-analysis
notice is a separate event. The desktop shows the
proposal from durable rows; the decision comes back through the engine
operation `calendar.automation.decide` (`engine_api.py:1662`, table `3657`)
into `decide_scheduling_proposal` (`service.py:936-1000`), which re-checks
both features (`947`) and delegates to `store.decide_automation_proposal`
(section 1.2 K4). A confirm immediately attempts the write in the same call
(`service.py:996`).

### 2.2 The data the rules will match on (1b)

**Stored after a pass** (`messages`, `db.py:1955-1987`): provider,
account_id, provider_message_id, thread_id, sender, sender_name, subject,
received_at, discovered_at, status, analysis fields (category, priority,
summary, action_required, suggested_action, deadline_text, deadline_iso,
confidence). **Not stored:** body, headers other than the fields above,
attachment bytes.

**Only on re-fetch:** body (`gateway.content`, `mailbox.py:80`; fetched at
analysis `service.py:1339` and again on every extraction attempt
`service.py:369-378`, bounded by `body_char_limit`) and attachment bytes
(K7). The retention boundary is enforced at fetch time
(`engine_api.py:1870-1896`, `_connect_source_is_retained`).

**Attachments:** descriptors only (`db.py:2001-2011`), replaced on each
analysis (`service.py:1341`, `db.py:2873`). The re-fetch path a rule action
must use is exactly the one `connect.attachment.invoke` uses:
`engine_api.py:3108-3120` builds a closure over
`gateway.attachment_bytes(provider_message_id, part_id, attachment_id)`;
the queue pump builds the same closure at `2942-2959`. Both re-verify size
and SHA-256 against the enqueue-time identity before any POST
(`2649-2657`), so a rule action inherits "the bytes that were queued are the
bytes that are sent".

**Multi-account and senders.** Section 1.1 C5. `mail_accounts`
(`db.py:1879-1889`) keyed `(provider, account_id)`, one active. Senders are
the global allowlist (`config.py:70-72`, `98`, `101-102`) and the pass
drops anything not on it (`service.py:1144`). A rule never sees a message
from an unwatched sender; the allowlist is a precondition, not a rule
condition.

### 2.3 The action side (1c)

**Manifests.** Section 1.2 K9. In the protocol's vocabulary
(`schemas/v2/manifest.schema.json:67-117`): one capability each, one
accepted media type each (`application/pdf`), `parameters` empty,
`effects.external false`, `effects.confirmation_required false`. A job
request carries exactly one input artifact (`job-request.schema.json:12-17`)
and a parameters object whose values are bounded strings, integers, or
booleans (`18-29`). A completed job returns one to eight output artifacts of
at most 2 MiB each (`job-status.schema.json:83-113`); an error carries
`code`, `message`, `retryable` (`114-123`).

**Consumer path.** Discovery (`connect.py:1043-1135`): refuses without the
exchange entitlement (`1050-1051`), reads owner-private v2 registrations,
GETs `/v2/manifest` with the bearer token (`1085-1097`), drops a manifest
whose instance or app id disagrees with its registration (`1100-1104`),
drops an instance advertised twice with different content (`1106-1110`),
and sorts by action label (`1116-1131`). Invocation
(`engine_api.py:3061-3245`): replay of an existing terminal job
(`3095-3098`), entitlement (`3100`), rediscovery of the selected provider
and parameter validation (`3102-3106`), media-type and size admission
against the manifest (`3138-3142`, `connect.py:453-460`), confirmation
requirement (`3143-3147`), lane lock and source lock (`3148-3153`), one
byte fetch (`3167`), atomic job plus dispatch creation with the 25-job lane
cap (`3184-3206`), then resume (`3240-3245`) which claims the lane head
under the native lane lock (`2802-2810`) and either submits (`2597-2704`)
or, if not the head, returns `connect_job_in_progress` (`2817-2820`).
Submission polls to a terminal state synchronously
(`2425` `wait_for_terminal`, default budget thirty minutes,
`connect.py:53`). The result is written to `connect_attachment_jobs` (K6).

**`PROVIDER_BUSY` today.** With `retryable: true` on a proven-new POST it is
recorded and the job returns to `waiting` with the 2/4/8/16/30-second
schedule (`engine_api.py:2526-2540`, `2503-2508`); an ambiguous failure
goes to `reconciling` (`2480-2488`); a definitive non-retryable refusal is
terminal (`2662-2667`). Ownership of the retry is the pump
(`engine_api.py:3101-3127`), called by the desktop host's queue thread
(`scheduler.rs:99-104`) and by nothing in the timer-driven path (C1). So:
retry policy and retry execution both exist when the desktop runs; under
the timer alone there is no retry execution, and a waiting job's fixed
two-hour admission deadline
(`db.py:261`, `docs/CONTRACTS.md:1017-1018`) is measured against a systemd
timer that fires every two hours. Section 6.5 designs around that.

### 2.4 Contract constraints (1d)

K8. In addition, the v2 `instance_id` is the provider's durable state
namespace, stable across process restarts unless the provider resets its
state (`adr/0002:87-96`), which is what makes "reconcile the same job after
the provider restarted" possible and what the rule engine relies on for
pinning a job to an instance. Two providers for one capability is an
`ambiguous_provider` diagnostic, never a silent choice
(`adr/0001:110-112`; `connect.py:1106-1110` for the instance-level case).

### 2.5 Open questions answered (1e)

**Q1. Does `connect.automations` exist outside Email Watcher?** No.
Section 1.1 C3. It is an unversioned string literal shared across nothing;
only the watcher reads it. It fails silently in both directions:

- a licence issued with a misspelled second feature is a valid, active
  licence for capability exchange (`entitlement.py:213-226` evaluates
  features independently) and every automation admission returns `None`
  with no diagnostic (`service.py:112-113`), so the person sees no
  proposals and no error;
- a provider cannot tell whether the caller is licensed for automation,
  because ADR-0003 deliberately keeps entitlement out of the wire
  (`adr/0003:23-25`), so a modified consumer that skips the gate cannot be
  refused on the provider side for the automation half. The exchange half
  is defended in depth (`adr/0003:27-34`); the automation half is defended
  only in the consumer.

Section 8.3 says what to move into `connect-contracts`.

**Q2. Is there an issuance path for `connect.automations`?** Yes, by hand,
and by nothing else. `tools/entitlement_issuer.py:535` accepts repeated
`--feature` values; `:581` defaults to `[connect.capability_exchange]` only
when none is given; `:341-344` validates each against the same pattern the
schema uses and nothing more. So an operator can issue a both-feature
licence today. There is no named tier, no fixture, no test, and no ADR
sentence that says the string is sellable; ADR-0004:132 says the opposite of
what the watcher does (C9). The Automate tier is enforceable and issuable,
but not specified. That is a contracts gap, not a code gap.

**Q3. What did #120, #121, #122 land, and does one-job-at-a-time hold?**

| PR | Layer | Evidence |
|---|---|---|
| #120 storage | schema 19 (`db.py:22`); `connect_job_dispatch` with states `waiting / dispatching / reconciling / provider_owned / terminal` (`db.py:251-271`); lane ordering index (`274-280`); active-fingerprint unique index (`282-286`); delete triggers that keep reconciling tombstones (`288-317`); native lane and source lock identities (`locking.py`, per PR body) | storage and admission primitives only |
| #121 pump | 2/4/8/16/30 backoff (`engine_api.py:2503-2508`); busy to `waiting` (`2526-2540`); `connect.queue.pump` draining due lane heads under the lane lock (`3006-3127`); entitlement-free discovery only for reconciliation (`connect.py:1152-1163`; `engine_api.py:2907-2921`) | historical at `29fd046`: engine operation existed with no caller; the host caller landed in #123 (next row) and the timer-driven path still has none (C1) |
| #122 source lock | source lock held across fetch, hash check, POST, and outcome persistence (`engine_api.py:2604-2676`); retention checked at enqueue and handoff (`1870-1896`, `2613-2616`); transient mailbox failure returns to `waiting` (`2633-2648`) | coordination between handoff and cleanup |

| #123 host wake and UI | `ConnectQueueScheduler` started at setup (`lib.rs:802-803`); pump at startup, after clicks and checks, and at the engine's `next_wake_unix_ms` (`scheduler.rs:91-150`, `275`); non-blocking pump rounds (`engine_api.py:2475-2480`, `3090`); expiry sweep (`db.py:3202`); Inbox rows carry dispatch state (`db.py:5872-5884`) | landing step 4 |
| #124 proof | two real invoices through the real Invoice Processor, four bounded pumps, zero operator retries (`docs/CONTRACTS.md:1302-1341`) | landing step 5, Linux |

Not landed anywhere: a pump in the timer-driven path (C1). The `29fd046`
citations in the #121 and #122 rows are labelled as such in section 0.

One-job-at-a-time still holds at the provider boundary (K1) and is now
mirrored on the consumer side: one process may own one provider lane at a
time (`engine_api.py:2889-2892`, `3009-3019`), and the lane is
`(protocol 2, provider_app_id, provider_instance_id)`, shared by every
capability that instance exposes (`docs/CONTRACTS.md:912-921`). Therefore
the rule engine may fan out across distinct provider instances and must
serialize within one instance, and it inherits that serialization for free
by going through the same enqueue and lane-head code. It does not get to
skip the queue.

---

## 3. Design constraints, checked against the code

- **The engine lives in Email Watcher.** Confirmed as the only option that
  needs no protocol change: the trigger source is the mail pass, which the
  watcher owns (`docs/CONTRACTS.md:149-151`), and ADR-0001 has no
  callbacks (`adr/0001:91-92`). A separate process would need to be told
  when mail arrives.
- **"Automate" is a licence tier, not a process.** Confirmed by how the
  existing gate works: a feature string read from the same installed file
  (`entitlement.py:34-35`, `213-226`).
- **Keep the engine core generic.** Adopted; section 4 defines the generic
  event and the mail adapter that produces it.
- **The existing automation must be expressible as a rule.** Section 4.6 is
  that rule. It is not special-cased; it is seeded.
- **Preserve the two-key gate and halt-don't-guess.** Sections 6.6, 6.7.

---

## 4. Rule shape

### 4.1 Definition

A rule is a bounded JSON document, validated by a strict model
(`extra` forbidden, closed enumerations, every string bounded), stored as
user input and never as code. Version 1:

```text
Rule
  rule_id          uuid4, engine-assigned
  name             1..80 characters, untrusted display text
  enabled          boolean; recorded on every version row (below), so the
                   enabled state in effect at a revision is history, not a
                   mutable flag
  system           boolean; true only for the seeded scheduling rule
  version          integer >= 1; incremented on every accepted edit
  scope            { provider?: identifier, account_id?: string(<=128) }
                   absent field = any
  trigger          { source_kind: "mail.message" }
  conditions       1..8 entries, all must hold (AND)
  action           exactly one of the kinds in section 4.3
  confirm_each     boolean, optional, default false; permitted only when
                   action.kind is "connect.invoke" (rejected at validation
                   on calendar.propose and notify, whose handoff is fixed
                   by their own paths); when true every fire of this rule
                   is prepared and hashed by dispatch step 3a and then
                   waits for a person (section 6.7) even if the capability
                   declares no effects
  created_at, updated_at   UTC
```

Each accepted edit, **and each enable or disable**, produces a new
immutable row in `automation_rule_versions` (`rule_id`, `version`, the
full definition, `enabled`, `accepted_at` UTC for display, and
`revision`). An enable or disable row repeats the previous definition
bytes with the flag changed; it is a version like any other, so whether a
rule was enabled at a given revision is decided by the version in effect
at that revision and never by a mutable current flag. `revision` is a database-assigned monotonic integer: a
single-row `automation_rule_set(revision)` counter incremented inside the
same `BEGIN IMMEDIATE` transaction as every rule write, so two rule writes
can never share a revision and a revision can never move backwards under a
clock change. A superseded or deleted version also records
`retired_revision`, the revision of the write that retired it. `revision`
and `retired_revision`, not the timestamps, are the applicability boundary
used in section 5.1.

Deletion is a version, not a removal. Each `automation_rule_versions` row
carries a `kind` discriminator outside the strict Rule definition:
`definition` rows hold a validated Rule document; `deleted` rows hold no
definition at all and are never passed through Rule validation, so section
4.4 cannot mark them invalid or list them. `automation.rules.delete` writes
a `deleted` version, retires the previous version at that revision, and
hides the rule from the list; a `deleted` version applies to no message.
Every earlier version and every fire that references one stay exactly as
they were. The product meaning is precise: a deleted rule stops applying
to messages analysed at or after the deletion revision; a message analysed
before it, whose evaluation has not yet caught up, still fires the version
that was in effect at its analysis (section 5.1), because that work was
already eligible; and past fires remain explainable by the text that
produced them. Deletion does not cancel already-eligible work. A system
rule cannot be deleted.

### 4.2 Matching vocabulary

Conditions are `{ field, op, value }` triples from a closed table. Fields
are exactly the stored, non-content fields of section 2.2 plus the
attachment descriptor. No body, no header, no free-text summary: a rule is
a pure function of persisted rows, which is what makes evaluation
replayable and idempotent (section 5.3). Body predicates are deferred
(section 9).

| field | ops | value |
|---|---|---|
| `sender` | `equals`, `domain_equals` | normalized address or lowercase domain (`config.normalize_validated_address` for `equals`) |
| `sender_name` | `equals`, `contains` | 1..320 characters, case-folded |
| `subject` | `contains`, `starts_with` | 1..200 characters, case-folded |
| `category` | `equals`, `in` | one of `invoice, scheduling, customer_request, automated_notice, informational, other` (`model.py:112`) |
| `priority` | `equals`, `in` | one of `urgent, high, normal, low` (`model.py:65`) |
| `action_required` | `equals` | boolean |
| `attachment.media_type` | `equals` | media-type pattern from `manifest.schema.json:41-45` |
| `attachment.filename` | `glob` | 1..255 characters, no `/` or `\`, case-folded |
| `attachment.byte_size` | `lte` | integer 1..1073741824 |
| `attachment.count` | `gte`, `lte` | integer 0..64 |

`attachment.*` conditions are evaluated per attachment and **select** the
artifacts the action applies to; a message with three PDFs and a rule
whose attachment conditions match two of them produces two fires
(section 5.2). A rule with an artifact-consuming action (section 4.3
`connect.invoke`) must contain at least one `attachment.media_type`
condition; the validator rejects it otherwise, because the manifest bounds
the action to declared media types (K9) and the rule must say which.
Conversely, the per-artifact fields `attachment.media_type`,
`attachment.filename`, and `attachment.byte_size` are permitted **only**
on rules whose action consumes an artifact; a `calendar.propose` or
`notify` rule that names one is rejected at validation, because such a rule
produces one fire per message and a per-artifact selection would either
be meaningless or demand two fires under one sentinel key. Only
`attachment.count` is message-level and allowed on any action.

OR is expressed as two rules. Negation is not offered in version 1.

The two motivating rules:

```text
"this vendor, a PDF that looks like a bill -> Invoice Processor"
  sender domain_equals vendor.example
  category equals invoice
  attachment.media_type equals application/pdf
  action connect.invoke invoice.extract 1.0 @ invoice-processor

"this sender, any PDF -> Document Summarizer"
  sender equals alice@example.com
  attachment.media_type equals application/pdf
  action connect.invoke document.summarize 1.0 @ document-summarizer
```

"Looks like a bill" is the model's `invoice` category, whose meaning is
already pinned by the analysis prompt: the sender is asking the mailbox
owner to pay (`model.py:86-87`). A rule may tighten it with a filename
glob; it cannot loosen it, because the engine has no other bill signal.

### 4.3 Action kinds

Bounded by what manifests declare (section 2.3), plus the two internal
actions the watcher already performs.

**`connect.invoke`**

```text
action.kind         "connect.invoke"
action.capability   { id: identifier, version: "major.minor" }
action.provider     { app_id: identifier }     instance resolved at fire time
action.parameters   object, <=16 keys, values string(<=1000)|integer|boolean
```

At **save** the validator checks shape only. At **fire** the engine
rediscovers the provider and validates parameters against the live manifest
through the same code the click path uses (`engine_api.py:3102-3106`,
`connect.py:1212-1263`), rejects an artifact the manifest does not accept
(`connect.py:453-460`), and refuses to auto-invoke any capability whose
manifest declares `effects.external` or `effects.confirmation_required`
(section 6.7). Both current manifests declare neither, so both rules above
run unattended.

**`calendar.propose`**

No fields. Delegates to the existing scheduling automation: the fire admits
an `email.schedule_event` run exactly as `_admit_scheduling_automation` does
today (`db.py:924-975`), and that run proceeds through its own fourteen-state
ledger unchanged. The fire stores its own state, links the run id, and
settles from the run through section 6.1; display reads the linked run.

**`notify`**

No fields. Emits one `automation_review`-class notification naming the rule
and the message. Exists so a person can test a rule's conditions without
handing anything to a provider.

### 4.4 Validation and what an invalid rule does

Validation happens on every write through the engine operation and again on
every load. A rule that fails validation on load is marked `invalid` with a
bounded reason, is never evaluated, is shown in the rules list as invalid,
and blocks nothing else. A write that fails validation changes nothing;
there is no partial rule. Labels, names, and values are untrusted text
rendered only through the watcher's own text components
(`adr/0002:61-62`).

Rejected at validation, with tests required for each (section 7):
unknown field or op; op not permitted for the field; empty conditions; more
than eight conditions; a 101st rule in the install (tombstones excluded); a per-artifact `attachment.*` condition on a non-artifact action; `confirm_each` on a non-`connect.invoke` action; an artifact-consuming action without an
`attachment.media_type` condition; a sender that does not normalize; a glob
containing a path separator; a category or priority outside the closed set;
a capability id or version that does not match the schema patterns
(`manifest.schema.json:36-40`, `81`); a parameters object with more than
sixteen keys or a non-primitive value; a `system` rule whose action or
conditions were changed (only `enabled` may change on a system rule).

### 4.5 Where rules are stored and how they are edited

**SQLite, in the watcher database, schema 20.** Table `automation_rules`
holding the bounded definition (`<= 16 KiB`), the version, the enabled flag,
the system flag, a `definition_sha256`, and timestamps. Every accepted edit
inserts a new immutable `automation_rule_versions` row and bumps the
current version; fires reference `(rule_id, rule_version)`, so a run can
always be explained by the exact rule text that produced it.

Edited through engine operations, native desktop forms only:
`automation.rules.list`, `automation.rules.get`, `automation.rules.put`,
`automation.rules.delete` (a tombstone version, section 4.1),
`automation.rules.set_enabled`. There is no text DSL and no file a person
edits by hand.

Rejected: rules in `config.toml` beside the sender list. Senders are
config because they are an allowlist with no run history; rules need a
version history and a transactional link to the runs they produce, which
TOML cannot give, and a hand edit would bypass validation.

### 4.6 The acceptance test: the existing automation as a rule

Seeded at migration as a system rule, **always present and enabled by
default**, whatever accounts or grants exist at migration time:

```text
rule_id     fixed uuid, recorded in code
name        "Meeting request -> propose a calendar event"
system      true
enabled     true at seed; false only by an explicit person action
scope       { provider: "microsoft365" }
trigger     { source_kind: "mail.message" }
conditions  [ { category equals scheduling } ]
action      { kind: "calendar.propose" }
```

Readiness is not a rule property. Today the admission checks -- provider is
Microsoft 365, the account has an address, the `proposal` grant is `ready`,
the tokens validate -- run when a scheduling message is processed
(`service.py:110-125`, `1351`), not at install time, and the first draft's
"enabled if and only if a ready grant exists at migration" would have
seeded the rule disabled on any install that connected Microsoft 365 later,
with no path back. Instead:

- The rule fires whenever its conditions match; the action's admission
  checks decide, per fire, whether a run is admitted. A fire whose action
  is not admitted (no Microsoft 365 account, grant not ready, tokens
  invalid) records the outcome `not_admitted` with the reason and does not
  halt, notify, or create a run -- exactly what today's code does for the
  same message (it skips silently). When the account or grant later becomes
  ready, the next matching message fires normally. No message is re-fired
  retroactively (section 5.1).
- `enabled` is owned by the person. A person may disable the seeded rule;
  that writes a disabled version at a fresh revision, and the engine never
  writes an enabled version on its own, not on migration, not on an
  account or grant transition, not on upgrade. A person may re-enable it,
  which writes an enabled version. Because enablement is a version, a
  message analysed while the rule was enabled still fires it if evaluation
  catches up after a disable, exactly as with deletion (4.1): the person's
  switch takes effect for messages analysed at or after it. They may not
  delete it or change its conditions in version 1.
- Everything the current code does after admission is unchanged, because
  the action **is** the current admission. What moves is only the predicate
  (`service.py:1354`) and the constant automation identity (`db.py:30-31`),
  both of which become rule data.

Settling cases (I13): install before any account, connect Microsoft 365
and grant later, then a scheduling message admits a run; grant revoked then
restored, a message during revocation records `not_admitted` and a message
after restoration admits; a rule disabled by a person stays disabled across
a migration re-run and across a grant transition; an install with a ready
grant at migration behaves exactly as today on the existing scheduling
suites.

If this rule cannot be expressed, the design is wrong. It can.

---

## 5. Evaluation semantics

### 5.1 When rules run

A new phase, **evaluate**, runs inside `run_watcher_check` immediately after
`Watcher.check` (between today's steps 4 and 5, `service.py:1441-1454`), on
every pass, under the production check lock. It is followed by a
**dispatch** phase (section 6). Both phases run in the systemd path and the
desktop path alike, because both call `run_watcher_check`
(`cli.py:184`, `engine_api.py:1574`).

Evaluation candidates are messages whose analysis is complete (`status` in
`analyzed`, `summarized`), whose `received_at` is inside retention
(`service.py:1305-1309` applies the same boundary), and whose
`rules_evaluated_version` is null. Each candidate is evaluated exactly
once, ever.

**Which rule versions apply to a message** is decided by two stored
revision numbers, not by timestamps and not by when the pass happens to
run:

- `messages.rules_revision_at_analysis`, captured by `mark_analyzed` inside
  its existing `BEGIN IMMEDIATE` transaction (`db.py:5421`) by reading the
  current `automation_rule_set.revision`; the analysis result and the
  revision it saw commit together;
- `automation_rule_versions.revision` and `retired_revision` (section 4.1).

A rule version applies to a message iff
`revision <= rules_revision_at_analysis < retired_revision` (an unretired
version has an infinite `retired_revision`). Because revisions are assigned
by the database inside serialized write transactions, "before" and
"after" are transaction order, not wall-clock order: equal timestamps and
clock rollback cannot admit a later rule or hide an earlier one. A rule
created after the message was analysed has a revision greater than the
one the analysis captured and cannot fire on it. A rule edited after the
message was analysed is evaluated on the version that was current at the
captured revision, so the edit neither fires retroactively nor loses the
work the earlier version was entitled to. A deleted rule's tombstone
version applies to nothing, and the version it retired applies only to
messages analysed before the deletion. The evaluation reads the version
table once per pass (one read transaction); a version written after that
snapshot has a revision greater than every candidate's captured revision
and cannot apply in this pass.

Consequences that follow from the boundary, not from extra rules:

- **Crash between analysis and evaluation, new rule saved in between.**
  The message stays a candidate; the new rule's `revision` is greater than
  the message's captured revision; it does not fire. The versions that did
  exist at the captured revision fire as they would have.
- **Migration.** Messages analysed before the migration receive
  `rules_revision_at_analysis = 0`, below every rule version's `revision`
  including the seeded rule's, so no rule applies to them and no
  retroactive fire is possible. Their `rules_evaluated_version` is set to `0` by the migration
  ("evaluated under no rules") so they are not re-read every pass. The
  runs the old scheduling path already admitted for them live in
  `automation_runs` untouched.
- **Edits during a pass.** There is no time-based rule; only the formula
  applies. A message analysed after the edit's revision (possible within
  the same pass, because `Watcher.check` runs before the evaluate phase)
  captures the new revision and fires the edited version in that pass; a
  message analysed before it fires the old version. The phase-start
  snapshot must therefore include every version with a revision at or
  below the highest captured revision among the candidates, which the
  one-transaction read guarantees.
- **Re-analysis.** `analysis.requeue` (`engine_api.py:1792`) does not clear
  `rules_evaluated_version`; a re-analysed message does not re-fire, even
  though its `analysis_at` and captured revision move forward.
- **Deletion.** A rule deleted after a message was analysed still fires
  its pre-deletion version on that message when evaluation catches up
  (the work it was entitled to); a message analysed after the deletion
  sees only the tombstone and nothing fires.

In one `BEGIN IMMEDIATE` transaction per message the engine writes the
message's `rules_evaluated_version`, the fire rows -- each with its own
durable `dispatch_request_id` (section 6.1) -- and the first event of each
fire. A crash before that commit leaves the message unevaluated and it is
picked up next pass; a crash after it cannot re-fire because the unique key
in section 5.3 rejects the duplicate. This is the same cannot-lose,
cannot-duplicate property the calendar admission has (section 2.1),
obtained without coupling evaluation into `mark_analyzed`.

### 5.2 Ordering and multiple matches

Rules are evaluated in `(created_at, rule_id)` order. Every matching rule
fires; there is no first-match-wins. A fire is the tuple
`(event_id, rule_id, rule_version, artifact_ref)` where `artifact_ref` is
`(message_id, part_id)` for artifact-consuming actions and null otherwise.

Fires that would perform the **same invocation** share one Connect job.
"Same invocation" is the existing v2 invocation fingerprint, unchanged
(`db.py:1440-1480` at `29fd046`): protocol version 2, provider `app_id`,
`version` and `instance_id`, capability id and version, the input artifact's
media type, byte size, SHA-256, display name and source app id, and the
**canonical parameters**. The first draft's key -- capability id and
version, provider app id, artifact -- omitted provider version and instance
and the parameters, so two rules requesting a parameterised capability
with different parameters (say, two output languages) would have shared
one result for two different requested operations; the review reproduced
that with the extracted helper (`contract_keys_equal: true`,
`existing_fingerprints_equal: false`). Neither current provider exposes
parameters, and the manifest-based action surface (section 4.3) allows
them, so the key must already be complete.

Collapse happens at dispatch admission, not at evaluation: every fire is
its own row with its own `dispatch_request_id`. Whether a fire joins an
existing job or creates one is decided by exactly one procedure, the
**admission decision** in section 6.2 step 4, which this section does not
restate. Discovery and source preparation precede confirmation and
admission. The decision replays the current attempt when already bound;
otherwise it prefers an active job, then the newest completed job, and
otherwise creates a job under the fire's current attempt. Failed history
from another attempt never blocks creation or satisfies a fire. A click
may re-run completed work; a rule joins a completed result when there is
no active match, instead of starting another execution on each pass.

Confirmation does not cross fires. A fire in `awaiting_confirmation` is not
admitted and therefore neither creates nor joins a job; a fire that needs
no confirmation and shares a fingerprint with one that does is admitted on
its own and runs. When the confirmed fire is later admitted it finds the
job by fingerprint as above and links to it -- the person confirmed exactly
this invocation, and it has been, or is being, performed. An auto-admitted
fire never satisfies another fire's unmet confirmation, and an unconfirmed
fire never rides an admitted fire's job. Settled by I18: equal parameters
collapse to one job; unequal parameters produce two jobs on one lane,
serialised by the lane; mixed confirmation produces one running job and
one waiting fire, which links to the same job after confirmation.

Two rules sending the same artifact to two different providers produce two
jobs on two lanes.

### 5.3 No match, and idempotency

No match writes the message's `rules_evaluated_version` and nothing else.
A rule cannot fire twice on the same message: unique index on
`(event_id, rule_id, rule_version, artifact_key)` in `automation_rule_runs`,
where `event_id` is the SHA-256 of `(provider, account_id,
provider_message_id, "mail.message", 1)`, i.e. the same message key the
scheduling admission uses (`db.py:934`), and `artifact_key` is a `NOT NULL`
text column equal to `message_id || "/" || part_id` for artifact-consuming
actions and the sentinel `"-"` otherwise. SQLite treats every NULL as
distinct in a unique index, so a nullable `artifact_ref` would not have
enforced this for `calendar.propose` or `notify` fires; the sentinel makes
the constraint real for both action classes, and I1 tests both.

### 5.4 The generic event

The engine core sees only this record; the mail adapter builds it from the
message row, the attachment descriptors, and the persisted analysis.

```text
Event
  event_id        sha256 (section 5.3)
  source_kind     "mail.message"       the only kind in version 1
  occurred_at     received_at
  scope           { provider, account_id }
  attributes      flat map, keys from the table in 4.2 minus attachment.*
  artifacts       list of { artifact_ref, display_name, media_type,
                            byte_size }     sha256 unknown until fetched
```

The core module imports no mailbox, provider, Connect, or calendar code.
A second source kind is a new adapter and a new `trigger.source_kind`
value; the core does not change. That is the lift-and-shift the prompt
asked for, and it is a testable rule: a test asserts the core's import
graph.

---

## 6. Dispatch, failure, back-pressure, concurrency, gates, handoff

### 6.1 Fire state machine

A fire's state is **stored**, with a monotonically increasing
`state_version`, and every transition is a compare-and-set on that version
inside one `BEGIN IMMEDIATE` transaction, the same discipline the calendar
ledger uses (`docs/CONTRACTS.md:564-573`). The Connect job and the calendar
run are inputs to the fire's transitions, not substitutes for its state:
earlier revisions of this contract derived the fire's state from the job
at read time and then attached obligations to the fire (intents, attempts,
hashes, cleanup) that no transaction owned. This revision fixes that at
the source. The table below is the only normative statement of the
machine; prose elsewhere refers to it.

| From | To | Guard | Written by |
|---|---|---|---|
| (none) | `matched` | rule applies (5.1), candidate authorization passes (6.6) | evaluation transaction |
| `matched` | `not_admitted` | `calendar.propose` readiness refused (4.6) | evaluation transaction |
| `matched` | `submitted` (run) | `calendar.propose` run admitted, `run_id` linked | evaluation transaction |
| `matched` | `completed` | `notify`; its intent written alongside | evaluation transaction |
| `matched` | `pending_dispatch` | `connect.invoke`, always, `confirm_each` or not; initialize `pending_since` | evaluation transaction |
| `pending_dispatch` | `awaiting_confirmation` | manifest effects or `confirm_each`; verified source identity and `item_sha256` persisted | dispatch step 3a |
| `awaiting_confirmation` | `pending_dispatch` | person confirmed that hash; `confirmed = true`, reset `pending_since` | decide operation |
| `awaiting_confirmation` | `declined` | person declined | decide operation |
| `pending_dispatch` | `ambiguous_provider` | two or more validated instances of the pinned app, before capability filtering | dispatch step 2 |
| `pending_dispatch` | `manual_review` | `unsupported_attachment`, `parameters_invalid`, `capability_unavailable`, `provider_changed`, or `stalled` (24 h in the current pending interval) | dispatch steps 2, 3, 3a, 4 |
| `pending_dispatch` | `submitted` (job) | `job_id` linked, created or joined by the single admission decision | dispatch step 4, admission transaction |
| `pending_dispatch` or `awaiting_confirmation` | `source_unavailable` | definitive missing/changed source in preparation, or source removed by cleanup | dispatch step 3, cleanup transaction |
| `submitted` | `completed` | retained linked job `completed`, or linked run `completed` | settlement |
| `submitted` | `failed` | retained linked job `failed` with a code other than `connect_queue_deadline_exceeded` or `connect_source_unavailable`, or linked run `failed` | settlement |
| `submitted` | `pending_dispatch` | retained linked job failed `connect_queue_deadline_exceeded` on attempt 1; open attempt 2 and clear the current `job_id` atomically (Attempts below) | settlement |
| `submitted` | `manual_review` | retained linked job failed `connect_queue_deadline_exceeded` on attempt 2; or linked run `ambiguous` or `manual_review` | settlement |
| `submitted` | `source_unavailable` | retained linked job failed `connect_source_unavailable`, or linked run `source_unavailable` | settlement |
| `submitted` | `source_unavailable` | cleanup removes a linked terminal or never-submitted waiting job; or a source-unavailable job tombstone has a validated terminal outcome and is being removed (6.4) | transaction removing the job, before removal |
| `submitted` | `declined` | linked run `declined` | settlement |
| `submitted` | `source_unavailable` | resumable calendar tombstone expires; close fire before both records are purged | calendar expiry transaction |

Terminal states: `not_admitted`, `declined`, `completed`, `failed`,
`source_unavailable`, `manual_review`, `ambiguous_provider`. A terminal
fire never transitions again. `not_admitted` is recorded once with its
reason, emits no notification, creates no run or job, and is never
retried; a later message fires afresh. All job-outcome rows marked
`retained` require `source_available = true` and no source deletion in
that transaction. Removal has precedence over success, failure and retry:
it uses the removal row, never a completion notice for discarded output.
The two deadline guards are exclusive by attempt number.

A linked calendar run is resumable in `detected`, `extracting`, `proposing`,
`awaiting_confirmation`, `write_authorized`, `writing`, `unresolved` and
`reconciling`. It is final for settlement in `ambiguous`, `manual_review`,
`source_unavailable`, `declined`, `completed` and `failed`
(`db.py:329-333`, `docs/CONTRACTS.md:585-605`). Active jobs and resumable
runs cause **no fire transition, no version increment and no fire intent**.
They are not self-transition rows. A person or reconciliation may advance
the run later; repeated settlement while it waits must be a no-op.

**Display.** A `submitted` fire is displayed from its linked job's
dispatch state (`waiting` shows "Waiting for <provider>", `reconciling`
shows "Reconnecting", `provider_owned` shows "Running", the Inbox fields
of `db.py:5872-5884`) or from its linked run's exact state, including the
run's own `awaiting_confirmation` and `unresolved`. Display reads the
link; it does not change the fire. A run's resumable states are the
run's to leave, through the calendar contract's own confirmation and
reconciliation paths; the fire stays `submitted` until the run is
terminal. Its desktop display covers the resumable handoff; the existing
calendar notices cover only the states listed in section 2.1.

**Settlement.** `settle` advances `submitted` fires using the durable
linked outcome and the disjoint guards above. In one `BEGIN IMMEDIATE`
transaction it compare-and-sets the fire's `state_version` and writes the
associated attempt changes and intents. Connect fires receive the
transition-specific intents subject to the notification deadline in
section 6.4/6.7; calendar fires receive
**no additional fire-owned intent**. Preserve the existing calendar
notification set exactly: `ambiguous`, `manual_review`, `source_unavailable`
(section 2.1); no new confirmation, unresolved or completion notice is
promised for calendar runs. A `notify` fire has already written
its one intent in evaluation and is not settled again. Intent insertion
and attempt creation roll back if the fire CAS loses; uniqueness on
`(fire_id, new_state_version, intent_kind)` and `(fire_id, attempt_no)`
prevents duplicate effects on replay.

Settlement runs at the end of every `connect.queue.pump` call, whoever
issued it (desktop queue thread, pump timer or pass), at the start and end
of every dispatch phase, and after calendar transitions, including human
decisions. It scans resumable links without mutating them, and settles
final links. A crash after a retained job's terminal commit leaves the
fire available to the next scan. **When a linked job is about to be
removed**, the same settlement logic must instead run *inside the removal
transaction*, before deletion, using that transaction's validated outcome
or cleanup decision (6.4); an after-commit scan cannot recover a deleted
row. A standalone pump therefore updates the fire even when the mail
check is not alive. I17/I20 cover both retained and removed dependencies.

**Recovery.** A `submitted` fire resumes through its current `job_id` or
`run_id`, whether created by this fire, another fire or a click. It never
creates another job while that link is current. A `pending_dispatch`
fire has no current `job_id`; it re-enters preparation and the single
admission decision in section 6.2 step 4. A joining fire's request id may
name no job; it does not need one. Admission commits the job, attempt
binding and fire link together, so a crash exposes either all of them or
none. Replaying an already bound attempt uses its existing link and
outcome, not a fresh fingerprint search. A request id from a lower attempt
replays that attempt's stored outcome and never creates work.

**Attempts.** A Connect fire has at most two attempts, each with an
immutable `automation_rule_run_attempts` identity (`attempt_no` and
`dispatch_request_id`). The first identity is created with the fire;
admission records a write-once `job_id` binding for that attempt, including
when it joins a click's job. Only the first-attempt deadline row opens
attempt 2: in the same CAS transaction, retain attempt 1's binding for
audit, create the fresh attempt identity, set the fire's current attempt
to 2, clear its current `job_id`, and set `pending_dispatch` with a fresh
`pending_since`. Repeated
settlement cannot open another attempt. Preparation and admission then
run normally; attempt 2 may join eligible active/completed work that
appeared meanwhile, or create if only failed history remains. The second
deadline row is terminal `manual_review` and never opens attempt 3.

`connect_queue_deadline_exceeded` proves no accepted provider submission,
not that the source is still intact (`docs/CONTRACTS.md:1055-1061`). Every
new attempt rechecks the source under the source lock before admission.
The confirmation receipt remains bound to the original item; a retry
cannot replace it with a newly hashed item. Definitive source loss or
size/digest drift is `source_unavailable`; a temporary mailbox failure
leaves the unbound fire `pending_dispatch` or the existing job `waiting`,
with the same attempt. No other failure opens a new attempt.

**Submission authorization.** `origin` records who created the job
(`automation` with the fire/attempt identifiers, or `interactive`); it is
provenance, not the sole authorization selector. Store an immutable
`admitted_effects` snapshot of both manifest booleans with every new v2
job, and durable authorization receipts bound to its full fingerprint and
that snapshot. An automation receipt identifies its fire and, when
required, that fire's confirmation. An interactive receipt is written
only by the real click path after its existing entitlement and confirmation
checks, whether the click creates a job **or joins an active one**. A fire
joining a click never creates an interactive receipt. Receipts contain
identifiers/hashes and the booleans, not source content.

At each proven-new POST, under the lane/source locks, rediscover the live
manifest and require its effect flags to equal `admitted_effects`. A
valid receipt must cover the exact fingerprint, those flags and any
required confirmation. Effect drift fails the job with
`CONNECT_EFFECTS_CHANGED`, or a missing/invalid receipt with
`CONNECT_ADMISSION_POLICY_REQUIRED`; no automatic retry or confirmation
transfer is allowed. Linked fires follow the failed-job row. This guard
runs in the pump, including the path where GET proves JOB_NOT_FOUND and a
new POST becomes possible; it cannot rely on the fire re-entering dispatch.
GET-only reconciliation of possibly-submitted work still proceeds.

Then authorize through either a valid interactive receipt plus an active
exchange key, or a valid automation receipt plus both active keys. A real
click joining automation work can therefore preserve interactive authority
after Automate expires, without changing the job's origin or identity.
Neither kind of receipt substitutes for a different fire's confirmation
or bypasses source/manifest checks. Admission must reject joining an active
job with a missing or different effects snapshot as `manual_review`
(`provider_changed`)
before binding the fire; completed history still uses the existing full
invocation fingerprint because it causes no new POST.

For pre-migration jobs without this metadata, do not invent an effects
snapshot or confirmation from the job's origin. GET-only reconciliation is
allowed. A fresh, validated click may record the missing metadata for the
same still-active identity; otherwise a proven-new POST fails with
`CONNECT_ADMISSION_POLICY_REQUIRED`. Migration and replay tests must cover
this explicit compatibility boundary. The v2 wire request and invocation
fingerprint remain unchanged; these are consumer-local admission records.

### 6.2 Dispatch phase

Dispatch branches on the action kind, and only `connect.invoke` fires
ever reach the numbered steps below.

- **`calendar.propose`** dispatches inside the evaluation transaction
  itself: the same `BEGIN IMMEDIATE` transaction that writes the fire
  performs today's admission (`_admit_scheduling_automation`,
  `db.py:924-975`, whose `ON CONFLICT DO NOTHING` on the run's unique key
  makes it idempotent) and links the fire to the resulting `run_id` in
  state `submitted`, or records `not_admitted` when the action's readiness
  checks (section 4.6) refuse. The fire then stays `submitted` through
  resumable run states and is settled as specified in section 6.1. This
  preserves the atomic admission the calendar contract requires (`docs/CONTRACTS.md:575-581`).
- **`notify`** also completes inside the evaluation transaction: the
  fire's `automation_review`-class intent row is written with the fire and
  the fire is `completed`; delivery follows the existing path.
- **`connect.invoke`** fires are always created `pending_dispatch`, whether
  or not the rule has `confirm_each`, and are processed by the dispatch
  phase below; a rule-requested confirmation is prepared by step 3a
  exactly like a manifest-required one, so every fire that waits for a
  person waits with a hashed, confirmable item.

For every `connect.invoke` fire in `pending_dispatch`, in `(occurred_at,
message_id, part_id)` order, grouped by resolved provider instance, within
the **phase deadline** (below):

1. re-check both licence features (section 6.6);
2. **discover** the pinned `provider.app_id` and the pinned capability
   through catalog-level discovery, `connect.discover_capabilities()`
   (`connect.py:1138-1149`), which returns every live v2 capability with
   its app id, app version, instance id, capability id and version, and
   accepted media (`connect.py:435-508`). First filter only by the rule's
   app id and count distinct instance ids across **all** its capabilities.
   Only after exactly one instance is established may the engine select
   the pinned capability id/version from that instance's live manifest.
   Thus two instances remain ambiguous even if only one advertises the
   requested capability, and a present app with other capabilities is
   distinguishable from an absent app. The persisted-job helper
   (`_discover_persisted_generic_capability`, `engine_api.py:2839-2870`
   @29fd046) takes a `ConnectJob` and is used only by the pump and
   reconciliation, after a job exists. Outcome map:

   | Discovery outcome | Fire |
   |---|---|
   | no instance of the app | stays `pending_dispatch`; `stalled` to `manual_review` after 24 h from `pending_since` |
   | two or more instances of the app | `ambiguous_provider` (`adr/0001:110-112`) |
   | instance present, capability id or version absent from its live manifest (`capability_unavailable`) | `manual_review` (`capability_unavailable`), notify, no retry; a rule edit produces a new version for later messages |
   | one instance, capability present | continue |

3. **Validate and prepare every source before confirmation or admission.**
   Validate parameters and the stored artifact descriptor against the live
   manifest; reject unsupported artifacts or parameters as `manual_review`
   (`unsupported_attachment` or `parameters_invalid`). Then take the
   existing lane lock followed by the per-message source lock, in the
   same order as a click (`engine_api.py:3279-3284`). If a lock is busy,
   release any acquired lock and leave `pending_dispatch`. Do not wait
   beyond the phase deadline. Under the source lock, recheck retention and
   the current attachment descriptor, fetch through the mailbox gateway,
   verify size, and compute the artifact SHA-256 exactly as the click
   preparation does (`engine_api.py:3285-3312`). This is unconditional,
   including when both effects flags and `confirm_each` are false.
   The verified descriptor, digest and canonical parameters form one
   prepared candidate used by **both** steps 3a and 4. Keep the source
   lock until the waiting-item or admission transaction commits, then
   release all locks; no database transaction spans the mailbox fetch.
   Do not persist attachment bytes. The pump independently re-fetches and
   verifies them at handoff (`engine_api.py:2721-2729`).

   A transient fetch failure leaves `pending_dispatch`; definitive missing
   source or size/digest drift from an already prepared identity uses the
   `source_unavailable` row. Initial preparation has no earlier digest to
   compare. Later preparation must compare against the saved identity
   before replacing anything. Preparation failure never creates or joins
   a job, and never leaves a waiting item without its identity.
3a. **Check confirmation against the prepared candidate.** Compute
   `item_sha256` from that candidate and the resolved provider as in 6.7.
   If already confirmed, compare to the saved confirmation receipt on
   **every** dispatch, even if a new manifest no longer declares effects:
   source drift follows step 3; any other identity mismatch is
   `manual_review` (`provider_changed`). Never overwrite the receipt to
   make it match. Otherwise, when either manifest effect flag or
   `confirm_each` is true, atomically persist the prepared identity and
   item hash, transition to `awaiting_confirmation`, and stop. A person's
   confirmation returns the fire to `pending_dispatch` with that receipt;
   the next dispatch prepares and checks it again. If confirmation is
   unnecessary, continue with the same prepared candidate.
4. **Admit**, the one admission decision (5.2 and 6.1 refer here). Compute
   the complete v2 invocation fingerprint from the prepared candidate
   (`db.py:1440-1480` at `29fd046`). Under the locks held from step 3,
   use one `BEGIN IMMEDIATE` transaction and verify that the fire still
   has the expected version and current attempt. The decision is:

   1. If this attempt already has a `job_id` binding, replay it. If its
      request id already names a job, validate that job's source and full
      fingerprint before binding/replay; a mismatch is an integrity
      error, never permission to create or join unrelated work. Under
      atomic admission, a normal crash cannot leave a created job with
      no attempt/fire link.
   2. Otherwise, among `(message_id, part_id, invocation_fingerprint)`
      matches, join the active job (`requested`, `accepted`, `processing`).
      The active-fingerprint unique index admits at most one
      (`db.py:282-286`). Before joining, enforce the effects-snapshot
      compatibility and receipt rules in section 6.1; incompatible policy
      halts this unbound fire without mutating the active job.
   3. Otherwise join the newest `completed` match, ordered by
      `(created_at DESC, job_id DESC)` so equal timestamps are deterministic.
   4. Otherwise create under the current attempt's `dispatch_request_id`
      with `origin = automation`. Failed rows from other attempts or
      clicks never block creation or satisfy this fire.

   Active work takes precedence over completed history; completed takes
   precedence over failed history. Preserve this ordering even when rows
   appear concurrently: selection, create-or-join, the write-once attempt
   binding, the fire's current `job_id` and `submitted` transition all
   commit together. On queue-cap rejection, leave the same attempt unbound
   and `pending_dispatch`. No provider submission occurs in admission.
   If the joined job is terminal, settle in this pass; completion is
   written only by settlement, not as an extra admission transition.

After the loop, **once per pass**, not per fire:

5. **pump**: call the same `connect.queue.pump` operation the desktop host
   calls (`engine_api.py:3101`; `engine.rs:1066` sends it with
   `limit: 25`). The pump submits or reconciles due lane heads and runs each
   claimed job with `wait_for_terminal=False` (`engine_api.py:3090`): a job
   still active after its submit or GET raises retryable `JOB_TIMEOUT`
   (`:2475-2480`) and the pump moves to the next head. No engine dispatch
   call waits for a terminal state. The engine's own ledger is therefore
   not a second queue: the Connect queue's 25-job cap and two-hour
   admission window (`db.py:33-34`) apply to engine-originated jobs
   exactly as to clicks, and fires that cannot be admitted under the cap
   stay `pending_dispatch` for the next pass. Every pump call ends with
   settlement (6.1).

The first draft's step 5 entered the click path, which "submits and waits
for a terminal state synchronously" (`engine_api.py:2487`,
`client.wait_for_terminal`), with a ten-minute budget checked only before
each submission. That never bounded the pass: the nested wait had its own
30-minute job timeout (`connect.py:53`, `DEFAULT_JOB_TIMEOUT_SECONDS`), so a
submission allowed at second 599 could hold the pass until second 2399.
The pump exists precisely so that nothing waits, and the engine uses it.

**Phase deadline.** The engine computes `deadline = evaluate_start + 10
minutes` once, when the evaluate phase begins, after `Watcher.check` has
returned. Every blocking operation in the evaluate and dispatch phases --
attachment fetch, lock acquisition, provider submit and GET inside the
pump, the pump call itself -- receives the remaining time to that deadline
as its timeout, and an operation whose minimum duration would not fit is
not started. When nothing remains, nothing more is dispatched and every
unbound fire stays `pending_dispatch`, which is a safe resumable state
because admission is durable and idempotent.

The evaluate phase's own CPU and SQLite work is bounded the same way, not
only its external calls. Bounds: an install holds at most **100 rules**
(tombstones excluded; validation rejects the 101st); a rule has at most 8
conditions and a message at most 64 attachments, so matching one candidate
is at most 100 x 8 x 64 comparisons over persisted fields. Candidates are
read in **chunks of 50** ordered by `analysis_at`; the version snapshot
for a chunk is built from the chunk's **distinct captured revisions**, at
most 50. For each such revision `r`, the rules in effect at `r` are
enumerated from the version table itself, not from the current rules
list, so a rule deleted after a candidate's analysis still fires the
version it had at that analysis (4.1, 5.1): the query is
`kind = 'definition' AND revision <= r AND (retired_revision IS NULL OR
retired_revision > r)`, and its result is at most 100 rows because the
100-rule cap held at every past revision, tombstones included at the time.
The rows the query must examine are those versions plus every version
written after `r` (an index on `retired_revision` makes the retired-before-`r`
majority unreachable). The number of writes since a candidate's analysis
is not bounded by design, so the statement is bounded by time instead:
every statement the evaluate phase runs is registered with SQLite's
progress handler, which aborts the statement when the phase deadline has
passed; an aborted snapshot or candidate transaction rolls back, the
candidate keeps its null marker, and the next pass, whose deadline is
fresh, evaluates it first. I7 exercises this with a history of writes
larger than the budget. Compacting retired versions that no unevaluated
message can still need is deferred (section 9). A rule is excluded at `r` only when the version
in effect at `r` is a disabled one; enabling and disabling are version
rows (4.1), so there is no current flag and no exception to the formula. A chunk spanning revisions 1 and
1,000,000 therefore examines the versions active at each of the two, plus
the writes since, never the million between. The deadline is checked before each chunk read and before each
candidate's transaction begins; when it is exhausted the phase stops, and
a candidate whose transaction has not begun keeps its null
`rules_evaluated_version` and is picked up first next pass. Nothing is
lost by stopping, because evaluation is durable per candidate.

The guarantee is scoped, and this contract says so plainly: the two new
phases add at most ten minutes to a pass. The work that precedes them --
scheduling writes, extraction and proposals, mailbox polling, and model
analysis (`service.py:1438-1448`) -- is unchanged by this contract and has
no deadline today; the engine does not propagate cancellation into it,
because changing the mail pass's own bounds is a separate contract. A
pass can therefore still reach the desktop scheduler's 30-minute engine
timeout (`scheduler.rs:17`) through the pre-existing phases, as it can
today, and this contract does not claim otherwise. What it claims, and
what I7 settles with a simulated clock, is that a slow provider or a slow
mailbox inside the new phases cannot hold them past their ten minutes.

**What the pass does not do.** It does not wait for jobs it admitted.
With the desktop open, the host's queue thread pumps them at the
engine-supplied wake times (C1). Without a desktop, the dedicated pump
timer of section 6.5 pumps them between mail checks; only if that timer
is unavailable or disabled is the next mail-check pass the next pump, and
then the two-hour admission window can expire a `waiting` job first; `connect_queue_deadline_exceeded` proves
the provider never accepted it (`docs/CONTRACTS.md:1055-1061`,
`1156-1158`), so settlement returns the fire to `pending_dispatch` and
opens its second attempt with a fresh `dispatch_request_id` in that same
transaction (section 6.1); the first attempt's request id remains bound to
its failed job and replays that failure forever. A second such failure
settles to `manual_review`. Fires not reached stay
`pending_dispatch`; jobs left `provider_owned` or `reconciling` are
reconciled by the next pump, GET-before-POST by construction
(`engine_api.py:2813-2830`).

`PROVIDER_BUSY` during the pass, which can only come from a user click that
won the lane in between, is handled by the existing deferral
(`engine_api.py:2526-2540`); the pump walks the backoff schedule.
An unbound fire retries transient dispatch obstacles on later passes.
Its stall clock is the persisted `pending_since` of its **current**
`pending_dispatch` interval, not its original match time. Initialize that
clock when evaluation first enters `pending_dispatch`; reset it, together
with `authorized_pending_seconds` and the `last_authorized_at` baseline, in
the same transaction when a person confirms a waiting item or settlement
opens attempt 2, so a fresh pending interval starts with zero authorized
time. Retries while already pending (provider absence, temporary
fetch failure, lock contention or full queue) do not reset it. An inactive
licence **pauses** it, and the pause is anchored to the licence, not to
when the engine happened to notice. The fire accumulates
`authorized_pending_seconds` and records `last_authorized_at` together
with `last_authorized_entitlement_id` at every dispatch step 1 that
passes. At such an observation, the interval since `last_authorized_at`
is added **only if** the licence now active carries the same
`entitlement_id` as at the previous observation: a licence's validity is
one contiguous interval (`issued_at <= not_before <= now < expires_at`,
`adr/0003:61-65`), so a licence active at both ends of a gap was active
throughout it. If the entitlement id differs, or there was no previous
authorized observation, nothing is added and the observation is recorded
afresh. A failed step 1 adds nothing and records nothing. A licence that
expired one minute after a pass and was replaced thirty hours later thus
contributes zero authorized time to the gap, whenever the engine observes
it. Re-installing a licence with the same entitlement id after removing
it counts the gap as authorized; that is the person restoring the same
rights and is accepted. This is what makes the taxonomy
row "licence not active at dispatch: stays `pending_dispatch`, resumes
when active" true without a contradictory terminal rule; previously
admitted work resumes, while a candidate that was evaluated `locked`
never fires (6.6). When `authorized_pending_seconds` reaches 24 hours without
admission, transition to `manual_review` (`stalled`). `pending_since`
remains the display anchor of the current interval; the stall decision
uses only the accumulated authorized time. Time spent in `awaiting_confirmation` or
`submitted` does not consume a later pending interval. Confirmation does
not extend source retention or permit changed bytes: source preparation
and the saved receipt must still pass before admission.

### 6.3 One dispatch design for both hosts

The first draft carried a "current world (no host pump)" design and a
"post-queue world (host pump wired)" design. #123 made the second the only
world: the desktop host pumps at engine-supplied wake times, and the engine
pass pumps within its deadline. There is one dispatch design (section 6.2)
and two hosts:

- **Desktop running.** The pass admits and pumps once within its deadline;
  the host's queue thread pumps again at the engine's `next_wake_unix_ms`
  and re-reads durable state (`scheduler.rs:91-101`). Nothing in the pass
  depends on the host, and the host reproduces no queue policy.
- **Timer-driven install, no desktop.** The pass admits and pumps once
  within its deadline, and the dedicated pump timer of section 6.5 owns
  progress between passes: it runs `eom-mail-watch pump` at a cadence
  inside the admission window, so a `waiting` or `provider_owned` job
  advances after the mail-check process has exited, without waiting for
  the next mail check. The mail check is not the only pump on this host.

Both hosts run the same tests (section 7); the tests that distinguish them
are the wake-time tests (host present: a `waiting` job advances before the
next pass through the host thread; host absent: it advances before the
next pass through the pump timer, and if that timer is disabled it
expires under the admission window and is handled as section 6.2 says).

### 6.4 Failure taxonomy

The following Connect notification entries apply before the notification
deadline defined below; expiry transitions are silent.

| Outcome | Fire state | Person told? | Retry |
|---|---|---|---|
| provider absent, no job yet | `pending_dispatch` | after 24 h, as `manual_review` (`stalled`) | every pass until then |
| two instances of the pinned app | `ambiguous_provider` | yes | none; person picks by clicking |
| pinned capability or version gone from the live manifest | `manual_review` (`capability_unavailable`) | yes | none; a rule edit applies to later messages |
| artifact not accepted by live manifest | `manual_review` (`unsupported_attachment`) | yes | none |
| parameters rejected by the live manifest | `manual_review` (`parameters_invalid`) | yes | none; a rule edit applies to later messages |
| provider version or instance changed after confirmation | `manual_review` (`provider_changed`) | yes | none; person may confirm again by clicking |
| `PROVIDER_BUSY` (retryable) | `submitted` / job `waiting` | no | queue backoff |
| ambiguous POST outcome | `submitted` / job `reconciling` | no | GET before POST, existing |
| non-retryable refusal, or job `failed` | `failed` with the provider's bounded `code` and `message` | yes | none; a person may click |
| Automate key inactive at a proven-new POST with automation-only authority | job failed `CONNECT_AUTOMATIONS_ENTITLEMENT_REQUIRED`; fire `failed` | yes | none; resumes for later messages when active |
| admission deadline before any POST (`connect_queue_deadline_exceeded`) | `pending_dispatch` with a second attempt; `manual_review` on the second failure | on the second | one |
| source gone or changed | `source_unavailable` | yes | none |
| licence not active at dispatch | fire stays `pending_dispatch`, evaluation continues recording `locked` outcomes | rules panel shows locked | resumes when active |
| completed `connect.invoke`, including output with withheld fields | `completed` | yes, one `automation_complete` intent | none |

"A result that comes back withheld" is a completed job whose output carries
withheld fields (Invoice Processor's contract). The engine does not parse
`application/vnd.local-connect.invoice+json`; unknown media types are
opaque by contract (`adr/0002:108-111`).

**Intent content and lifetime.** A fire-owned intent stores **no
message-derived content**: only `fire_id`, `intent_kind`, the fire's
`state_version` at creation, `notification_deadline`, and timestamps. The
sender label and subject a notice shows are read from the live `messages`
row at delivery time, exactly as the existing intent UNION already reads
them for analysis and calendar notices (`db.py:5396-5420`); the rule name
is read from the rule version. Consequently, when `inbox.delete`,
`inbox.clear`, or retention removes the message, there is nothing
message-derived left in the outbox to send: the fire's intents are deleted
in the same transaction as the message row, and no new intent is written
for a fire settled by that removal. This keeps the existing privacy
contract that deletion removes message-owned notification state and
retains no sender or subject (`docs/ENGINE_API.md:247-254`); the fire
outbox is subordinate to the message row, not a second copy of it.

`notification_deadline` is the source's configured retention cutoff,
persisted on the fire and its intents so no source text is needed to
evaluate it later. Before the cutoff, a transition writes its required
intent atomically and retains it for delivery/retry until acknowledged,
superseded (below), or expired. At/after the cutoff, transitions are
silent. Delivery rechecks the deadline and the live message row and
discards an intent that fails either.

**Supersession and delivery claim.** Every fire transition is a
compare-and-set on `state_version`. The same transaction marks every
undelivered intent of that fire whose `state_version` is lower as
superseded. Delivery of a fire intent runs under the host operation lock
that native notification delivery already holds across selection, platform
delivery, and acknowledgement (`docs/ENGINE_API.md:88-94`,
`notifications.pending_under_host_lock`), and the human-decision operation
`automation.rule_run.decide` acquires that same native lock before its
transition, as `inbox.delete` acquires the production lock before a
deletion. Under the lock, delivery re-reads the intent and delivers it only
if the fire's current `state_version` equals the intent's; a decision
cannot interleave between that check and the acknowledgement, because it
waits for the lock. An `awaiting_confirmation` request whose delivery was
delayed and whose fire has since been confirmed, declined, completed, or
failed is therefore never sent; a delivered (acknowledged) intent is
history and is not retracted. The timer-driven pass delivers through the
same lock (`notifications.pending` acquires it itself). Calendar notices remain governed by their existing ledger,
not this fire outbox.

**Completion intent.** Before that deadline, a completed `connect.invoke`
fire has exactly one intent of kind `automation_complete`, written by the settlement
transaction that moves the fire to `completed` (section 6.1), whether the
fire's own attempt created the job, the fire joined a job, or the job was
already `completed` at admission. It travels the same durable intent table
and delivery path as `automation_review` (`db.py:5396-5420`,
`service.py:1461-1470`), honours the same opt-in phone setting, and is
rendered at delivery as the rule name, the live message's sender label and
subject, and the word "completed"; it never includes provider output text, so this contract adds no new content class
to the phone channel beyond what `send_review` already carries
(`service.py:1025-1032`). A completed `calendar.propose` fire adds no
intent of its own: preserve the calendar ledger's actual notification
set in section 2.1, which does not include completion. A completed `notify` fire's only intent is the one the action
exists to send, written with the fire in the evaluation transaction.
`not_admitted` and `declined` create no intent.

**Source cleanup.** Retention purge, `inbox.delete` and `inbox.clear`
hold the existing per-message source lock before their deletion
transaction (`docs/CONTRACTS.md:1147-1160`). That same transaction handles
all linked fires before removing their dependencies:

- Every undelivered fire intent of the message is deleted; a removal
  transition writes **no** intent, because the message it would describe
  is being removed (the person deleted it, or retention expired it), and
  the outbox holds nothing message-derived to send in any case.
- An unbound Connect fire (`pending_dispatch` or `awaiting_confirmation`)
  becomes `source_unavailable`, silently.
- For a `submitted` Connect fire, the removal row has precedence whenever
  cleanup will delete its job: this includes both never-submitted waiting
  jobs and **already terminal jobs not yet settled to the fire**. Transition
  it to `source_unavailable`, silently, before the existing trigger removes
  the job (`db.py:296-317`). Do not open a retry or create a completion
  notice for a result being removed.
- If Connect may own an active job (`dispatching`, `reconciling` or
  `provider_owned`), retain its content-free tombstone and leave the fire
  `submitted`. On a later validated terminal response, the transaction
  removing that tombstone first settles all linked fires to
  `source_unavailable`, silently (the message is gone), with no persisted
  late result and no completion intent (`db.py:3707-3716`, `docs/CONTRACTS.md:1172-1176`). This includes
  failure outcomes as well as completion. Settlement after deletion is
  insufficient because there is no row left to inspect.
- For a `calendar.propose` fire, apply the calendar ledger's existing
  cleanup/compaction rules, then settle from its resulting state within
  the transaction. A resumable run remains linked and `submitted`, while
  a final run maps through 6.1. Calendar notifications remain owned by the
  run, with no duplicate fire intent. If its reconciliation tombstone
  expires while still resumable, close the fire as `source_unavailable`
  before purging both at the same cutoff; expiry does not authorize new
  work or another notification (`docs/CONTRACTS.md:635-652`).

Already-terminal fires never transition again. Fire and attempt records
retain only identifiers, hashes, states and timestamps, not source text,
attachment bytes or result content. Retain them for the source's configured
eligibility window and purge them at its cutoff, together with any expired
calendar tombstone. A nonterminal fire whose Connect tombstone still owns
an unresolved request remains a bounded content-free reconciliation record
until that dependency is terminal; it is never dispatchable again after
source loss. This does not extend retention of source or result content.
Deletion cannot cascade away a nonterminal fire while leaving work eligible.

### 6.5 Why the engine pumps inside the timer-driven pass

Because the timer-driven install has no other pump. At `c10a37c` the unit
runs `eom-mail-watch check` every two hours after boot
(`systemd/eom-email-watcher.service:11`, `eom-email-watcher.timer:5-6`),
and neither `cli.py` nor `service.py` references `connect.queue.pump`; only
the desktop's `ConnectQueueScheduler` does (C1). `run_watcher_check`
calling the pump is the only design in which "it did the work while you
were asleep" is true for the timer-driven install.

It is not sufficient on its own, and this contract does not claim it is: a
two-hour timer cannot walk the queue's own two-hour admission window
(`db.py:34`) for a job it left `waiting`, and one outstanding job per lane
does not stop a waiting job's deadline from expiring between passes. So the
timer-driven install additionally gets:

- a `eom-mail-watch pump` CLI subcommand that runs one bounded pump call
  under the pass deadline rule of section 6.2 and exits with the engine's
  `next_wake_unix_ms` in its output;
- a second user timer, `eom-email-watcher-connect-queue.timer`, that runs
  it. Its cadence is an operator decision recorded at implementation;
  the admission window bounds the longest useful interval, and the pump is
  a no-op (one read) when no queue is active.

With the desktop open, the pass's pump and the host's pump are the same
operation on the same durable state and contend only on the lane lock
(`engine_api.py:3097`, `lock_contended`); neither waits for the other.

### 6.6 Entitlement enforcement

Where the gate is checked:

1. inside every per-message evaluation transaction, immediately before
   that message's fires are written: `feature_entitlements_active` reads
   and verifies the installed file on each call (`entitlement.py:213-226`),
   so the check is per candidate, not per phase. If either feature is
   missing the candidate is still marked evaluated with outcome `locked`
   and no fire is created (so activating the licence later does not fire
   rules on the whole retention window). This is the authorization
   boundary for the message's candidate batch: once the check passes,
   the bounded, database-only writes in that transaction are authorized.
   There is no network/model work between that check and commit. A
   `notify` intent or `calendar.propose` admission belongs to that batch;
   expiry before the next candidate check stops the next batch, while
   expiry after an authorized batch's check does not revoke its commit.
   I3 tests this exact boundary; later calendar writes still use the
   calendar contract's own authorization and confirmation gates;
2. immediately before each dispatch (section 6.2 step 1), both features,
   through `feature_entitlements_active(CONNECT_FEATURE_ID,
   AUTOMATIONS_FEATURE_ID)` (`entitlement.py:280-281`), the same call the
   service loop makes (`service.py:112`);
3. inside enqueue, the exchange feature again (`engine_api.py:3231`),
   and at every proven-new POST in the pump (`3029-3039`), using the
   snapshot/receipt rules in section 6.1. Automation-only authority needs
   both keys; independent interactive authority needs exchange. An
   automation job with no valid interactive receipt fails with
   `CONNECT_AUTOMATIONS_ENTITLEMENT_REQUIRED` when Automate is inactive,
   through `_fail_generic_connect_record` (`3031-3037`); settlement marks
   its fires failed. A validated click that joined it supplies independent
   interactive authority for that same job. GET-only reconciliation
   continues after entitlement loss (`docs/CONTRACTS.md:1120-1125`);
4. at every human decision on a fire (section 6.7), both features
   (`service.py:947` pattern).

What an unlicensed person sees: the rules panel is visible, rules can be
authored and saved (they are local data), and the panel header reads
**"Automations locked"** with the same affordance as "Connect actions
locked" (`main.ts:1474-1478`). The enabled toggle is rendered inert. A
rule whose action is `connect.invoke` additionally shows "Connect actions
locked" when only the exchange feature is missing, because the two keys are
independent (`docs/CONTRACTS.md:137-142`). Previews of pending fires are
hidden the way proposal previews are (`engine_api.py:1599-1612`); completed
and failed fires stay visible after entitlement loss, as writes do.

### 6.7 Human handoff and confirmation

For a `connect.invoke` fire, each transition into `awaiting_confirmation`,
`ambiguous_provider`, `manual_review`, `failed`, or `source_unavailable`
writes one `automation_review` intent before its notification deadline,
**except the removal transitions of section 6.4 source cleanup**, which
write none because the message they would describe is being removed;
expired transitions are likewise silent as specified in section 6.4. The intent is
written in the transaction that makes the transition
(section 6.1 table, "Written by" column), delivered through the same
UNION the calendar runs use (`db.py:5396-5420`), by the same code
(`service.py:1461-1470`, `1013-1045`), and acknowledged the same way
(`db.py:5473-5490`). The intent row holds identifiers and the state
version only (6.4); at delivery it is rendered as the rule name, the live
message's sender label, and its subject, nothing else, and it is delivered
only while it is the fire's current intent (6.4, supersession).

A prepared Connect fire moves from `pending_dispatch` to
`awaiting_confirmation` when the live manifest declares `effects.external` or `effects.confirmation_required`
for the selected capability, or when the rule author set
`confirm_each: true` (an optional rule field, default false). A person
confirms one specific fire by
`(fire_id, state_version, item_sha256)` where `item_sha256` binds the
full invocation fingerprint (including source/artifact identity, provider
app id/version/instance, capability id/version and canonical parameters)
**plus both** `effects.external` and `effects.confirmation_required` booleans.
This is a consumer-local confirmation hash; it extends the existing
fingerprint (`db.py:1461-1479`) without changing that fingerprint or the
wire protocol. Provider identity remains significant because the
existing client already treats an app-version mismatch as a different
capability identity (`connect.py:1713-1723`); the engine operation
`automation.rule_run.decide` mirrors `calendar.automation.decide`
(`db.py:4277-4284` for the compare-and-set shape). If the resolved
instance **or app version** at dispatch differs from what was confirmed,
the fire halts in `manual_review` with `provider_changed`; it does not
re-resolve, so an upgrade between confirmation and dispatch can never
execute under a confirmation the person gave to a different version. A confirm
is single-fire and non-transferable; repeated clicks return the existing
result.

Neither current provider declares effects, so in version 1 nothing a rule
can do leaves the machine or has an outward effect without a person
confirming a specific item, **with one opt-in exception**: the review and
completion notifications of section 6.4 go out through the existing phone
channel when a person has enabled it, and they carry the rule name, the
sender label and the subject -- outbound content, the same class
`send_review` already sends (`service.py:1025-1032`), never provider
output. Otherwise `calendar.propose` ends in the calendar ledger's own
`awaiting_confirmation`, `connect.invoke` hands bytes to a loopback
process with no external effect, and `notify` is that same notification
class.

### 6.8 Concurrency model

Stated explicitly because it is a known blind spot.

- **Processes.** Four actors can touch Connect state: the systemd check,
  the desktop's scheduled check, a desktop click, and -- since #123 -- the
  desktop's `email-watcher-connect-queue` thread, which calls the pump at
  engine-supplied wake times (`scheduler.rs:91-101`). The pump takes the
  lane lock per head and returns `lock_contended` instead of waiting
  (`engine_api.py:3097`), so a host pump that meets a pass, or a pass that
  meets a host pump, skips that lane for this call; durable state is the
  only shared truth and neither actor holds a lock across calls. The two checks are
  mutually excluded by `<database>.check.lock` (`cli.py:74`,
  `engine_api.py:270`); the click is not, and contends only on the lane
  lock (`engine_api.py:2889-2892`) and the source lock (`3279-3284`). The
  engine adds no lock: it runs inside the check and reuses both.
- **Within a pass.** Single-threaded. The evaluate phase is a sequence of
  per-message transactions; the dispatch phase is a sequence of
  per-fire submissions. "Fan out across provider instances" means
  interleaved sequential submissions to different lanes within the budget,
  not threads.
- **Two rules, one message.** Two fires; same artifact and same capability
  collapse to one job (section 5.2); different capabilities on the same
  artifact are two jobs, possibly on one lane if one provider exposes both,
  in which case the lane serializes them.
- **One rule, a batch.** N fires, one lane, admitted into the durable
  queue in `(occurred_at, message_id, part_id)` order up to the 25-job cap
  (section 6.2 step 4); the lane serialises them and each pump advances the
  head. The order is durable and survives restart because it is computed
  from stored columns, not from memory.
- **A pass starting while the previous pass's jobs are outstanding.**
  Cannot overlap (check lock). Outstanding means `provider_owned` or
  `reconciling` rows at the budget boundary; the next pass reconciles due
  heads before submitting (section 6.2). A click that ran between passes
  and left a job `waiting` is advanced by the same pump call, which is the
  first time a click's busy refusal has been retried without a second click
  (C1).
- **A click during a pass.** The click and the pass contend the lane lock;
  since #123 the losing click returns the job's durable active state
  (`engine_api.py:2905`, `2934`) with a durable job row, and the pump
  advances it. `connect_job_in_progress` is raised only when no active job
  row exists for the request (`2936`) or the job changed during
  reconciliation (`2863`).
- **Rule edits during a pass.** Snapshot at phase start (one read
  transaction); which messages the edit applies to is decided by revision
  (section 5.1), not by when the pass ran. Edit operations do not take the
  check lock and therefore never wait on a thirty-minute pass.
- **Cleanup racing dispatch.** Inherited: source lock ordering is lane then
  source for a pump, source only for cleanup (`docs/CONTRACTS.md:1147-1160`).
  The engine adds no lock and cannot create a cycle.

---

## 7. Invariants and the tests that settle them

Stated as things that must never happen, each with the evidence that
proves it does not.

| # | Never | Settling evidence |
|---|---|---|
| I1 | A rule fires twice on one message and artifact, or the database admits a duplicate fire row. | Unique index on `(event_id, rule_id, rule_version, artifact_key)` with `artifact_key NOT NULL` (section 5.3). Database-level: a direct second insert of a `calendar.propose` fire and of a `notify` fire for the same `(event, rule, version)` fails on the sentinel key; a direct second insert of a `connect.invoke` fire for the same artifact fails; an insert with a NULL key fails; a control insert for a distinct valid artifact of the same message succeeds. Engine-level: the same message evaluated in two passes and after a crash injected between evaluation commit and dispatch yields one fire per key. |
| I2 | A rule version applies to a message outside its `[revision, retired_revision)` window, or an eligible earlier version is lost. | Test creates a rule after analysis, runs a pass, asserts no fire; edits a matching rule after analysis, asserts the message fires on the pre-edit version and not the edit; deletes a matching rule after analysis, asserts the pre-deletion version fires and a message analysed after the deletion does not; crash injected between analysis commit and evaluation with a new matching rule saved in between, asserts no fire from the new rule and the expected fire from the old; migration over retained analysed messages asserts zero fires, `rules_revision_at_analysis = 0`, and `rules_evaluated_version = 0`. Clock probes: a rule saved with the system clock set earlier than the analysis timestamp still does not apply (revision order wins); an analysis and a rule write with identical timestamps resolve by revision. Disable then evaluate: a rule enabled at analysis, disabled before evaluation; assert the version in effect at the captured revision fires; a message analysed after the disable does not; re-enabling writes a version and applies only to later analyses. |
| I3 | A candidate batch is authorized, or an automation job is newly POSTed, without both features active at its authorization boundary. | Matrix over (none, exchange only, automations only, both) times each action kind: only both authorizes a candidate batch. With a simulated clock, authorize at expires_at minus one second and reject at expires_at. Two messages in one phase with expiry between their checks: first batch commits; second records locked, including notify. Expiry after a batch's check but before its database-only commit does not revoke that batch (6.6); no blocking provider work is allowed there. Licence pause: a pending_dispatch fire observed authorized at T0, the licence expiring at T0 plus one minute, no pass until T0 plus 30 hours (step 1 fails), a new licence installed and observed at T0 plus 31 hours; assert authorized_pending_seconds is zero, the fire is still pending_dispatch (not stalled) and dispatches on that pass; the same sequence with the original licence re-installed (same entitlement id) counts the gap; a candidate evaluated locked during the gap never fires. Admit an automation job with both keys, revoke Automate before a later pump in a separate desktop/timer process: zero POSTs, CONNECT_AUTOMATIONS_ENTITLEMENT_REQUIRED, one failure intent; an exchange-authorized click job still POSTs. Mixed authority: automation creates a waiting job, a real validated click joins the same job_id, then Automate expires; exchange plus the persisted interactive receipt permits one POST. Control: fire-only joins do not create interactive authority and fail after Automate expiry. Exchange loss blocks both. Repeat a click-origin job joined by a fire. Receipt identity/effects mismatches cannot authorize. |
| I4 | A capability bypasses required confirmation, or a waiting fire lacks a verified source identity. | Cross confirm_each true/false with effects (neither, external only, confirmation_required only, both). Every case fetches and hashes before step 3a; only the false/neither case proceeds without waiting. Other cases persist a confirmable item with the real artifact digest. Stale confirmation hash is refused; valid confirmation resumes. Before redispatch, change same-sized bytes, provider version, instance or effect flags: source drift is source_unavailable; provider identity drift is provider_changed; a removed effects flag never skips comparison to the saved receipt. Transient fetch failure leaves pending_dispatch without a waiting item or job; definitive missing source closes it. Non-Connect confirm_each is rejected at save. A person confirms after more than 24 hours while the source is still retained: pending_since resets and dispatch succeeds if otherwise eligible. At pending_since + 24 hours, continuous provider absence stalls; an ordinary retry before that boundary never resets the clock. Repeat pending-clock reset for attempt 2; expired source remains source_unavailable despite confirmation. Effect drift after enqueue: hold a no-effects job waiting, change either manifest flag without changing app/version/instance, and pump in a separate process. Assert CONNECT_EFFECTS_CHANGED and zero POSTs; matching flags/receipt authorize normally. Repeat after GET returns JOB_NOT_FOUND. Missing legacy policy metadata cannot authorize a new POST; fresh validated click replay may attach it to the same active job; GET-only recovery remains allowed. |
| I5 | Bytes are sent that differ from the bytes that were queued. | Inherited (`engine_api.py:2721-2729`); test changes the attachment between enqueue and handoff through the engine path and asserts `connect_source_unavailable` and no POST. |
| I6 | An engine dispatch call waits for a provider's terminal state, or the engine reproduces queue policy outside the pump. | Test with three PDFs, one provider, a provider stub that completes slowly; assert the pass admits all three under the cap, makes exactly one submit for the lane head, returns without any `wait_for_terminal` call (spy on `ConnectV2Client.wait_for_terminal`), and leaves the other two `waiting` in the durable queue; a host pump at the returned wake time advances the next. |
| I7 | The evaluate and dispatch phases run past their deadline. | Simulated clock and a deadline propagated to every blocking call in the two new phases. Pre-submit timeout: a gateway whose attachment fetch exceeds the remaining time; assert no POST occurred, the job (if already admitted) is `waiting` with `CONNECT_SOURCE_TEMPORARILY_UNAVAILABLE` (`engine_api.py:2708-2716`) or the fire is still `pending_dispatch` if not, and the phases return by the deadline. Ambiguous POST: a provider stub that accepts the bytes and never answers; assert the submit is cut at the deadline, the job is `reconciling` (or `provider_owned` if acceptance was observed), never `waiting`, and the next pump issues GET for the same request identity before any POST is permitted. Slow mailbox: a gateway whose fetch consumes the whole deadline; assert nothing is submitted, every unbound fire is `pending_dispatch`, and the phases return by the deadline. Evaluation work: 100 rules each with 200 versions and 500 unevaluated candidates under a simulated clock that advances per candidate; assert the phase returns at the deadline, the chunk snapshot query touched at most one version per rule per captured revision, every candidate whose transaction had not begun still has a null `rules_evaluated_version`, and the next pass evaluates them first. Validation: the 101st rule is rejected. The pre-existing mail phases are outside this invariant by section 6.2. Progress-handler abort: 100,000 rule writes after a candidate's analysis under a simulated clock that expires mid-query; assert the snapshot statement aborts, the candidate keeps its null marker, nothing is written, and the next pass evaluates it. |
| I8 | Discovery chooses an instance by filtering away a conflicting instance, or confuses an absent capability with an absent app. | Two app instances both offering the pinned capability, and two where only one offers it: ambiguous_provider in both, no job. One instance with multiple capabilities counts once. One instance offering only other capabilities: capability_unavailable; no instance of the app: pending_dispatch. A changed unique instance after confirmation: provider_changed, no job. Exercise catalog discovery with real manifest fixtures, not a synthetic persisted job. |
| I9 | An invalid rule is partially applied or evaluated. | Every rejection in section 4.4 has a test asserting the write returns an error, the prior version is unchanged, and a corrupted stored definition loads as `invalid` and is skipped without failing the pass. Both error directions: a rule with exactly eight conditions saves; nine does not; a 16-key parameters object saves; 17 does not. |
| I10 | Provider output text leaves the machine in a notification. | Test captures the ntfy payload of the `automation_complete` intent for a completed `connect.invoke` fire whose output carries withheld fields, and asserts it contains rule name, sender label, subject, the word "completed", and no substring of the job's output; with the phone topic unset, asserts no network call and a desktop-only delivery. |
| I11 | The engine core imports mail, provider, calendar, or Connect code. | Test walks the core module's import graph and asserts the allowlist. |
| I12 | Notification ownership is duplicated, a transition's required intent is missing, an obsolete intent is delivered, or an intent carries message content. | For each Connect transition before its notification deadline to awaiting_confirmation, ambiguous_provider, source_unavailable, manual_review or failed, assert one automation_review intent keyed by the new state version, even under CAS replay; assert the intent row stores no sender, subject, or rule text. A Connect completion before its notification deadline, including a joined completed job, adds exactly one automation_complete and no new automation_review. Supersession under the lock: a delivery holding the host operation lock past its version check and a concurrent decide operation; assert the decision waits for the lock, the delivery either completes and acknowledges before the decision or observes the superseded row and sends nothing, and no obsolete request is ever handed to the platform channel; an already-delivered intent is untouched. A notify fire has exactly its action intent. Calendar fires add no intents beyond the linked run's own notifications. not_admitted, declined, and every removal transition add no intent. Existing delivery and opt-in settings apply. |
| I13 | The seeded scheduling rule behaves differently from today, or its enablement depends on setup order. | The existing scheduling tests (`tests/test_scheduling.py`, `tests/test_service.py` automation cases) pass unchanged with the trigger replaced by the seeded rule. Lifecycle: migrate with no account, connect and grant later, assert the next scheduling message admits a run; revoke the grant, assert `not_admitted` and no run; restore it, assert admission resumes; a person disables the rule, re-run the migration and flip the grant, assert it stays disabled and admits nothing; re-enable, assert admission. |
| I14 | A rule reads message bodies. | The event builder's inputs are asserted by type: message row, attachment descriptors, analysis fields; a test asserts `gateway.content` is not called during evaluation. |
| I15 | Cleanup deletes a dependency before settling its fire, permits a later POST, retains source/result content, or leaves message-derived notification content deliverable. | Race inbox.delete, inbox.clear and retention against unbound preparation, waiting confirmation, a never-submitted job, retained completed/failed jobs before settlement, and possibly-submitted jobs. Source lock winner determines ordering. Unbound/removed-job fires become source_unavailable in the removal transaction with no intent; every undelivered intent of the message is deleted in that transaction; a delivery attempt after removal finds no message row and sends nothing (assert zero network calls). Possibly-submitted jobs retain content-free tombstones and submitted fires; late completed AND failed outcomes settle all linked fires before atomic job deletion, silently, with no stored late output. Repeat with multiple fires joined to one job. Calendar cleanup covers awaiting_confirmation, unresolved, terminal and expired-tombstone runs, using the calendar ledger's own rules. Fire and attempt records are content-free and are purged at the source's retention cutoff, except a fire whose Connect tombstone is still `dispatching`, `reconciling`, or `provider_owned` (a pump that died after persisting `dispatching` is recovered as `reconciling` by the next pump, 6.4), which is retained with that tombstone until its validated terminal response settles it, then purged with it; the cleanup test covers all three states. |
| I17 | Recovery duplicates an execution, retries a forbidden failure, or loses a linked job. | Crash after admission commit, after acceptance, and after retained terminal commit before settlement; a counting provider observes one execution and the same job_id throughout, with one completion intent per Connect fire. Two joined fires recover from the same job, although the joining request id names no job. Roll back admission mid-transaction: no partial job/attempt binding/fire link. Expire attempt 1: one CAS transaction opens attempt 2, preserves the old binding, clears current job_id and leaves pending_dispatch; crash there and resume. Failed-only history creates one replacement; if another fire has meanwhile created active work, or completed work now exists, join it instead. Expire attempt 2: manual_review, no third identity. Other failures never open an attempt. Replay old request ids without execution. History permutations failed/completed and completed/failed select completed; completed plus active selects active; completed timestamps tied use job_id order. Calendar awaiting_confirmation and unresolved remain submitted with unchanged fire version, then settle after confirmation/reconciliation. |
| I18 | Fires with different invocation parameters share a job, or confirmation crosses fires. | Two rules, one PDF, one parameterised fixture capability: equal parameters produce one job referenced by two fires; unequal parameters produce two jobs on one lane, serialised; one rule with `confirm_each: true` and one without, equal parameters: the unconfirmed fire runs alone, the confirming fire stays `awaiting_confirmation` with no job, and after confirmation links to the same job's result without a second execution; both fires settle `completed` with one `automation_complete` intent each. |
| I16 | The unattended path fails under the systemd sandbox. | Live: the installed unit (`ProtectHome=read-only`, `ReadWritePaths` to the state directory) runs one pass that discovers Invoice Processor under `XDG_RUNTIME_DIR`, reads the entitlement under `~/.config`, takes the lane and source locks beside the database, and admits two `invoice.extract` jobs with no desktop process running. The mail-check process exits with the second job `waiting`; the installed `eom-email-watcher-connect-queue.timer` then runs `eom-mail-watch pump` and the proof observes the second job advance to `provider_owned` and `completed` through those pump invocations alone, before any further mail check, so the test cannot pass merely because the first job finished inside the initial check. A desktop-present variant repeats it with the host running and asserts that the host's queue thread, not the pass, completed the jobs. This is the end-to-end proof for the timer-driven install. |
| I20 | Multiple actors duplicate a fire effect or removal loses final settlement. | Concurrent, repeated settle from pass, desktop queue thread and pump timer after retained completion: one CAS transition and one completion intent per unexpired Connect fire. Crash between job terminal commit and settle, then run only a standalone pump: settlement completes. Crash immediately before and after source-tombstone deletion: fire transition, review intent and job deletion are all committed or all absent. Repeated active-job/resumable-run scans change no fire version or intents. Losing a CAS inserts neither intent nor attempt; calendar final settlement adds no duplicate calendar intent. Scope required Connect intents to unexpired notification_deadline; at/after expiry the atomic removal still occurs but adds no fire intent. |
| I21 | The transition table, guards, prose and implementation disagree. | Enumerate implementation transitions and compare to 6.1, including definitive pre-admission source loss and tombstone removal. Exercise every row with a positive case and rejected guard. Prove deadline attempt 1 and 2, retained completion and source-unavailable removal are mutually exclusive. No transition leaves a terminal fire; scans of resumable links are no-ops, not self-transitions. Audit effects against the action-specific ownership in 6.1/6.7. |

Real adapters throughout: the provider is the reference provider script the
repo already ships (`scripts/connect_reference_provider.py`) or the real
Invoice Processor; only the mailbox is stubbed, at the `MailboxGateway`
boundary (`mailbox.py:71-84`).

---

## 8. Flags, separate from the design

### 8.1 Where the product brief is now wrong

Section 1.1 in full. Rows for the claim ledger:

| Brief location | Claim | Verdict | Evidence |
|---|---|---|---|
| `Local Connect Status.md:53` | queue closed | true for the desktop host since #123; false for the timer-driven install | C1 |
| `Local Connect Status.md:52` | unattended permission closed | overstated | feature exists in one repo, no contract (C3) |
| `Product Brief.md:107-116` | Email Watcher automatically hands to Summarizer/Invoices today | contradicted | no Connect call in `service.py` (C2) |
| `Product Brief.md:138` | senders across multiple accounts | contradicted | one global allowlist, one active account (C5) |
| `Product Brief.md:150-152`, `342` | bytes fetched only when you act | contradicted in part | pump re-fetches unattended (C6) |
| `Product Brief.md:252-254` | gate refuses to run an automation | confirmed, with the authorized-write exception | `service.py:797` (K2) |
| `Product Brief.md:261-262` | halts in `ambiguous` or `manual_review` | confirmed, incomplete | fourteen states (K3) |
| `Product Brief.md:434-435` | "A queue ... in development" | stale for the desktop since #123/#124; still true for the timer-driven install | C1 |
| `docs/CONTRACTS.md:858-859` @29fd046 | engine pump remains a later slice | was contradicted; corrected at `c10a37c` | C4 |
| `adr/0004:132` | activation grants only capability exchange | stale | watcher grants a second feature from the same file (C9) |
| issue #117 body | table `connect_jobs` at `db.py:83` | wrong name | `connect_attachment_jobs`, `db.py:92` (C8) |

### 8.2 Whether ADR-0001 needs a superseding ADR

Not for the queue: ADR-0001 already assigns retry to the caller and
forbids a provider-side queue, and Email Watcher's queue is caller-side
polling with caller-owned re-POST. Direction is not inverted. **Yes for
the record**, and the rule engine is the moment to write it, because three
decisions now live only in one consumer's `docs/CONTRACTS.md` and nowhere
a second consumer would find them:

- consumer-owned admission queue keyed by
  `(protocol, provider app, provider instance)` is the normative pattern
  for every consumer, and `PROVIDER_BUSY` with `retryable: true` is defined
  as an authoritative pre-admission refusal (`docs/CONTRACTS.md:1050-1053`);
- `JOB_NOT_FOUND` proves non-retention only before authoritative
  acceptance (`1073-1074`);
- a consumer's unattended trigger layer is consumer-local and requires no
  protocol change; a provider that wants to be usable under Automate needs
  nothing beyond ADR-0002 (this document's section 3).

Proposed: **ADR-0006, "Consumer-owned admission and the Automate
feature"**, status Accepted, amending ADR-0001's "v0 has no ... automatic
retry service" sentence with "the caller owns admission, ordering, and
retry" and correcting ADR-0004:132.

### 8.3 What belongs in connect-contracts rather than in Email Watcher

At minimum the feature vocabulary, so the paid boundary stops depending on
matching string literals across repos:

1. `entitlements/v1/features.json`: an enumerated registry of feature
   identifiers with a one-line meaning each, today exactly
   `connect.capability_exchange` and `connect.automations`, and a test that
   every fixture licence uses only registered features. The claims schema
   stays as it is (a registry, not a schema enum, so adding a feature is
   additive and does not invalidate installed licences).
2. `entitlements/v1/fixtures/valid/active-automations.json`: a signed test
   licence carrying both features, so every app's entitlement tests can
   assert the second key from the shared fixture instead of minting one
   locally (Email Watcher's live acceptance used an ephemeral one,
   `docs/CONTRACTS.md:21-23`).
3. In `tools/entitlement_issuer.py`: validate `--feature` against the
   registry, not only the pattern (`:341-344`), so a misspelled tier cannot
   be issued.
4. ADR-0006 as in 8.2, which also states that `connect.automations` is
   consumer-enforced only and that the wire deliberately carries no
   entitlement (`adr/0003:23-25`), so no provider is expected to check it.
5. Nothing about rules, events, or actions. Rules are consumer-local data
   and the action vocabulary is already the manifest (`schemas/v2/manifest.
   schema.json`). Putting a rule schema in the contracts repo would make a
   consumer's UI a protocol.

---

## 9. Deferred, with the reason

- **Body and header predicates.** Would require re-fetching content at
  evaluation and would make evaluation depend on mailbox availability;
  breaks section 5.3's pure-function property. Revisit with a stored,
  bounded, retention-purged body digest if a real rule needs it.
- **Compaction of retired rule versions.** A version retired at revision x
  could be removed once no message with a captured revision below x is
  still unevaluated; deferred because the progress-handler bound makes it
  a space concern, not a correctness one.
- **Retroactive firing on rule creation or edit.** Product decision: a new
  rule silently sending every retained PDF to a provider is the kind of
  surprise the brief promises not to cause. Offer as an explicit
  "apply to the last N days" action later, confirmed once.
- **OR and negation.** Two rules cover OR; negation invites
  "everything except" rules that fire on surprises.
- **Re-fire after `analysis.requeue`.** Halt-don't-guess; see 5.1.
- **A second trigger source.** The core is ready; there is no second source
  and no contract for one.
- **Batch verbs and a morning digest** (`Brief:293`). Batching is
  scheduling plus aggregation over completed fires; the fire ledger this
  contract creates is the input to it. Separate contract.
- **Output-aware actions** ("if withheld, then ..."). Requires the engine to
  parse provider media types; ADR-0002 says unknown types are opaque.
- **Windows.** The engine reuses the existing locks and paths, which have
  Windows implementations (`connect_windows.py`). The desktop scheduler and
  the queue thread run for as long as the host process runs: closing the
  window does not stop it, because the host intercepts `CloseRequested`,
  prevents the close and hides the window (`lib.rs:737-745`); quitting the
  host does. An unattended host on Windows with no desktop process (the
  systemd role) is unspecified and is not claimed. Installed Windows
  behaviour is unverified; the release target tracks Linux and Windows
  together and this contract does not redefine it.

---

## 10. Explicit non-scope

- any change to a provider, a Connect schema, a manifest, or the error
  taxonomy;
- provider-side queueing;
- a separate automation process, broker, or callback protocol;
- automatic or model-confirmed execution of any capability that declares
  effects;
- storing message bodies or attachment bytes;
- changing the sender allowlist model, the one-active-account model, or
  the mailbox read-only promise;
- notifications carrying provider output.

---

## 11. Rejected alternatives

- **Evaluate inside `mark_analyzed`, like the scheduling admission.**
  Rejected: it would couple the rule set to the analysis transaction and
  make a slow or failing rule evaluation block the analysis commit. The
  durable "unevaluated" marker gives the same cannot-lose property.
- **Rules in `config.toml`.** Section 4.5.
- **Make the engine's own ledger the queue: submit one job per lane per
  pass and wait for it synchronously** (r1's section 6.2). Rejected in r2:
  the synchronous wait carries its own 30-minute job timeout
  (`connect.py:53`), so no pre-submission budget bounds the pass; and the
  durable queue with a non-blocking pump now exists (#123), so a second
  queue in the engine would reproduce policy the pump already owns. The
  concern that motivated r1 -- a `waiting` job dying on the two-hour
  admission window between timer passes -- is real and is met by the
  connect-queue timer of section 6.5, not by refusing to enqueue.
- **A per-rule provider instance pin.** Rejected: instances rotate on
  provider state reset (`adr/0002:87-96`); pinning the app id and resolving
  the instance at fire time is what the click path does.
- **Free-text rule DSL.** Rejected: user input that is parsed is user input
  that is attacked; a closed form is validated once and rendered natively.
- **First-match-wins.** Rejected: silent precedence is a guess; every
  matching rule fires and the collapse rule makes duplicates impossible.
- **Skip the confirmation gate for capabilities the rule author already
  trusts.** Rejected: the manifest's `effects` is the provider's statement
  about what leaves the machine; a consumer must not override it
  (`adr/0002:59`).

---

## 12. Landing order (for the implementation contract to follow, not this one)

1. This document, contract only.
2. ADR-0006 and the feature registry in `connect-contracts` (section 8.3),
   so the second key has a home before more code depends on it.
3. Schema 20: rules, rule versions with `revision`/`retired_revision` and
   tombstone deletion, the `automation_rule_set` revision counter,
   `messages.rules_revision_at_analysis`, fires with a non-null
   `artifact_key`, attempts with `dispatch_request_id` and write-once job
   bindings, the fire's current attempt and job link, `pending_since`,
   job effects snapshots and authorization receipts, fire/intent
   notification deadlines,
   `rules_evaluated_version` (migration sets `0` on already-analysed
   messages), stored fire `state`/`state_version`, the job `origin`
   column, and the settlement step; the seeded system rule, enabled; the
   engine core with I1, I2, I9, I11, I14, I21.
4. Evaluate phase wired into `run_watcher_check`; the scheduling trigger
   replaced by the seeded rule; I3, I12, I13.
5. Dispatch phase over the durable queue: admission under the fire's
   request id, fingerprint collapse, the in-pass pump call, the pass
   deadline; I5, I6, I7, I8, I15, I17, I18.
6. `eom-mail-watch pump` and the connect-queue user timer for the
   timer-driven install (section 6.5).
7. Confirmation operation and rules UI, "Automations locked"; I4, I10.
8. Live systemd proof, I16, recorded here with the tested revision.

---

## 13. Revision history

- **r1, `f500a1f` (2026-09-09).** First draft, read against `29fd046`.
- **r2 (2026-09-09).** After the local review of r1, which confirmed every
  finding against the code. Changed: section 0 (baseline `c10a37c`,
  relationship to the dashboard assignment); C1 and C4 (the desktop host
  pumps since #123; the timer-driven install still does not); 4.1
  (`confirm_each` and `accepted_at` are normative); 4.6 (the seeded rule is
  enabled by default and readiness is decided per fire, never at
  migration); 5.1 (rule-version applicability is
  `accepted_at <= analysis_at`, with crash, migration and edit cases); 5.2
  (collapse on the complete v2 invocation fingerprint including parameters;
  confirmation never crosses fires); 6.1 (`dispatch_request_id` persisted
  before any provider call); 6.2 (dispatch admits into the durable queue
  and pumps, nothing waits for a terminal state, one absolute pass deadline
  propagated to every blocking call); 6.3 (one design, two hosts); 6.5
  (why the pass pumps, plus the CLI pump and timer the systemd install
  needs); 6.7 (the opt-in phone notification is named as outbound
  content); 6.8 (the host queue thread as a fourth actor); 7 (I2, I6, I7,
  I13 rewritten; I17, I18 added); 9 (the Windows window-close claim
  corrected against `lib.rs:737-745`); 12 (landing order).
- **r3 (2026-09-10).** Citation and accuracy pass only, no design change:
  nine `engine_api.py` line citations that were `29fd046` numbers without
  the label now point at the same code at `c10a37c`; section 6.8's
  click-during-pass paragraph now describes the post-#123 behavior (the
  losing click returns the job's active state, not an error); I16 adds
  the desktop-present variant.
- **r4 (2026-09-10).** After the PR #126 review and the Codex threads on
  `01ee702`. Changed: 4.1 (database-assigned `revision`/`retired_revision`
  replace timestamps as the applicability boundary; deletion is a tombstone
  version); 4.5 (delete operation); 5.1 (applicability by
  `revision <= rules_revision_at_analysis < retired_revision`, captured in
  the analysis transaction; deletion consequence); 5.3 (non-null
  `artifact_key` with a sentinel, because NULLs are distinct in a SQLite
  unique index); 6.1 (`not_admitted` terminal state in the diagram;
  attempts with their own request ids, at most two, opened only after a
  proven-never-accepted terminal); 6.2 (deadline scoped to the evaluate
  and dispatch phases, the 30-minute-timeout claim withdrawn; expiry opens
  a second attempt); 6.3 (the pump timer owns progress between mail
  checks on a timer-driven install); 6.4 table; 6.7 (provider app version
  in `item_sha256`, `provider_changed` on version change); 2.3, 2.5 Q3 and
  8.1 (the queue claims scoped to the timer-driven path; #123 and #124
  rows added); 7 (I2, I7, I16, I17 rewritten); 12 (schema step).
- **r5 (2026-09-10).** After the review of `0cb8daa` and six Codex
  threads on it. Changed: I1 (non-null `artifact_key`, direct duplicate
  inserts must fail for both action classes, distinct-artifact control);
  6.2 opening (stale next-mail-check sentence replaced by the pump timer);
  2.5 Q3 (#121 row labelled historical); 2.1 and 5.1 (`mark_analyzed`
  anchor `db.py:5405-5421` at `c10a37c`); 4.1 (tombstone is a `deleted`
  version kind outside Rule validation; deletion stops later applicability
  and does not cancel already-eligible work); 4.2 and 4.4 (per-artifact
  conditions only on artifact-consuming actions); 6.1 and 6.2 (dispatch
  branches by action kind: `calendar.propose` admits its run and `notify`
  completes inside the evaluation transaction; `pending_dispatch ->
  awaiting_confirmation` after discovery and hashing, step 3a); 6.1 and
  6.4 (a second attempt only after `connect_queue_deadline_exceeded`;
  `connect_source_unavailable` is terminal); I12 (enumerated handoff
  states, silent states excluded).
- **r6 (2026-09-10).** After the review of `f3f73e9` and three Codex
  threads on it. Changed: 4.1 and 4.4 (`confirm_each` permitted only on
  `connect.invoke`, rejected elsewhere); 6.2 (`connect.invoke` fires are
  always created `pending_dispatch`; step 3a prepares and hashes a
  rule-requested confirmation exactly like a manifest-required one; live
  parameter rejection halts in `manual_review` with `parameters_invalid`);
  6.4 (new taxonomy row; a defined `automation_complete` intent for
  completed `connect.invoke` fires, one per completion, same delivery path
  and opt-in as review intents); I4 (confirmation matrix, validation
  case), I10 (payload of the completion intent), I12 (completion and
  silent states reconciled).

- **r7 (2026-09-10).** Root-cause revision after seven Codex threads on
  `42ddf79`, two of which were regressions introduced in r5 and r6 and one
  a contradiction r4 created. The source of the recurring defects was
  r2's decision that a fire's state is derived from its job at read time
  while later sections attached fire-owned obligations that no
  transaction owned. Changed: 6.1 rewritten as the single normative
  transition table with a stored, compare-and-set fire state, one
  idempotent **settlement** step run by every pump call and every pass,
  recovery through the linked `job_id` (request ids only create), a
  durable job `origin`, and the stray `matched -> awaiting_confirmation`
  edge removed; 6.2 (discovery outcome table including
  `capability_unavailable`; admission writes `origin`; every pump ends in
  settlement); 6.4 (rows for `capability_unavailable`,
  `provider_changed`, and the Automate-key failure; the completion intent
  written by settlement; source cleanup settles no-job fires and never
  cascade-deletes nonterminal fires); 6.6 (both keys required at a
  proven-new POST of an automation-origin job, through the pump's
  existing failure path); 6.7 (intents written by the transitioning
  transaction); 5.1, 5.2, 6.8 (edit timing stated only as the revision
  formula; joined fires recover through `job_id`); I3, I15, I17, I18
  rewritten; I20 (settlement) and I21 (table is normative) added.
- **r8 (2026-09-10).** After six Codex threads and one reviewer thread on
  `b0d2360`, four of them consequences of r7's own text. Source: the
  find-or-create decision was stated in three places (5.2, 6.1 recovery,
  6.2 step 4) with three different rules. Changed: 6.2 step 4 is now the
  one admission decision, including the bounded fetch-and-hash under the
  source lock that gives an effect-free fire its artifact identity, the
  fingerprint, and the precedence request-id-named, active, newest
  completed, else create (failed history never blocks or satisfies); 5.2
  and 6.1 recovery point at it; 6.2 step 2 uses catalog-level discovery
  (`connect.discover_capabilities`) for unbound fires, the persisted-job
  helper only after a job exists; 6.1 keeps a fire `submitted` while its
  calendar run is in any nonterminal state and settles only on the run's
  terminal states, with a late-terminal tombstone row; 6.4 cleanup
  settles a fire before the trigger deletes its never-submitted job; 6.6
  makes the entitlement check per candidate inside the evaluation
  transaction; I3, I15, I17 extended.

- **r9 (2026-09-10).** Operator-requested correction pass on top of r8.
  Prepared source identity now precedes both confirmation and admission,
  with one lock scope and explicit missing/transient source outcomes.
  Discovery counts app instances before filtering capabilities. The
  transition table uses disjoint retained/removal and first/second-attempt
  guards; resumable calendar states are no-ops. Retry atomically clears
  the current link while retaining attempt bindings; active/completed
  work can be joined by a second attempt. Settlement effects are owned by
  action kind, and dependency removal settles before deleting waiting,
  terminal or reconciliation rows. Candidate authorization has an explicit
  transaction boundary. I3, I4, I8, I12, I15, I17, I20 and I21 cover these
  paths. These are contract requirements, not implemented behavior.

- **r10 (2026-09-11 UTC).** Reconciled the remaining stall-clock feedback.
  `pending_since` measures the current unbound pending interval and resets
  on initial evaluation, human confirmation or the second attempt, never
  on an ordinary retry. Confirmation wait and submitted work do not consume
  that interval. I4 covers delayed confirmation, exact expiry, retry and
  unchanged source-retention checks. No runtime implementation is added.

- **r11 (2026-09-11 UTC).** Closed deferred-POST policy drift and mixed
  authorization: snapshot both effect flags, bind confirmation/receipts,
  recheck before every proven-new POST, and preserve a real joining click's
  authority independently of creator origin. Specify legacy metadata
  handling without fabricating historical confirmation. Correct the
  calendar notification inventory to the actual query. Fire notices have
  the source retention deadline; expiry transitions are silent and delivery
  discards expired intents. I3/I4/I12/I15/I20 cover these boundaries.

- **r12 (2026-09-11 UTC).** Reconciled the four review threads on
  `f76d91b` at their source. Fire intents carry no message-derived
  content and are rendered from the live message row at delivery, so
  deletion removes them by construction (6.4, 6.7; `ENGINE_API.md:247-254`
  preserved); every transition supersedes the fire's earlier undelivered
  intents and delivery requires the current state version (6.4); the
  stall clock pauses while the licence is inactive via
  `licence_blocked_at`, so admitted work resumes and only never-admitted
  `locked` candidates stay locked (6.2, taxonomy row unchanged); the
  evaluate phase's own work is bounded by a 100-rule cap, 50-candidate
  chunks, an indexed per-chunk version snapshot, and a deadline check
  before every candidate transaction (6.2, 4.4). I3, I7, I12, I15
  updated. No runtime implementation is added.

- **r13 (2026-09-11 UTC).** After five Codex threads on `706f5f9`.
  Changed: 6.2 (the chunk snapshot is one indexed point lookup per live
  rule per distinct captured revision, never a range over history; the
  pump runs once per pass after the admission loop); 6.2 stall clock (the
  pause is anchored to the licence: authorized time accrues only between
  two authorized observations of the same `entitlement_id`, so a licence
  that expired long before the engine noticed contributes nothing); 6.7
  (the intent mandate excludes the removal transitions of 6.4); I15 (a
  fire whose Connect tombstone is unresolved is retained with it past the
  cutoff); I3 (the pause test anchored to expiry and reinstall).

- **r14 (2026-09-11 UTC).** After four Codex threads on `0a408c9`, all
  consequences of r12 and r13 text. Changed: 6.2 (the snapshot enumerates
  the rules in effect at each captured revision from the version table,
  so a deleted rule's pre-deletion version still fires for an earlier
  analysis; cost bounded by 100 plus writes since that analysis; deletion
  does not flip the enabled flag); 6.2 stall clock (authorized time and
  its baseline reset with each fresh pending interval); 6.4 (delivery of
  a fire intent and the decide operation share the host operation lock,
  which is what makes "never sent" true); I15 (retention exception
  includes `dispatching`); I12 (the lock-based supersession test).

- **r15 (2026-09-11 UTC).** After two Codex threads on `b9c7259`, both on
  r14's snapshot paragraph. Enabling and disabling are now version rows
  (4.1, 4.6), so applicability is revision-only with no current-flag
  exception and a disable behaves like a deletion for messages analysed
  before it. Every evaluate-phase statement is bounded by SQLite's
  progress handler at the phase deadline (6.2), replacing the claim that
  writes since analysis are "usually small"; history compaction is
  deferred (9). I2 and I7 gain the corresponding cases.
