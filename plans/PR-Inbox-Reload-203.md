# Inbox refresh ownership — #203

Status: proposed contract only. No implementation, installation or live setting change.
Base: `9e33e809719c719fff14169180d8b09b7c3924e7`.

## Root cause

A scope clear and its required load have different owners. The clear in
`desktop/src/main.ts:2539` discards the visible rows, but `renderHealth:4053`
can decline the accompanying load while a mail-account operation is busy. The
next health render sees an already-reconciled scope and no longer requests it.
The busy operation's failure path refreshes health, not the missing load.
Introducing account-reconciliation/health gate: `8723b849`.

A separate Boolean, `inboxReloadAfterMutation`, and
`resumeInboxReloadAfterMutation:2603` remember only “load page one.”
`refreshLoadedInboxSpan:2807` loses its target when `loadInbox:2721` declines
work during a removal. Removal completion later reloads only page one. The
Boolean replay was introduced in `680f5d25`; the bounded span reader predates it
(`b5ea470f`). No later filter or timer can repair the lost request information.

Current-main correction to the old issue: `67bcd752` makes both removal finally
blocks enqueue a first-page refresh unconditionally. A cleared scope during a
removal therefore recovers today. Preserve that control; do not claim it is
still broken. The account-operation loss and loaded-span loss are reproduced.

## Validated inventory and reproduction

The durable `reproduction.mjs` executes the current production bodies for scope
clearing/reconciliation, health rendering, mailbox mutation admission, page and
span loading, and deferred removal replay. Node `v26.7.0` strips only TypeScript
annotations. DOM rendering and backend responses are fixture boundaries. This
is not a mounted or installed UI proof.

At the pinned base:

| Sequence | Observed result |
|---|---|
| Unblocked active-account scope change | one request, 25 rows, loaded |
| Scope reconciled while mail-account operation is held, then fails; follow-up health repeats the same scope | zero requests, zero rows, pending |
| Cleared scope during removal, then existing completion replay | one request, 25 rows, loaded (control) |
| Refresh of 50 loaded rows during removal, then existing completion replay | one request, only 25 rows (span lost) |

The probe uses real owners and fake data; its removal-completion statements are
explicitly reproduced rather than invoking a real destructive command. Preserve
this fixture as regression coverage, extend it to invoke the actual command
completion wrappers, and disclose that extension. No model, prompt, provider
credential or performance setting participates.

## Required behavior

### R1 — retain the current request before a busy guard

One frontend owner records refresh intent for the current `inboxQueryEpoch`:
first page or previously loaded span. Scope clearing registers its first-page
obligation as part of that same transition, before any caller can skip a load.
Sender selection, account Apply/Reset, active-account reconciliation and refresh
callers use this owner. No per-caller retry flags or timer workaround.

Deletion/clear or mail-account activity can postpone a current refresh, but
cannot erase it. Reattempt when the final blocking operation settles, on either
success or failure, and when existing configuration admission becomes ready.
Do not invoke the backend while the relevant busy/admission guard is closed.
This coordinates local Inbox reads; it does not initiate a mailbox poll.

### R2 — preserve query and refresh intent

For the same epoch, a pending span intent keeps the largest requested loaded-row
target. A page-one invalidation must not downgrade that pending span. Restore
up to that target through the existing cursor path, stopping at end of results.
An explicit query/filter/sender/account change advances the epoch and replaces
old work with the new scope's first-page request; never carry an old count or
cursor into a new query. A later span for the new query uses only its own rows.

A user Load more action retains its current next-cursor semantics. Never reuse
an append cursor after scope change or treat an absent cursor as work to retry.
If a span is blocked between pages, retain its original target and current query
intent; on resumption refresh that query consistently, without duplicate rows.

### R3 — coalesce safely, preserve freshness

Keep at most one pending intent per current query and one active refresh for it.
Repeated wakeups coalesce; new work arriving during a refresh is not cleared by
that refresh's completion. Superseded transport may finish but cannot commit
or delay the new scope's own load. Exercise both completion orders.

Existing request-generation, query-epoch, mailbox-effect-scope, configuration,
automation-decision and removal fences remain authoritative. A stale effect is
superseded, not a reason to replay that old event with a fresh revision or null
scope. A current scope-clear obligation is independent and survives until its
own valid load, supersession or configuration teardown. Account-operation
revision updates occur before the settlement wakeup.

### R4 — preserve truthful completion and failure

