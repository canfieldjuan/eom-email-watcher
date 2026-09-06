# Calendar capability and email automation contract

Status: **proposed; not implemented**

This document freezes the boundary for adding Microsoft 365 calendar operations
and the first email-driven automation to Email Watcher. It is an implementation
gate, not a claim that calendar or automation behavior exists today.

The existing mailbox watcher, Local Connect consumer, and monthly Gmail sender
remain authoritative until later reviewed slices implement this contract.

## Verified baseline

The contract starts from these current-code facts:

- Microsoft 365 mail uses an MSAL public desktop client against
  `login.microsoftonline.com`, defaults to the `organizations` tenant, requests
  only delegated `Mail.Read`, and polls the Microsoft Graph v1.0 Inbox message
  delta endpoint.
- Generic IMAP is a separate mailbox adapter. Calendar work must not be added to
  the IMAP adapter merely because Microsoft mail and calendar share an account.
- Outbound email is an EOM-only Gmail capability using a separate token that
  requests only `gmail.send`. There is no Microsoft mail-send implementation.
- The public Microsoft mailbox setup is read-only. It must never request a
  calendar write grant as a side effect.
- Email Watcher and Document Summarizer already validate the same signed Local
  Connect entitlement-v1 envelope and the `connect.capability_exchange` feature.
  The entitlement format already supports multiple feature identifiers.
- Email Watcher is a Local Connect consumer only. It has no provider listener,
  manifest route, job endpoint, or provider registration lifecycle.
- Current email analysis can classify a message as `scheduling`, but it does not
  extract a calendar proposal, candidate times, attendees, or a referenced
  event.
- The repository has no calendar-provider adapter and no automation run ledger.
  A systemd `OnCalendar` timer and validation of a calendar date are unrelated
  uses of the word "calendar."

## Definitions

### Capability

A **capability** is one bounded operation an application performs and may
advertise over Local Connect. Examples are `calendar.read`,
`calendar.propose_event`, `calendar.write`, `mail.send`, and
`document.summarize`.

A capability declaration describes the operation, accepted and produced
artifact types, input limit, parameters, effects, and confirmation requirement.
Discovery and invocation over Local Connect require an active signed entitlement
whose feature list contains `connect.capability_exchange`.

A capability is not a triggered workflow. Advertising `calendar.read` does not
authorize an application to monitor email, decide to schedule something, or
write an event.

### Automation

An **automation** is a durable, triggered workflow that composes one or more
capabilities, owns a run ledger, and records each state transition and operator
decision. Any external write requires explicit user confirmation of the exact
proposed effect before the write begins.

Starting or resuming an automation requires an active signed entitlement whose
feature list contains `connect.automations`. This feature lives in the same
entitlement-v1 file, is verified by the same embedded keyring, and is independent
of `connect.capability_exchange`.

The automation feature is additive, not substitutive:

- `connect.automations` never grants Local Connect discovery or invocation;
- `connect.capability_exchange` never grants automation execution; and
- a workflow that uses a capability gated by `connect.capability_exchange`
  must pass both gates at the boundary where each applies.

Calendar is a capability. "A watched email describes a schedule change, propose
an event, and write it after the user confirms" is an automation.

## Ownership boundary

Email-driven automations live in Email Watcher because Email Watcher owns the
trigger and mailbox state. This work does not create a new application or a
shared database.

Email Watcher owns:

- watched-sender admission;
- the explicitly selected email and derived scheduling extraction;
- the automation run, current-state projection, and immutable event history;
- the confirmation UI; and
- the Microsoft calendar tokens it receives for this account.

Other Connect applications receive only explicit capability artifacts. They do
not receive mailbox credentials, unrestricted mailbox access, Email Watcher's
database, or raw email bodies by implication.

## Microsoft authorization profiles

All Microsoft grants use the existing MSAL public client, authority, tenant, and
selected work-or-school principal. They are separately initiated consent flows
and separate private token-cache files:

| Profile | Exact delegated scope | Purpose | Token boundary |
| --- | --- | --- | --- |
| Mailbox read | `Mail.Read` | Existing watched-mail polling | Existing per-account MSAL cache; unchanged |
| Calendar read | `Calendars.Read` | Read the signed-in user's calendar view | New calendar-read cache |
| Meeting proposal | `Calendars.Read.Shared` | Call `findMeetingTimes` for organizer and attendee availability | New proposal cache |
| Calendar write | `Calendars.ReadWrite` | Create a confirmed event in the signed-in user's calendar | New calendar-write cache |

