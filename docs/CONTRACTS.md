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
- a workflow that invokes a Connect capability must pass both gates at the
  boundary where each applies.

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
selected work-or-school account identity. They are separate consent profiles and
separate private token-cache files:

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

For a generated Microsoft account ID, caches are derived internally beneath the
existing private `mail-accounts` directory; token paths never come from UI or
database input:

```text
<account-id>.msal-cache.json                    # existing Mail.Read
<account-id>.calendar-read.msal-cache.json      # Calendars.Read
<account-id>.calendar-proposal.msal-cache.json  # Calendars.Read.Shared
<account-id>.calendar-write.msal-cache.json     # Calendars.ReadWrite
```

Every completed calendar grant must resolve to the same normalized Microsoft
account identity selected for the automation. An identity mismatch is rejected
before a cache replaces the previous cache.

### Consent state

Each calendar profile has an independent public state:

```text
not_requested -> consent_pending -> ready
                            \----> rejected
ready -> consent_pending | revoked
```

`consent_pending` is nonterminal and is not an operational failure. It means the
interactive flow requires a user or tenant administrator to complete consent.
The default Microsoft delegated permissions above do not require administrator
consent in every tenant, but a work-or-school tenant's consent policy may require
administrator approval. Mail polling continues in every calendar consent state.

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

The initial request fixes an explicit view window. The adapter then follows and
persists Graph's complete, opaque `@odata.nextLink` and `@odata.deltaLink` URLs.
The stored cursor is bound to that window; changing the window starts a new
initial round. Calendar delta does not support the mailbox delta query shape:
the implementation must not copy the mail adapter's `$select`, `$filter`, or
`changeType` parameters.

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
transport failure. It cannot create, update, invite, or cancel an event.

### Confirmed writes

After confirmation, creation uses:

```text
POST https://graph.microsoft.com/v1.0/me/events
```

The durable run records a stable `transactionId` before the first POST and reuses
it for any permitted retry so Microsoft Graph can suppress duplicate event
creation after a lost response. A timeout after submission is ambiguous, not
proof of failure; the run remains unresolved until reconciliation establishes an
authoritative outcome.

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

## Local Connect v2 calendar capabilities

Email Watcher will be a Connect v2 provider as well as its existing consumer.
Before implementation advertises either capability, the language-neutral
schemas and conformance fixtures must be added to the canonical
`connect-contracts` repository and consumed by both sides. This document does not
create an Email Watcher-only wire extension.

Proposed v2 declarations:

| Capability | Accepts | Produces | External effect | Confirmation |
| --- | --- | --- | --- | --- |
| `calendar.read@1.0` | `application/vnd.local-connect.calendar-query+json` | `application/vnd.local-connect.calendar-events+json` | No | No |
| `calendar.propose_event@1.0` | `application/vnd.local-connect.meeting-request+json` | `application/vnd.local-connect.meeting-proposal+json` | No | No |

Both declarations use native v2 action labels/descriptions, explicit bounded
`max_bytes`, bounded parameters, and generic integrity-checked output artifacts.
Availability requires the corresponding Microsoft grant as well as
`connect.capability_exchange`. Entitlement and consent are rechecked immediately
before admission.

`calendar.write` is intentionally **not advertised** in this slice. The first
automation may call its private confirmed-write boundary only from Email
Watcher-owned UI after recording confirmation. Exposing a generic write
capability requires a later contract and threat review.

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

An ambiguous extraction creates no meeting proposal and makes no calendar write.
It records an `ambiguous` outcome and emits only a normal Email Watcher
notification that a schedule mention needs human review.

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

Minimum flow:

```text
detected -> extracting -> ambiguous
                      \-> proposing -> awaiting_confirmation
awaiting_confirmation -> declined
awaiting_confirmation -> write_authorized -> writing -> completed
                                               \------> failed | unresolved
```

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
subject, attendees, start, end, IANA zone, location, and Teams-link choice. The
user may confirm that exact version, revise it into a new proposal version, or
decline it.

Confirmation is single-run, single-proposal, and non-transferable. Changing any
effect-bearing field invalidates prior confirmation. Repeated clicks and stale UI
versions cannot create another write. Version 1 has no automatic write mode.

## Acceptance evidence for later implementation

Implementation is not complete until all of the following pass against current
merged code:

1. A fixture proves Microsoft mailbox setup requests exactly `Mail.Read` and
   never requests a calendar scope.
2. Separate fixtures prove calendar read, proposal, and write setup request only
   their exact profile scope and write distinct private caches.
3. A live work-or-school Microsoft 365 test account proves calendar-read consent
   and a complete `/me/calendarView/delta` round with persisted continuation.
4. The live account proves proposal consent and a real `findMeetingTimes` domain
   result, including the valid no-suggestions case.
5. A watched-sender scheduling fixture reaches a durable proposal and native
   confirmation UI without writing an event.
6. An ambiguous-email negative control records `ambiguous`, emits the review
   notification, and produces no proposal and no Graph write request.
7. A live, explicitly confirmed proposal creates one event through the separate
   write grant and records its event identity and provenance.
8. Lost-response and repeated-confirmation probes reuse one transaction ID and
   cannot produce duplicate event work.
9. Missing, pending, revoked, or feature-incomplete consent leaves mailbox
   monitoring healthy and truthfully reports calendar unavailability.
10. Connect v2 conformance fixtures prove `calendar.read` and
    `calendar.propose_event` disappear when either the capability entitlement or
    corresponding Microsoft grant is unavailable; `calendar.write` never appears.

Live evidence must identify the tested application revision and sanitized
account/tenant class. A mocked Graph response cannot substitute for the required
live consent, read, proposal, and confirmed-write proof.

## Landing order

Later implementation remains split into reviewed vertical slices:

1. canonical `connect-contracts` calendar artifact schemas and fixtures;
2. feature-aware entitlement lookup using the existing signed file and keyring;
3. separate Microsoft calendar grants and read adapter;
4. Connect v2 calendar read/proposal provider surface;
5. scheduling extraction and immutable automation ledger;
6. native proposal/confirmation UI and private confirmed-write adapter; and
7. live acceptance evidence.

No implementation slice may weaken the existing mailbox read-only path or claim
completion from mocks alone.

## Explicit non-scope

- Gmail Calendar;
- Microsoft mail sending;
- automatic or model-confirmed calendar writes;
- advertising `calendar.write` over Connect;
- Graph webhooks or a public callback service;
- cross-machine or cloud Connect;
- a workflow builder, generic rules engine, or new automation application;
- Document Summarizer or Invoicing changes;
- shared databases, shared credential stores, or unrestricted mailbox handoff;
- personal Microsoft-account support for `findMeetingTimes`; and
- weakening the public Microsoft mailbox read-only promise.

## Rejected alternatives

- **One broad Microsoft cache:** rejected because it lets read-only setup acquire
  or reuse write authority and makes revocation/diagnostics ambiguous.
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
