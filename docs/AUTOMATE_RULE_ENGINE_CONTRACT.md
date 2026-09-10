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
change (`db.py:5340`, `5374-5386`, `924-975`), inserting `automation_runs`
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

**Human handoff.** Every halt produces a durable `automation_review`
notification intent (`db.py:5418-5420`, delivered by
`service.py:1013-1045` through `send_review`, which is the ntfy plus desktop
channel, and acknowledged at `db.py:5473-5490`). The desktop shows the
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
schedule (`engine_api.py:2464-2478`, `2441-2446`); an ambiguous failure
goes to `reconciling` (`2480-2488`); a definitive non-retryable refusal is
terminal (`2662-2667`). Ownership of the retry is the pump
(`2884-2996`), which nothing calls (C1). So: retry policy exists, retry
execution does not, and a waiting job's fixed two-hour admission deadline
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
| #121 pump | 2/4/8/16/30 backoff (`engine_api.py:2441-2446`); busy to `waiting` (`2464-2478`); `connect.queue.pump` draining due lane heads under the lane lock (`2884-2996`); entitlement-free discovery only for reconciliation (`connect.py:1152-1163`; `engine_api.py:2907-2921`) | engine operation exists; no host calls it (C1) |
| #122 source lock | source lock held across fetch, hash check, POST, and outcome persistence (`engine_api.py:2604-2676`); retention checked at enqueue and handoff (`1870-1896`, `2613-2616`); transient mailbox failure returns to `waiting` (`2633-2648`) | coordination between handoff and cleanup |

Not landed: host wakeups, durable queue UI states
(`docs/CONTRACTS.md:1203-1210`), the installed two-invoice proof
(`1265-1267`).

One-job-at-a-time still holds at the provider boundary (K1) and is now
mirrored on the consumer side: one process may own one provider lane at a
time (`engine_api.py:2802-2805`, `2897-2904`), and the lane is
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
  enabled          boolean
  system           boolean; true only for the seeded scheduling rule
  version          integer >= 1; incremented on every accepted edit
  scope            { provider?: identifier, account_id?: string(<=128) }
                   absent field = any
  trigger          { source_kind: "mail.message" }
  conditions       1..8 entries, all must hold (AND)
  action           exactly one of the kinds in section 4.3
  confirm_each     boolean, optional, default false; when true every fire
                   of this rule waits for a person (section 6.7) even if
                   the capability declares no effects
  created_at, updated_at   UTC
```

Each accepted edit produces a new immutable row in `automation_rule_versions`
(`rule_id`, `version`, the full definition, `accepted_at` UTC). `accepted_at`
is the rule-version applicability boundary used in section 5.1.

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
ledger unchanged. The engine's fire references the run id and derives its
state from it (section 6.6).

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
than eight conditions; an artifact-consuming action without an
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
`automation.rules.delete`, `automation.rules.set_enabled`. There is no text
DSL and no file a person edits by hand.

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
  the engine records `disabled_by_person_at` and never re-enables it, not
  on migration, not on an account or grant transition, not on upgrade.
  A person may re-enable it. They may not delete it or change its
  conditions in version 1.
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
timestamps, not by when the pass happens to run:

- `messages.analysis_at`, written by `mark_analyzed` when the analysis that
  produced the matching fields committed (`db.py:5351`);
- `automation_rule_versions.accepted_at`, written when a rule version was
  saved (section 4.1).

A rule version applies to a message iff `accepted_at <= analysis_at` and it
is the latest version of its rule with that property. A rule created after
the message was analysed has no applicable version and cannot fire on it.
A rule edited after the message was analysed is evaluated on the version
that existed at analysis, so the edit neither fires retroactively nor
loses the work the earlier version was entitled to. The evaluation reads
the version table once per pass (one read transaction); a version accepted
after that snapshot is by construction newer than every candidate's
`analysis_at` and cannot apply in this pass. This is the rule the first
draft promised (I2) but did not mechanise.

Consequences that follow from the boundary, not from extra rules:

- **Crash between analysis and evaluation, new rule saved in between.**
  The message stays a candidate; the new rule's `accepted_at` is later than
  the message's `analysis_at`; it does not fire. The versions that did
  exist at analysis fire as they would have.
- **Migration.** Messages analysed before the migration have
  `analysis_at` earlier than every rule version's `accepted_at`, including
  the seeded rule's, so no rule applies to them and no retroactive fire is
  possible. Their `rules_evaluated_version` is set to `0` by the migration
  ("evaluated under no rules") so they are not re-read every pass. The
  runs the old scheduling path already admitted for them live in
  `automation_runs` untouched.
- **Edits during a pass** take effect for messages analysed after the edit,
  which is the next pass at the earliest.
- **Re-analysis.** `analysis.requeue` (`engine_api.py:1792`) does not clear
  `rules_evaluated_version`; a re-analysed message does not re-fire, even
  though its `analysis_at` moves forward.

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
its own row with its own `dispatch_request_id`. When the dispatch phase
admits a fire (section 6.2), it looks up, within the same transaction, a
job for the same artifact with an equal fingerprint:

- an active job (`requested`, `accepted`, `processing`) -- the fire links
  to it and creates nothing; this is what the existing active-fingerprint
  unique index already guarantees for a click (`db.py:282-286`,
  `docs/CONTRACTS.md:923-933`);
- a `completed` job -- the fire links to it and records `completed` with
  that job's result; the bytes are not sent again for an invocation that
  already produced a result. (A click is allowed to re-run; a rule is not,
  because a rule that re-runs on every pass is a loop.)
- a `failed` job, or none -- the fire creates a new job under its own
  `dispatch_request_id`.

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
`(event_id, rule_id, rule_version, artifact_ref)` in `automation_rule_runs`,
where `event_id` is the SHA-256 of `(provider, account_id,
provider_message_id, "mail.message", 1)`, i.e. the same message key the
scheduling admission uses (`db.py:934`).

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

```text
matched -> pending_dispatch -> submitted -> completed | failed
matched -> awaiting_confirmation -> pending_dispatch      (person confirmed)
                                 -> declined              (person declined)