`Calendars.Read.Shared` is a distinct proposal grant because Microsoft Graph's
`findMeetingTimes` v1.0 operation does not document `Calendars.Read` as a
sufficient delegated permission. It supports delegated work-or-school accounts,
not personal Microsoft accounts.

No cache may be reused across profiles, and no broader cache may silently satisfy
a narrower setup path. The mailbox **Add account** path requests only
`Mail.Read`; calendar read, proposal, and write consent are separately initiated
user actions. Acquiring read or proposal consent must not request
`Calendars.ReadWrite`.

Because every flow uses one Microsoft public-client registration, cache
separation is an application routing, audit, and least-requested-scope
convention—not a cryptographic OAuth isolation boundary. Microsoft consent for a
user/client can be cumulative. Every acquisition call and Graph operation must
therefore enforce its exact scope allowlist even when the tenant has previously
consented to broader scopes. Hard isolation would require separately registered
client IDs and is deferred as an infrastructure/product decision.

For a generated Microsoft account ID, caches are derived internally beneath the
existing private `mail-accounts` directory; token paths never come from UI or
database input:

```text
<account-id>.msal-cache.json                    # existing Mail.Read
<account-id>.calendar-read.msal-cache.json      # Calendars.Read
<account-id>.calendar-proposal.msal-cache.json  # Calendars.Read.Shared
<account-id>.calendar-write.msal-cache.json     # Calendars.ReadWrite
```

Every completed calendar grant must resolve to the same tenant-scoped immutable
Microsoft principal selected for the automation. The durable identity uses the
MSAL home-account identifier together with tenant/object claims when available;
a normalized email address is display/diagnostic metadata, not an identity key.
An immutable-principal mismatch is rejected before a cache replaces the previous
cache or changes the profile's current consent state. The run binds that same
principal before confirmation.

### Consent state

Each calendar profile has an independent public state:

```text
not_requested -> consent_pending
consent_pending -> ready | rejected
ready -> consent_pending | revoked
rejected | revoked -> consent_pending
any state --disconnect--> not_requested
```

`consent_pending` is nonterminal and is not an operational failure. It means the
interactive flow requires a user or tenant administrator to complete consent.
The default Microsoft delegated permissions above do not require administrator
consent in every tenant, but a work-or-school tenant's consent policy may require
administrator approval. Mail polling continues in every calendar consent state.

Each calendar profile has its own explicit disconnect action. Disconnect takes
the existing mailbox operation lock, safely removes only that profile's cache,
resets that profile to `not_requested`, and immediately makes the dependent
internal capability unavailable. A rejected or revoked profile may begin a new
explicit consent attempt. Disconnecting the Microsoft mailbox also disables all
email-driven calendar automation even if a separately consented calendar cache
remains; it does not silently delete those separate grants. The user can remove
each calendar grant independently.

## Microsoft calendar adapter

The calendar adapter is a sibling of the Microsoft mailbox adapter. It may share
public-client construction, authority validation, private-cache installation,
Graph URL validation, and bounded HTTP behavior, but it does not add calendar
branches to the provider-neutral mailbox protocol.

### Read and change tracking

Calendar change tracking uses:

```text
GET https://graph.microsoft.com/v1.0/me/calendarView/delta
    ?startDateTime=<inclusive ISO-8601 boundary>
    &endDateTime=<exclusive ISO-8601 boundary>
```

The initial request fixes an explicit view window. The adapter maintains a
durable per-account, per-window event projection. During each delta round it
applies event upserts and `@removed` tombstones to that projection. The complete,
opaque `@odata.nextLink` or `@odata.deltaLink`, all projection changes, and the
round state advance commit in one SQLite transaction; a crash cannot preserve a
new cursor while losing its event mutations.

The stored cursor and projection are bound to the same window; changing the
window starts a new initial round and projection. Subsequent delta responses are
changes, not a complete view, so `calendar.read` is served from the durable
projection after a completed round. Calendar delta does not support the mailbox
delta query shape: the implementation must not copy the mail adapter's `$select`,
`$filter`, or `changeType` parameters.

"Same way mail is polled" means the same safety properties—bounded responses,
validated Graph-only continuation URLs, opaque cursor persistence, pagination,
stale-cursor recovery, and atomic state advancement—not the same endpoint or
query parameters.

Graph webhooks are excluded. They require a publicly reachable HTTPS callback,
which conflicts with this local-first deployment.

### Meeting proposals

Candidate availability uses:

```text
POST https://graph.microsoft.com/v1.0/me/findMeetingTimes
```

