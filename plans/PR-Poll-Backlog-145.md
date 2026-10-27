# #145 — bounded mailbox backlog progress and polling cadence

Status: **ACCEPTED CONTRACT — implementation remains separate.**
Operator acceptance of C1–C5 at `85dc3f3` was relayed verbatim by oversight
on 2026-10-27: Juan said “accept all three”; the relay explicitly identifies
this #145 contract as one of those three (mailbox receipt `8ca31e7e`).
Oversight CONTRACT PASS: review `5661592417` at the accepted head.
Merge this contract-only PR after its exact-head CI and review gates hold;
then implement C1–C5 in a separate PR based on the merged contract, beginning
with fail-first regression tests. No live settings changes, installation or
promotion. #203 remains queued.

## Root cause and measured inventory

Source: `e7887be676cc44c8ef158c7fcb6e0054c07ac58f`.

- `Watcher._check_active`, `src/eom_email_watcher/service.py:2414`, calls
  `changes_since` once, captures that batch and commits its cursor at :2560.
  `_finish_active_result` (:2274–2325) uses `polling_complete` for retention
  but omits ordinary backlog from the public outcome.
- `MailboxChanges.drained` (`mailbox.py:76`) already distinguishes bounded
  continuation from exhaustion. Gmail `_history_changes` (`gmail.py:1012`)
  and IMAP `_incremental_changes` (`imap.py:2006`) bound incremental IDs.
  The cap is correct resource protection; it must not be removed.
- `PollingScheduler::stage` (`desktop/src-tauri/src/scheduler.rs:590`) always
  schedules completion plus configured interval. `next_check_unix_ms` (:473)
  is the delay owner. Config's default (:34) is 120 minutes; the shipped
  systemd timer (:6) is two hours plus its existing randomization.
- Introducing history: original one-read service ancestry `cd5a387`, provider
  abstraction `f026d46`; bounded Gmail continuation `afddebe3`; IMAP incremental
  paging `6d353f28`; desktop interval/default `ec7e7a8d`; two-hour systemd
  cadence `6a1b53a6`. This is a composition of existing owners, not a defect
  introduced by the confirmation repair. Preserve cursor protections.
- Graph `_delta_round` (`microsoft365.py:610`) follows its next links in one
  adapter call. It does NOT have the same 200-ID incremental behavior; no
  universal per-provider throughput claim or Graph re-pagination is made.

Evidence uses existing test transport fixtures, real Gmail/IMAP adapters,
real `Watcher.check` and SQLite, and a fixed service clock. The model is a
stub; mail is synthetic. No live config, mailbox, model, scheduler, timer or
installed app was changed. The scheduler probe compiles the exact unmodified
`next_check_unix_ms` function with deterministic inputs.

| Synthetic arrival burst; watched message last | Gmail batches | Checks before watched admission |
|---|---|---|
| 200 | 200 | 1 |
| 201 | 200, 1 | 2 |
| 401 | 200, 200, 1 | 3 |

IMAP independently produces 200/200/1 and false/false/true drained flags.
For Gmail 401, the first two check results omit any backlog signal. The
compiled scheduler gives relative check times 0/120/240 minutes; supplying
5 minutes to that same function gives 0/5/10. These are inter-check delay
measurements, excluding processing, downtime and jitter, NOT wall-clock SLA
or an implemented fix. One metadata failure propagates, leaves the failed
batch's persisted cursor unchanged, and the next successful check admits the
remaining watched message. Sustained 250 new IDs per check leaves measured
backlogs 50/100/150: no finite bound promises to outrun arbitrary arrivals.

## Accepted behavior (acceptance clauses)

**C1 — one truthful owner.** Add optional `mailbox_poll_state` to check results:
`drained`, `pending`, or `unknown`. The service derives it from successful
provider observations, never counts or decoded cursor text. Pending means a
successful normal incremental batch explicitly returned `drained=False`.
Unknown dominates if any required folder failed, lacked a usable cursor or
has unresolved recovery. Inactive and preview calls cannot request faster
scheduling. Existing clients may omit the field; absence means unknown.

Do not derive pending from `not polling_complete`: `_poll_sent_folder`
currently returns false both for unread pages and errors. Preserve its
retention predicate while carrying a distinct observation at that owner.
Gmail's single mailbox-wide cursor includes scoped Sent observations; do not
add a second Gmail poll. IMAP's available, entitled Sent folder keeps its own
single batch and cursor; unavailable/out-of-scope Sent creates no backlog.

