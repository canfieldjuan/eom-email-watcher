# #145 — bounded mailbox backlog progress and polling cadence

Status: **PROPOSED — contract only; no implementation authorized.**
Operator chose “145 next”. #203 remains queued. Oversight contract review and
Juan's explicit acceptance are required before implementation.

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

## Proposed behavior (acceptance clauses)

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

A drained, unknown, inactive, failed or recovery-pending outcome uses the
existing configured/recovery behavior. Restarts retain existing durable
cursors; volatile pending status is not falsely reconstructed from cursor
syntax. The first post-restart check retains configured startup timing.
Manual checks stay one check; they do not launch a hidden background loop.

**C3 — initial latency and compatibility.** Propose a 5-minute default for
new/missing config and the example config. Change the shipped Linux timer to
5 minutes while retaining boot delay, jitter and its deployment/locking gates.
Explicit saved intervals are preserved. Existing customized/installed units
are not rewritten by this PR; normal reviewed cutover adopts the new template.
Desktop and headless cadence remain separately reported: the static systemd
timer does not pretend to read the GUI interval. A retained old timer can
still poll slowly until explicitly upgraded. No live settings changes now.

Qualification target: on an active qualified host with no provider errors,
zero backlog, and bounded fixture processing, a new message is admitted by
the next configured/default check. For the 401-ID fixture, eventual admission
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
  configured cadence for other outcomes; repeat/check/stop races, cancellation,
  idle intervals 1/5/120/1440, settings rejection of 0/false/empty/out-of-range;
  no lock held during the wait; restart resumes saved cursor without duplicates.
- C3: actual default config and shipped timer, explicit saved 120 unchanged;
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

This proposal changes only this document. No runtime or test code changed;
all synthetic reproduction artifacts are retained outside the worktree.
Cold audit must show this contract alone. After acceptance, every changed
assertion maps to C1–C5; no weakened assertions, skips or downstream workarounds.

## Gap audit

**NOT DONE:** baseline reproduced; implementation, regression changes, native
qualification, oversight contract review and operator acceptance remain.
The five-minute cadence/status wording is a proposed product decision, not an
operator acceptance inferred from “145 next”. Keep #145 open until its accepted
target and platform evidence are met; sustained overload remains bounded and
visible; it is not eliminated for arbitrary input rates.

## Durable evidence (SHA-256 aliases)

- `reproduce.py`: `643934994cf26eb72d6ca3d3631d2ba1312e318d4999d4d3a3e6d383aaceec1f`
- `reproduction.json`: `09963e242f3557de07e0d3139273e526b0cf8ce9def5264b58a7938fcd2cfed5`
- `scheduler-owner.rs`: `5e80b748f01a69f2dce974f74ae4f79dcef36bd35703b0c5c69eeabcfb262ce4`
- `scheduler-owner.log`: `127358c374b7fcd72189a3c32ae907f83e2320e12f6b4f227b6381c817915d2d`
- `origin-history.txt`: `11a5d3880583806867c3dd2b9a7d7adcb3752b6ca19d96852691a181ab2803b0`
- `provider-guidance.md`: `690b226705efc5fd2d69ccb4ac463f3d294151720c8378fbee588d6906d1b86a`