The proposal operation is read-only even though Graph exposes it as `POST`. It
may return no suggestions and a reason; that is a valid domain result, not a
transport failure. A no-suggestions result records the bounded request and
Graph reason, transitions the run from `proposing` to `manual_review`, and asks
the user to revise the scheduling constraints; it never creates an empty
confirmation. It cannot create, update, invite, or cancel an event.

### Confirmed writes

After confirmation, creation uses:

```text
POST https://graph.microsoft.com/v1.0/me/events
```

The durable run records a stable `transactionId` before the first POST and reuses
it for any permitted retry so Microsoft Graph can suppress duplicate event
creation after a lost response. A timeout after submission is ambiguous, not
proof of failure; the run remains unresolved until reconciliation establishes an
authoritative outcome. Reconciliation searches the bounded relevant calendar
view for the persisted transaction ID. A matching event completes the original
run; an authoritative Graph rejection fails it; absence alone is not proof that
creation failed and leaves the run truthfully unresolved.

Creating an event with attendees causes Microsoft 365 to send meeting
invitations. The durable proposal and confirmation surface must describe that
outbound communication as an external effect, not merely label it a calendar
write. The confirmed attendee set is exactly the invitation recipient set.

A Teams meeting request sets both:

```json
{
  "isOnlineMeeting": true,
  "onlineMeetingProvider": "teamsForBusiness"
}
```

only after the target calendar declares `teamsForBusiness` in
`allowedOnlineMeetingProviders`. Otherwise the proposal remains valid without a
Teams link or asks the user to revise it; the adapter must not falsely report a
Teams meeting.

### Time-zone invariant

Every extracted time, proposal, confirmed event, ledger event, and Connect
artifact carries:

- an explicit instant or local date-time with UTC offset; and
- an explicit canonical IANA time-zone identifier.

Naive local times are invalid. The application stores IANA identifiers as its
domain value. The Microsoft adapter converts that identifier to a time-zone name
accepted by the mailbox server when required and preserves the IANA source value
in its own provenance. Daylight-saving resolution happens before confirmation;
ambiguous or nonexistent local times fail closed to clarification.

## Entitlement and visibility boundary

The calendar operations in this contract are internal Email Watcher
capabilities. Calendar setup and calendar availability appear only when the
existing signed entitlement contains `connect.capability_exchange`. The
email-driven automation UI appears, and an automation may start or resume, only
when that same entitlement contains both `connect.capability_exchange` and
`connect.automations`. An automation-only entitlement cannot expose calendar
operations, and a capability-exchange-only entitlement cannot expose or run the
automation. Every operation also requires its corresponding Microsoft grant.
Both entitlement features and consent are rechecked immediately before
admission.

Email Watcher remains a Local Connect consumer. It does not publish a manifest,
open a provider listener, accept Connect jobs, write provider registrations, or
add calendar schemas to `connect-contracts` under this contract. Advertising
`calendar.read`, `calendar.propose_event`, or `calendar.write` to other
applications is a later contract with its own interoperability and threat review.

## First automation: scheduling email to confirmed event

### Trigger and admission

The trigger is a newly analyzed email that has already passed the exact
watched-sender gate. A generic `scheduling` category alone is not authority to
create a proposal. The automation runs a separate, strict scheduling extraction
over the admitted email data.

Email subject, body, quoted history, attachment names, and extracted strings are
untrusted data, never instructions. They cannot select a tool, bypass consent,
alter an entitlement decision, or confirm their own proposal.

### Structured extraction

The extraction schema contains only:

- intent: new meeting, reschedule, cancellation mention, or unclear;
- proposed time ranges, each with offset and IANA zone;
- attendees with normalized email addresses;
- referenced event identity or bounded human reference, when present;
- source evidence for each populated field; and
- confidence/ambiguity reasons.

Deterministic validation rejects invented attendees, times unsupported by source
evidence, invalid addresses, naive times, impossible ranges, and partial or
unknown schema members.

Version 1 admits only a clear new-meeting request to proposal and creation.
Reschedule and cancellation intents are durable `manual_review` outcomes because
`POST /me/events` cannot update or cancel the referenced event; they create no
proposal and make no Graph write. Supporting those intents requires a later
contract for exact event matching plus confirmed update/delete operations.

An unclear or otherwise ambiguous extraction likewise creates no meeting
proposal and makes no calendar write. It records an `ambiguous` outcome and emits
only a normal Email Watcher notification that a schedule mention needs human
review.

### Durable run ledger

The automation ledger follows Document Summarizer's established state-machine
pattern:

- one current-state projection per run with a monotonically increasing
  `state_version`;