**C2 — bounded progress between checks.** Keep one existing incremental batch
per eligible folder per check and all existing provider limits. After a
successful scheduled check with state pending, the desktop schedules another
check after `min(configured_interval, 5 minutes)` from completion. It releases
all operation/source/host locks between checks; no sleeping inside a database
transaction, repeated engine-call loop, extra threads or parallel provider
reads. Each next check reloads config/admission and mailbox identity normally.
The single scheduler delay owner implements this rule; no UI-owned retry rule.

A `CheckFailed` outcome keeps the existing configured cadence. A
`DeliveryFailed` outcome with successful mailbox state pending uses the
pending cadence: notification transport failure does not undo mailbox progress.
Queued notifications retain their existing retry path. Drained, unknown,
inactive and recovery-pending outcomes keep existing configured/recovery behavior.

Manual checks remain single checks, but a successful manual check observing
pending reports that observation to the same scheduler deadline owner. It may
only pull an enabled scheduler's staged deadline earlier, to
`min(staged_deadline, completion + min(configured_interval, 5 minutes))`.
It never postpones an earlier deadline, enables a disabled scheduler, starts a
second worker or launches a loop. The waiting worker must observe the earlier
deadline. Deadline updates and check admission preserve existing mailbox-operation
exclusion: manual and scheduled checks cannot execute concurrently. The owner
must not lose an earlier manual deadline in a racing scheduled completion.
A manual CheckFailed, unknown, inactive, drained or recovery-pending result
leaves the staged deadline unchanged. A manual DeliveryFailed with successful
pending mailbox work may pull it earlier under the same formula. The UI never
writes or computes scheduler deadlines.

Restarts retain existing durable cursors; volatile pending status is not falsely
reconstructed from cursor syntax. The first post-restart check retains configured
startup timing.

**C3 — initial latency and compatibility.** Propose a 5-minute default for
new/missing config and the example config. Change the shipped Linux timer to
5 minutes while retaining boot delay, jitter and its deployment/locking gates.
All saved intervals are preserved, including values written by initialization.
`initialize_config` (`config.py:4022`) explicitly persists the default, so existing
desktop installs normally retain a saved 120-minute idle interval. Do not guess
whether a saved value was initialized or deliberately chosen. Their idle
first-arrival latency changes only when the owner edits that setting; pending
catch-up is the only immediate cadence improvement from this implementation.

Existing customized/installed units are not rewritten by this PR; headless idle
latency changes only through reviewed unit adoption. The CLI never reads the
poll interval for scheduling. Desktop and headless cadence remain separately
reported: the static systemd timer does not read the GUI interval. A retained old
timer still polls slowly until explicitly upgraded. No live settings changes now.

Qualification target: on an active qualified host with no provider errors,
zero backlog, and bounded fixture processing, a new message is admitted by
the next configured/default check. Existing desktop installations retain their
saved idle interval (normally 120 minutes) until the owner changes it; existing
headless installations retain their old cadence until reviewed unit adoption.
The new default must not be presented as improving their idle first-arrival
latency automatically. For the 401-ID fixture, eventual admission
must use three bounded checks with at most 5 minutes scheduled waiting between
successes, including when the saved idle setting is 120 minutes. Initial
arrival-to-check delay, processing, jitter, lock contention and downtime are
reported separately; never advertise a hard five-minute end-to-end SLA.

**C4 — fairness and visible status.** Complete the existing Sent, pending-work,
reconciliation and notification phases once per check. Catch-up cannot starve
them with an inner Inbox loop. Multi-account scheduling is not added. Preserve
existing active-account and mailbox identity exclusion. Scope changes never
reuse old admission or cursors. Show pending in manual and scheduled results:
“Check finished; more mailbox changes remain. They will be read in later checks.”
Retain existing count/delivery/error details. Do not show “caught up” on
unknown, provider failure, recovery, cancellation or missing legacy fields.
Status is the last observed check, not a durable backlog count or estimate.

**C5 — errors fail closed.** Never advance a folder cursor for a failed capture
batch, claim it drained, suppress the existing error, or resubmit in a tight
loop. Already committed prior batches remain progress; accepted IDs remain
deduplicated. A normal poll error uses existing error cadence, not the new
pending cadence. In particular, Sent error + Inbox pending must not trigger a
faster retry of the failed provider. Keep stale-cursor recovery, retry-after
handling already owned by recovery, retention barriers, invalid-message
policy, authorization and entitlement behavior unchanged. Do not promise new
cross-process provider retry-after enforcement; if current throttling behavior
blocks the target, reproduce and amend this contract before expanding scope.