`loadInbox` callers that require their own committed projection keep that
contract: deferred, stale and failed work never reports a successful commit.
In particular, automation decision/calendar receipt acknowledgement still waits
for its qualifying committed projection. A later unrelated load cannot falsely
acknowledge the original caller's receipt.

Keep existing pending/loading/loaded/failed wording and transitions. Blocked
work is pending, not indefinitely “Loading.” A real backend failure retains the
existing error and rows, ends that attempt, and does not create a retry loop.
Only a new explicit request/event or independently pending current intent can
start another attempt. No backoff, timeout, interval or paging-cap changes.

## Required change surface and single owner

- `desktop/src/inboxReload.ts`: bounded, typed frontend refresh-intent owner.
- `desktop/src/main.ts`: integrate scope clear, first-page/span entry points,
  removal/account settlement and configuration readiness with that owner.
  Keep page commit/freshness decisions in the actual page loader.
- Replace `inboxReloadAfterMutation` and `resumeInboxReloadAfterMutation` and the
  duplicated sender-only/account-health deferral branches with the shared owner.
  Preserve the removal completion behavior proved by the control.
- `desktop/test/inboxReload.test.ts`: retained production-body reproduction,
  held-promise integration and owner tests. Existing `inboxFilters`,
  `gmailLabels`, `connectAvailability`, `automationDecision`, `calendarProposal`
  and removal lifecycle tests only where their wiring changes; preserve every
  behavior assertion. Update brittle source anchors to behavioral assertions,
  not weaker success criteria.
- Link this canonical contract from the earlier empty-state plan if needed;
  do not restate these rules in multiple documents.

## Explicit non-scope

No backend, Rust, schema, provider, authorization, deletion semantics, polling,
page size, receipt retention, dependencies or UI copy/layout changes. No durable
queue, background worker, live settings, installation or promotion. Do not fold
#145 installed action/latency qualification into this PR. No runtime diagnostic
or reusable harness tooling. Keep the implementation bounded to this owner and
its direct wiring/tests; stop for a revised contract if more is required.

## Verification plan

1. Declare fail-first tests for the held failed-account operation (current
   scope eventually loads) and deferred span (restores its prior row target).
   Preserve the unblocked/removal first-page controls and run actual completion
   wrappers, not just a new queue helper.
2. Table of sender/account Apply/Reset/reconciliation crossed with removal,
   mail-operation and combined blockers; release in both orders, including
   rejection, cancellation and overlapping operations. Final-settlement wakeup
   must be necessary: removing it must fail the regression.
3. Span requests before/between pages; targets 0, one page, multiple pages and
   exhausted results. Coalescing first/span requests must not shrink the target.
4. New scope while old request is held; old success and failure in either order.
   No stale rows, cursor, counts, loading state or receipt acknowledgement may
   leak into the new scope. Configuration teardown/ready cannot replay old work.
5. Current backend failure remains visible without hot retry; later explicit
   retry succeeds. Automatic refresh and awaited receipt consumers retain their
   distinct commit guarantees. Mutate the intent type or epoch/wakeup handling
   to prove the relevant regression fails.
6. Run focused frontend tests and the TypeScript/Vite build, then required CI
   and exact-head independent review. Exercise the integrated UI with disposable
   held/failing backend responses for visible state; no production account data.
   Installed platform claims require separate native proof.

## Implementation summary

Contract only. Production and test files are unchanged.

## Cold diff audit

Only this plan is added. It narrows the historical issue using current owner
reproduction, identifies the introducing commits, and defines one owner and
regression/commit requirements without changing runtime behavior.

## Gap audit

NOT DONE — proposed contract awaits oversight review and operator acceptance.
Implementation, regressions and native/UI qualification have not been performed.

## Evidence aliases (SHA-256)

- `reproduction.mjs`: `dc40a8d8f14d3b82ef505ab7a041bc25f4ea9f5161f7c59fed99ecb4415bf24e`
- `reproduction.json`: `1a0fa8ae110ce364bfb00a75761734f2ce960a7219717c249f7742e94e91a261`
- `source-receipt.json`: `38058099da41355ea9eaba7f980a8b4a239a456d8984fff286d8d0cc33744233`
- `owners.txt`: `05139d41df2081f3f6376b0b9daf6597277f2f58eb8e6ce92579f238930279a6`
- `account-owners.txt`: `b0117160f05e81f8aea0164aa498b857109738591ff5da61919d97c482ae4b65`