- one immutable, ordered event per accepted transition;
- expected-state and expected-version compare-and-set on every mutation;
- the projection update, related durable data, and event append in one SQLite
  transaction; and
- a rejected or stale transition changes nothing and appends no event.

Run admission is atomic with analysis completion. When a watched message becomes
durably analyzed as scheduling, insertion of the `detected` run and its first
event occurs in the same SQLite transaction as that analysis state change. A
unique `(provider, account_id, provider_message_id, automation_id,
automation_version)` key makes retries idempotent. A crash can neither lose an
eligible trigger after marking it analyzed nor create two runs for the same
automation version.

Minimum flow:

```text
detected -> extracting -> ambiguous | manual_review | source_unavailable
                      \-> proposing -> awaiting_confirmation
                                   \-> manual_review
awaiting_confirmation -> proposing  # revise or expire and re-propose
awaiting_confirmation -> declined
awaiting_confirmation -> write_authorized -> writing -> completed
                                               \------> failed | unresolved
unresolved -> reconciling -> completed | failed | unresolved
```

`detected` and `extracting` are recoverable work states. Each extraction attempt
fetches the source message through the mailbox adapter and holds the raw content
only in process memory. A restart retries either state. If the provider proves
the source message is no longer retrievable, the run records a structured
`source_unavailable` terminal outcome and emits a human-review notification; it
must not remain indefinitely in `extracting`. Raw content is not added to the
message or automation tables to provide this recovery.

The run and event history record the source message identity without copying the
raw body, extraction/result schema versions, proposal content and hash, calendar
account identity, user decision, decision timestamp, confirmed proposal hash,
stable Graph `transactionId`, Graph event identity when known, and structured
failure or unresolved reason.

No state may claim `awaiting_confirmation` before the proposal is durable. No
state may claim `write_authorized` unless the confirmation event identifies the
exact durable proposal version and hash. No state may claim `completed` before
the created event identity and write outcome commit durably.

### Confirmation boundary

Email Watcher renders the proposal using its own native UI and shows the exact
subject, attendees, start, end, IANA zone, location, and Teams-link choice. When
attendees are present, it explicitly states that confirmation will send meeting
invitations to those addresses. The user may confirm that exact version, revise
it into a new proposal version, or decline it.

Confirmation is single-run, single-proposal, and non-transferable. Changing any
effect-bearing field invalidates prior confirmation. Each proposal durably
records its availability-observation time and expires at the earlier of fifteen
minutes after that observation or the proposed start. Confirmation admission
must compare the current time with that expiry and require a future start. An
expired proposal returns to `proposing`, refreshes availability, and requires a
new proposal version and confirmation; it cannot authorize a write. Repeated
clicks and stale UI versions cannot create another write. Version 1 has no
automatic write mode.

## Acceptance evidence for later implementation

Implementation is not complete until all of the following pass against current
merged code:

1. A fixture proves Microsoft mailbox setup requests exactly `Mail.Read` and
   never requests a calendar scope.
2. Separate fixtures prove calendar read, proposal, and write setup request only
   their exact profile scope and write distinct private caches.
3. Scope-boundary fixtures prove that broader tenant consent or another local
   cache cannot make a call site request a scope outside its exact allowlist.
4. A cross-principal negative fixture proves a mismatched grant cannot replace
   the existing cache, change its consent state, or bind to an automation run.
5. Consent fixtures prove rejected and revoked profiles can restart explicit
   consent, and disconnect removes only the selected cache and resets that
   profile to `not_requested`.
6. A live work-or-school Microsoft 365 test account proves calendar-read consent
   and a complete `/me/calendarView/delta` round with persisted continuation.
7. An initial-plus-subsequent delta fixture proves unchanged events remain,
   tombstones remove deleted events, and cursor/projection changes roll back
   together at an injected crash point.
8. The live account proves proposal consent and a real `findMeetingTimes` domain
   result; a no-suggestions fixture records the reason, reaches `manual_review`,
   and never creates an empty confirmation.
9. A watched-sender scheduling fixture reaches a durable proposal and native
   confirmation UI without writing an event.
10. Atomic-admission and post-admission crash probes prove analysis completion
    cannot lose or duplicate the one run keyed to the source message and
    automation version, and restart can resume `detected` or `extracting`.
11. A missing-source fixture proves a provider-confirmed deleted or moved source
    reaches `source_unavailable`, emits human-review notice, stores no raw body,
    and does not remain stuck in an active extraction state.
12. Ambiguous, reschedule, and cancellation negative controls record their
   non-writing outcomes, emit the review notification, and produce no proposal
   and no Graph write request.