## Rate and resource effects

No increase in per-check ID limits or provider concurrency. More frequent
checks increase provider traffic: 120 to 5 minutes means 12 to 288 theoretical
idle checks/day (24x), excluding processing/jitter. Existing explicit idle
settings change only while successful pending work is observed. Faster drain
moves the same message reads earlier; Gmail continuation can replay prefix
history/observations, so request volume is not necessarily one request/page.
The fixture measured 200/200/1 metadata requests and one history request per
check; it is not a maximum for live Gmail.

[Google's current quota guidance](https://developers.google.com/workspace/gmail/api/reference/quota)
can differ for older projects. Published new-project metadata reads cost 20
units and history reads 2; this fixture's full metadata-only batch models 4002
units before body/profile/label/other-client costs. Do not assume the account's
quota, change billing, claim this guarantees quota safety, or increase caps.
[Graph guidance](https://learn.microsoft.com/en-us/graph/throttling) warns
against immediate retries and requires honoring provider backoff. There is no
universal IMAP quota; preserve its timeouts/read-only sessions. Provider quota
or account limitations remain explicit qualification limits.

## Required change surface

- `service.py`: `_check_active`, `_poll_sent_folder`, `_finish_active_result`;
  distinguish unknown/errors from successful pending observations, reuse existing
  cursor transaction ownership; `mailbox.py` only if a small internal typed
  observation is needed (no provider cursor format changes).
- `config.py`, `config.example.toml`, `systemd/eom-email-watcher.timer`: C3.
- `desktop/src-tauri/src/lib.rs`: initialization receipt verification currently
  compares the poll interval to 120 (:331); update that strict expected default
  with the Python default and its initialization tests, never relax verification.
  Forward successful manual check observations to the scheduler's single deadline
  owner; do not duplicate the scheduling rule in the command or frontend.
- `desktop/src-tauri/src/engine.rs`, `scheduler.rs`, `desktop/src/main.ts`:
  optional result transport, one scheduler delay rule, truthful status rendering.
- Existing Python service/config/API/CLI tests, Rust scheduler/transport tests,
  desktop result tests and deployed unit proof fixtures: exact C1–C5 assertions.
- `docs/ENGINE_API.md`, desktop/operator scheduling docs: accepted semantics.

## Explicit non-scope and assumptions

No schema/migration, new persisted queue, provider paging rewrite, new auth,
webhooks/IDLE, Gmail stale-cursor recovery redesign, model changes, new account
concurrency, rate-limit bypass, timeout increases, live configuration edits,
installation/promotion, shared-provider work, or #203 Inbox reload changes.
No provider request/credential is needed for contract fixtures. Native installed
qualification later needs a supported host and disposable mailbox fixture;
source evidence alone cannot certify Windows or customer-account latency.

## Verification plan and assertion mapping

- C1: 200/201/401 real-adapter twins; empty/duplicate IDs, pending/true-drained,
  missing field and unknown state; Sent failure dominates faster scheduling.
- C2: held clock proves delay min(config,5) only for successful pending; exact
  configured cadence for CheckFailed and non-pending outcomes; DeliveryFailed
  plus pending keeps catch-up. Manual pending during a staged wait pulls only
  earlier (including a preexisting earlier deadline); a manual attempt during an
  in-flight scheduled check obeys exclusion. Race scheduled completion against
  manual admission, and verify no earlier deadline is overwritten, no disabled
  scheduler is enabled, and the sleeping worker notices the new deadline.
  Repeat/check/stop races, cancellation,
  idle intervals 1/5/120/1440, settings rejection of 0/false/empty/out-of-range;
  no lock held during the wait; restart resumes saved cursor without duplicates.
- C3: actual default config and shipped timer; both initialization-written and
  user-chosen saved 120 stay unchanged, with no automatic idle-latency claim;
  measured first arrival and burst latency with processing/jitter separated.
- C4: Sent/notifications/pending work each get a turn during continuous Inbox
  arrivals; mounted manual/scheduled messages retain errors and do not claim
  drained on unknown. Account changes cannot adopt stale results.
- C5: metadata/network/auth errors, throttle response, malformed/stale cursor,
  duplicate continuation and cancellation; last safe cursor remains; no hidden
  retry. Include the retained synthetic failure/retry twin.
- Declare fail-first regression before implementation: current code omits the
  pending outcome and waits the saved 120-minute interval for pending work.
  Preserve the fixture as owner-level regression, not a text-matching test.
- Adjacent Python/Rust/desktop checks, lint/types/build; exact-head Linux and
  Windows CI plus native installed action/latency proof for claimed platforms.
  No broad suite just to duplicate CI; scope-required gates remain mandatory.

## Implementation summary / cold diff audit

The separate implementation preserves C1–C5 above. No schema, provider cap,
provider request path or installed setting changed.

- C1/C5: `service.py` carries the Sent observation separately from the existing
  retention boolean; `_check_active` aggregates it with Inbox observations and
  `_finish_active_result` reports unknown for preview/recovery. No cursor logic
  changed. No downstream inference from message counts or cursor syntax remains.
- C2: `scheduler.rs` owns `poll_delay`, manual deadline observations and scheduled
  completion. A mutex serializes deadline writes with manual revision tracking;
  the existing gate condition variable wakes a waiter without a lost wake-up.
  Removed the superseded fixed-deadline wait helper. Existing delivery exclusion
  still serializes manual/scheduled mailbox work; no new worker or polling loop.
- C1/C2/C4: `engine.rs` carries an optional state, `lib.rs` forwards manual success
  to the admitted scheduler, and `main.ts` uses `mailboxProgress.ts` for shared
  wording after existing recovery/inactive/current-account gates. `delivery.rs`
  changes only an existing test struct literal for the optional field.
- C3: Python/example defaults and the shipped timer change; Rust initialization
  receipt verification remains an exact comparison. Saved values are untouched.
- API and desktop docs link to the canonical clauses rather than redefining them.

Changed-assertion mapping:

| Test owner / assertions | Contract |
|---|---|
| Service `mailbox_poll_state` tests: real Gmail bounded batches and fresh-owner resume, one watched admission, one batch/check; Sent error dominates pending and Sent still gets one turn; empty/duplicate IDs; preview leaves cursor unchanged; failed capture preserves cursor, retry admits once | C1, C4, C5 |
| Scheduler pending transport through delivery failure; configured/pending cadence at interval boundaries and unknown/legacy/inactive/recovery/error twins | C1, C2, C5 |
| Scheduler manual earlier-only, disabled/stopped controls, both completion orders, repeated observations, return to idle, real sleeping worker wake/stop | C2 |
| Config initialization/missing-value assertions updated to the accepted default; saved values including old initialization unchanged; false/empty settings rejected | C3 |
| Rust real-engine initialization/settings expectations and strict receipt fixtures updated; legacy result literals explicitly lack the optional state | C1, C3 |
| Existing desktop rendering tests extended with manual/scheduled pending and delivery details, unknown/legacy controls and stale-account event exclusion | C4 |

Fail-first probes observed absent service outcomes and absent Rust event state;
initialization returned the old default. A mutation restoring configured cadence
made the held-clock deadline regression fail. The mutation was removed afterward.
Local verification and failure classifications are retained with the implementation
review packet; no passing claim is made for an unrun platform qualification.

## Gap audit

**NOT DONE:** implementation and local regressions are prepared. Exact-head CI,
independent review, and native installed action/latency qualification remain.
Keep #145 open until its accepted target and platform evidence are met. No live
settings, installation or promotion is authorized by this implementation PR.

## Durable evidence (SHA-256 aliases)

- `reproduce.py`: `643934994cf26eb72d6ca3d3631d2ba1312e318d4999d4d3a3e6d383aaceec1f`
- `reproduction.json`: `09963e242f3557de07e0d3139273e526b0cf8ce9def5264b58a7938fcd2cfed5`
- `scheduler-owner.rs`: `5e80b748f01a69f2dce974f74ae4f79dcef36bd35703b0c5c69eeabcfb262ce4`
- `scheduler-owner.log`: `127358c374b7fcd72189a3c32ae907f83e2320e12f6b4f227b6381c817915d2d`
- `origin-history.txt`: `11a5d3880583806867c3dd2b9a7d7adcb3752b6ca19d96852691a181ab2803b0`
- `provider-guidance.md`: `690b226705efc5fd2d69ccb4ac463f3d294151720c8378fbee588d6906d1b86a`