pending_dispatch -> provider_unavailable | ambiguous_provider
                 | source_unavailable | manual_review
submitted -> source_unavailable                           (cleanup won)
```

`submitted` is not a stored truth of its own: a fire in `submitted` holds a
`job_id`, and its displayed state is derived from that job's
`connect_attachment_jobs.status` and `connect_job_dispatch.state` at read
time (`waiting` shows "Waiting for <provider>", `reconciling` shows
"Reconnecting", `provider_owned` shows "Running", exactly the queue
contract's UI vocabulary, `docs/CONTRACTS.md:1203-1210`). There is one
state machine for a job, not two.

For `calendar.propose` the fire holds a `run_id` and derives its state from
`automation_runs.state`; `completed`, `declined`, `failed` map directly;
every halting state of section 2.1 maps to `manual_review` for display and
notification purposes while the underlying run keeps its exact state.

**Durable dispatch identity.** Every fire is created with a
`dispatch_request_id` (uuid4) in the evaluation transaction (section 5.1),
before any external call can happen. A Connect job created for a fire is
created under that request id, in the same transaction that writes the job
row and the fire's `job_id`, and only then is the provider contacted. The
existing request-id replay (`engine_api.py:3085-3100` at `29fd046`:
a request id whose job is `completed` returns the completed result) makes
this recoverable across every crash boundary: a fire in `pending_dispatch`
or `submitted` whose `dispatch_request_id` already names a job row resumes
that job by id -- through the pump if active, through the stored result if
completed -- and never generates a second request id. The first draft
entered the click path "with a fresh engine-owned request_id" and left the
fire-to-job association to a later write, which permitted one provider
execution per crash. A unique fire row alone is not proof of one provider
execution; the persisted request id before the first POST is.

### 6.2 Dispatch phase

For every fire in `pending_dispatch`, in `(occurred_at, message_id,
part_id)` order, grouped by resolved provider instance, within the
**pass deadline** (below):

1. re-check both licence features (section 6.6);
2. discover the pinned `provider.app_id`; zero instances leaves the fire
   `pending_dispatch` for the next pass; two or more instances of the
   same app id halts it in `ambiguous_provider` (`adr/0001:110-112`);
3. validate the artifact and parameters against the live manifest
   (section 4.3);
4. **admit**: in one transaction, look up an equal-fingerprint job
   (section 5.2) and either link the fire to it or create the job row
   under the fire's `dispatch_request_id` with the fire's `job_id` set --
   the same durable queue admission a click performs
   (`engine_api.py:3138-3245` at `29fd046`), so the job is in the lane's
   queue with the trusted digest recorded, and **no provider call has been
   made yet**;
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
   stay `pending_dispatch` for the next pass.

The first draft's step 5 entered the click path, which "submits and waits
for a terminal state synchronously" (`engine_api.py:2425`,
`client.wait_for_terminal`), with a ten-minute budget checked only before
each submission. That never bounded the pass: the nested wait had its own
30-minute job timeout (`connect.py:53`, `DEFAULT_JOB_TIMEOUT_SECONDS`), so a
submission allowed at second 599 could hold the pass until second 2399.
The pump exists precisely so that nothing waits, and the engine uses it.

**Pass deadline.** The pass computes `deadline = pass_start + 10 minutes`
once, at the start of `run_watcher_check`, before mail is fetched. Every
blocking operation in the evaluate and dispatch phases -- attachment
fetch, lock acquisition, provider submit and GET inside the pump, the
pump call itself -- receives the remaining time to that deadline as its
timeout, and an operation whose minimum duration would not fit is not
started. Mail fetch and analysis run before dispatch and consume the same
deadline, so a slow mailbox leaves less time for dispatch and never more;
when nothing remains, nothing is dispatched and every fire stays
`pending_dispatch`, which is a safe resumable state because admission is
durable and idempotent. This is what makes the desktop scheduler's
30-minute engine timeout (`scheduler.rs:17`) unreachable by a pass and is
the invariant I7 now actually settles: both the slow-provider and the
slow-mailbox case are tested with a simulated clock.

**What the pass does not do.** It does not wait for jobs it admitted.
With the desktop open, the host's queue thread pumps them at the
engine-supplied wake times (C1). Without a desktop, the next pump is the
next timer-driven pass (section 6.5), and the two-hour admission window
can expire a `waiting` job first; `connect_queue_deadline_exceeded` proves
the provider never accepted it (`docs/CONTRACTS.md:1055-1061`,
`1156-1158`), so the fire may create one new job identity once, then halts
in `manual_review` on the second such failure. Fires not reached stay
`pending_dispatch`; jobs left `provider_owned` or `reconciling` are
reconciled by the next pump, GET-before-POST by construction
(`engine_api.py:2731-2742`).

`PROVIDER_BUSY` during the pass, which can only come from a user click that
won the lane in between, is handled by the existing deferral
(`engine_api.py:2464-2478`); the pump walks the backoff schedule.
Provider absence for a fire that has **not** yet produced a job is a
pass-level retry with no Connect state; a fire that has been
`pending_dispatch` for 24 hours halts in `provider_unavailable` and
notifies.

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
- **Timer-driven install, no desktop.** The pass is the only pump until the
  next pass (section 6.5).

Both hosts run the same tests (section 7); the tests that distinguish them
are the wake-time tests (host present: a `waiting` job advances before the
next pass; host absent: it advances at the next pass or expires under the
admission window and is handled as section 6.2 says).

### 6.4 Failure taxonomy

| Outcome | Fire state | Person told? | Retry |
|---|---|---|---|
| provider absent, no job yet | `pending_dispatch` | after 24 h, as `provider_unavailable` | every pass until then |
| two instances of the pinned app | `ambiguous_provider` | yes | none; person picks by clicking |
| artifact not accepted by live manifest | `manual_review` (`unsupported_attachment`) | yes | none |
| `PROVIDER_BUSY` (retryable) | `submitted` / job `waiting` | no | queue backoff |
| ambiguous POST outcome | `submitted` / job `reconciling` | no | GET before POST, existing |
| non-retryable refusal, or job `failed` | `failed` with the provider's bounded `code` and `message` | yes | none; a person may click |
| deadline or source loss before acceptance | new job once, then `manual_review` | on the second | one |
| source gone or changed | `source_unavailable` | yes | none |
| licence not active at dispatch | fire stays `pending_dispatch`, evaluation continues recording `locked` outcomes | rules panel shows locked | resumes when active |
| completed, output withheld fields | `completed` | yes | none |

"A result that comes back withheld" is a completed job whose output carries
withheld fields (Invoice Processor's contract). The engine does not parse
`application/vnd.local-connect.invoice+json`; unknown media types are
opaque by contract (`adr/0002:108-111`). The completion notification says
the rule completed and that a result is waiting; it never includes
provider output text, so this contract adds no new content class to the
phone channel beyond what `send_review` already carries
(`service.py:1025-1032`).

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

1. at the start of the evaluate phase, once; if either feature is missing,
   every candidate is still marked evaluated with outcome `locked` and no
   fire is created (so activating the licence later does not fire rules on
   the whole retention window);
2. immediately before each dispatch (section 6.2 step 1), both features,
   through `feature_entitlements_active(CONNECT_FEATURE_ID,
   AUTOMATIONS_FEATURE_ID)` (`entitlement.py:280-281`), the same call the
   service loop makes (`service.py:112`);
3. inside the enqueue path, the exchange feature again
   (`engine_api.py:3100`) and at every proven-new POST (`2907-2917`); this
   is not the engine's check to remove;
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

Every halting state emits an `automation_review` intent through the same
UNION the calendar runs use (`db.py:5396-5420`), delivered by the same code
(`service.py:1461-1470`, `1013-1045`) and acknowledged the same way
(`db.py:5473-5490`). The intent names the rule, the sender label, and the
subject; nothing else.

A fire enters `awaiting_confirmation` instead of `pending_dispatch` when the
live manifest declares `effects.external` or `effects.confirmation_required`
for the selected capability, or when the rule author set
`confirm_each: true` (an optional rule field, default false). A person
confirms one specific fire by
`(fire_id, state_version, item_sha256)` where `item_sha256` binds the
artifact digest, capability id and version, provider app id and instance
id, and canonical parameters; the engine operation
`automation.rule_run.decide` mirrors `calendar.automation.decide`
(`db.py:4277-4284` for the compare-and-set shape). If the resolved instance
at dispatch differs from the confirmed instance, the fire halts in
`manual_review` with `provider_changed`; it does not re-resolve. A confirm
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
  lock (`engine_api.py:2802-2805`) and the source lock (`3149-3153`). The
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
  the loser gets `connect_job_in_progress` (`engine_api.py:2817-2820`,
  `2830-2835`) with a durable job row, and the pump advances it.
- **Rule edits during a pass.** Snapshot at phase start (one read
  transaction); the edit is visible next pass. Edit operations do not take
  the check lock and therefore never wait on a thirty-minute pass.
- **Cleanup racing dispatch.** Inherited: source lock ordering is lane then
  source for a pump, source only for cleanup (`docs/CONTRACTS.md:1147-1160`).
  The engine adds no lock and cannot create a cycle.

---

## 7. Invariants and the tests that settle them

Stated as things that must never happen, each with the evidence that
proves it does not.

| # | Never | Settling evidence |
|---|---|---|
| I1 | A rule fires twice on one message and artifact. | Unique index on `(event_id, rule_id, rule_version, artifact_ref)`; test evaluates the same message in two passes and after a crash injected between evaluation commit and dispatch, asserts one fire. |
| I2 | A rule version fires on a message whose `analysis_at` precedes the version's `accepted_at`, or an eligible earlier version is lost. | Test creates a rule after analysis, runs a pass, asserts no fire; edits a matching rule after analysis, asserts the message fires on the pre-edit version and not the edit; crash injected between analysis commit and evaluation with a new matching rule saved in between, asserts no fire from the new rule and the expected fire from the old; migration over retained analysed messages asserts zero fires and `rules_evaluated_version = 0`. |
| I3 | Anything fires without both features active. | Matrix test over (none, exchange only, automations only, both) times (rule with `connect.invoke`, `calendar.propose`, `notify`): only (both, any) fires; the automations-only row also asserts the seeded scheduling rule does not admit a run (`tests/test_service.py:542` extended). Boundary probe: the licence active until `expires_at` minus one second fires; at `expires_at` does not. |
| I4 | A capability with declared effects or confirmation is invoked without a person confirming that exact item. | Fixture manifest with `effects.external: true`; test asserts `awaiting_confirmation`, then confirms with a stale `item_sha256`, asserts refusal; confirms with the right one, asserts dispatch. Negative: a manifest with both false is dispatched without confirmation. |
| I5 | Bytes are sent that differ from the bytes that were queued. | Inherited (`engine_api.py:2649-2657`); test changes the attachment between enqueue and handoff through the engine path and asserts `connect_source_unavailable` and no POST. |
| I6 | An engine dispatch call waits for a provider's terminal state, or the engine reproduces queue policy outside the pump. | Test with three PDFs, one provider, a provider stub that completes slowly; assert the pass admits all three under the cap, makes exactly one submit for the lane head, returns without any `wait_for_terminal` call (spy on `ConnectV2Client.wait_for_terminal`), and leaves the other two `waiting` in the durable queue; a host pump at the returned wake time advances the next. |
| I7 | A pass runs past its deadline. | Simulated clock and a deadline propagated to every blocking call. Slow provider: a stub whose submit sleeps past the remaining time; assert the submit is cut at the deadline, the pass returns within it, the job is `requested`/`waiting`, and the next pump reconciles GET-before-POST. Slow mailbox: a gateway whose attachment fetch consumes the whole deadline; assert nothing is submitted, every fire is `pending_dispatch`, and the pass still returns by the deadline. |
| I8 | The engine picks a provider. | Two registrations for one app id; assert `ambiguous_provider` and no job. One registration whose instance changed between confirmation and dispatch; assert `provider_changed`, no job. |
| I9 | An invalid rule is partially applied or evaluated. | Every rejection in section 4.4 has a test asserting the write returns an error, the prior version is unchanged, and a corrupted stored definition loads as `invalid` and is skipped without failing the pass. Both error directions: a rule with exactly eight conditions saves; nine does not; a 16-key parameters object saves; 17 does not. |
| I10 | Provider output text leaves the machine in a notification. | Test captures the ntfy payload for a completed fire and asserts it contains rule name, sender label, subject, outcome word, and no substring of the job's output. |
| I11 | The engine core imports mail, provider, calendar, or Connect code. | Test walks the core module's import graph and asserts the allowlist. |
| I12 | A halt is silent. | For every non-`completed` terminal or waiting-on-person state in section 6.1, a test asserts one `automation_review` intent exists and is delivered by the existing path. |
| I13 | The seeded scheduling rule behaves differently from today, or its enablement depends on setup order. | The existing scheduling tests (`tests/test_scheduling.py`, `tests/test_service.py` automation cases) pass unchanged with the trigger replaced by the seeded rule. Lifecycle: migrate with no account, connect and grant later, assert the next scheduling message admits a run; revoke the grant, assert `not_admitted` and no run; restore it, assert admission resumes; a person disables the rule, re-run the migration and flip the grant, assert it stays disabled and admits nothing; re-enable, assert admission. |
| I14 | A rule reads message bodies. | The event builder's inputs are asserted by type: message row, attachment descriptors, analysis fields; a test asserts `gateway.content` is not called during evaluation. |
| I15 | Retention or deletion loses an idempotency record while a fire is still eligible. | Inherited from `messages_delete_connect_attachment_jobs` (`db.py:296-317`); test deletes the source message of a `submitted` fire and asserts the fire becomes `source_unavailable` and no later POST occurs. |
| I17 | A fire causes more than one provider execution, or loses its job across a crash. | Crash probes at three points with a real provider process and a counting stub behind it: before submission (job row exists, no POST yet), after acceptance (job `provider_owned`), and after terminal persistence but before fire bookkeeping (job `completed`, fire not yet linked). After each, the next pass or pump resumes by the persisted `dispatch_request_id`; assert exactly one provider execution, the fire ends `completed` with that job's result, and no second request id was ever generated. |
| I18 | Fires with different invocation parameters share a job, or confirmation crosses fires. | Two rules, one PDF, one parameterised fixture capability: equal parameters produce one job referenced by two fires; unequal parameters produce two jobs on one lane, serialised; one rule with `confirm_each: true` and one without, equal parameters: the unconfirmed fire runs alone, the confirming fire stays `awaiting_confirmation` with no job, and after confirmation links to the same job's result without a second execution. |
| I16 | The unattended path fails under the systemd sandbox. | Live: the installed unit (`ProtectHome=read-only`, `ReadWritePaths` to the state directory) runs one pass that discovers Invoice Processor under `XDG_RUNTIME_DIR`, reads the entitlement under `~/.config`, takes the lane and source locks beside the database, and completes one `invoice.extract` job with no desktop open. This is the end-to-end proof; the two-invoice proof the queue contract owes (`docs/CONTRACTS.md:1265-1267`) is folded into it. |

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
| `Local Connect Status.md:53` | queue closed | contradicted | no caller of `connect.queue.pump` (C1) |
| `Local Connect Status.md:52` | unattended permission closed | overstated | feature exists in one repo, no contract (C3) |
| `Product Brief.md:107-116` | Email Watcher automatically hands to Summarizer/Invoices today | contradicted | no Connect call in `service.py` (C2) |
| `Product Brief.md:138` | senders across multiple accounts | contradicted | one global allowlist, one active account (C5) |
| `Product Brief.md:150-152`, `342` | bytes fetched only when you act | contradicted in part | pump re-fetches unattended (C6) |
| `Product Brief.md:252-254` | gate refuses to run an automation | confirmed, with the authorized-write exception | `service.py:797` (K2) |
| `Product Brief.md:261-262` | halts in `ambiguous` or `manual_review` | confirmed, incomplete | fourteen states (K3) |
| `Product Brief.md:434-435` | "A queue ... in development" | confirmed | consistent with C1; the Status file, not the brief, is wrong here |
| `docs/CONTRACTS.md:858-859` | engine pump remains a later slice | contradicted | #121 landed it (C4) |
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
3. Schema 20: rules, rule versions with `accepted_at`, fires with
   `dispatch_request_id`, `rules_evaluated_version` (migration sets `0` on
   already-analysed messages); the seeded system rule, enabled; the engine
   core with I1, I2, I9, I11, I14.
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