13. A live, explicitly confirmed new-meeting proposal creates one event through
    the separate write grant, visibly warns that attendee invitations will be
    sent, and records its event identity and provenance.
14. Lost-response and repeated-confirmation probes reuse one transaction ID,
    cannot produce duplicate event work, and prove reconciliation can move an
    unresolved run to `completed` or `failed` only from authoritative evidence.
15. An expired-proposal fixture proves a delayed confirmation performs no write,
    refreshes availability into a new proposal version, and requires new
    confirmation; a proposal whose start has passed cannot be confirmed.
16. Separate disconnect fixtures remove each calendar cache, disable its
    dependent capability, reset its state to `not_requested`, and leave unrelated
    grants intact; mailbox disconnect disables email-driven automation without
    silently deleting calendar grants.
17. An entitlement matrix proves that capability exchange alone exposes
    calendar setup and, when the corresponding read grant is ready, calendar
    availability; automations alone exposes neither calendar nor automation;
    and both features expose automation subject to the corresponding Microsoft
    grants. Missing, pending, or revoked consent leaves mailbox monitoring
    healthy and truthfully reports calendar unavailability.

Live evidence must identify the tested application revision and sanitized
account/tenant class. A mocked Graph response cannot substitute for the required
live consent, read, proposal, and confirmed-write proof.

## Landing order

Later implementation remains split into reviewed vertical slices:

1. feature-aware entitlement lookup using the existing signed file and keyring;
2. separate Microsoft calendar grants and read adapter;
3. scheduling extraction and immutable automation ledger;
4. native proposal/confirmation UI and private confirmed-write adapter; and
5. live acceptance evidence.

No implementation slice may weaken the existing mailbox read-only path or claim
completion from mocks alone.

## Explicit non-scope

- Gmail Calendar;
- Microsoft mail sending;
- automatic or model-confirmed calendar writes;
- any Email Watcher Connect provider surface, calendar capability advertisement,
  provider listener, provider registration, job endpoint, or new Connect schema;
- Graph webhooks or a public callback service;
- cross-machine or cloud Connect;
- a workflow builder, generic rules engine, or new automation application;
- Document Summarizer or Invoicing changes;
- shared databases, shared credential stores, or unrestricted mailbox handoff;
- personal Microsoft-account support for `findMeetingTimes`; and
- weakening the public Microsoft mailbox read-only promise.

## Rejected alternatives

- **One broad Microsoft cache:** rejected because it obscures call-site scope,
  consent intent, removal, and diagnostics. Separate caches remain an application
  convention, not hard OAuth-client isolation.
- **Combine calendar read and proposal under `Calendars.Read.Shared`:** rejected
  for version 1 so ordinary calendar viewing still asks only for
  `Calendars.Read`. The additional prompt is a deliberate consent-friction trade,
  not a claim of cryptographic isolation.
- **Separate Microsoft client registrations now:** deferred because they provide
  stronger scope isolation but require release-time Entra registration and
  onboarding decisions beyond this automation contract.
- **Use `Calendars.Read` for `findMeetingTimes`:** rejected because Microsoft does
  not document it as a sufficient delegated permission for that operation.
- **Copy the mail delta URL builder:** rejected because calendar-view delta has a
  fixed date window and does not support the mail query parameters.
- **Graph webhooks:** rejected because a local desktop application has no public
  HTTPS notification endpoint.
- **New automation app or shared database:** rejected because the trigger owner
  must own its workflow state and confirmation boundary.
- **Model output as confirmation:** rejected because email and model output are
  untrusted data, not user authorization.
- **A second entitlement format:** rejected because the signed entitlement-v1
  feature list already represents independent features.

## External API basis

The Microsoft boundary above was checked against these primary v1.0 references:

- [calendar-view delta](https://learn.microsoft.com/en-us/graph/api/event-delta?view=graph-rest-1.0)
- [`findMeetingTimes`](https://learn.microsoft.com/en-us/graph/api/user-findmeetingtimes?view=graph-rest-1.0)
- [Microsoft Graph permission reference](https://learn.microsoft.com/en-us/graph/permissions-reference)
- [create event](https://learn.microsoft.com/en-us/graph/api/user-post-events?view=graph-rest-1.0)
- [Outlook event as an online meeting](https://learn.microsoft.com/en-us/graph/outlook-calendar-online-meetings)
- [`dateTimeTimeZone`](https://learn.microsoft.com/en-us/graph/api/resources/datetimetimezone?view=graph-rest-1.0)
