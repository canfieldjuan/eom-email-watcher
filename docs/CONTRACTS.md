# Calendar capability and email automation contract

Status: **implemented: landing items 1–5 are complete**

This document freezes the boundary for adding Microsoft 365 calendar operations
and the first email-driven automation to Email Watcher. It records the implemented
contract and the live acceptance evidence for the completed landing sequence.

Feature-aware entitlement lookup, isolated calendar grants, calendar-read
projection, strict scheduling extraction, the recoverable automation ledger,
native confirmation, and idempotent confirmed writes are implemented. The
existing mailbox watcher, Local Connect consumer, and monthly Gmail sender remain
authoritative.

Live acceptance was completed on 2026-09-07 with this revision against one
Microsoft 365 work-or-school test tenant. The installed capability entitlement
authorized the isolated read, proposal, and write consent profiles. A bounded
`calendarView/delta` round completed, a real `findMeetingTimes` request returned
a fully available no-attendee proposal, and a fresh process confirmed the exact
durable proposal. Its write ledger reached `completed` with an event identity,
and a subsequent delta round observed that event. The automation portion used an
isolated, ephemeral signed test entitlement carrying both required features; the
installed entitlement did not carry `connect.automations` and was not modified.
The no-attendee event avoided external invitations. The deterministic unclear-
intent control remained non-writing and produced neither proposal nor write
payload.

### Exact-current live acceptance addendum — 2026-09-08

Revision `ab48af93eb2ecffe213bb1eea5ee03a45108d978` was installed as the
systemd service snapshot and exercised with the installed native desktop
against the same class of Microsoft 365 work-or-school tenant. A controlled
watched-sender message whose source contained an ordinary MIME hard wrap
reached `awaiting_confirmation` with the requested Chicago start/end, no
attendees, and the target Microsoft account bound to the durable proposal. A
desktop process restart retained the same proposal and native **Create event**
action.

The native action advanced that run to `completed`. A read-back from Microsoft
Graph matched the durable event and transaction identities, the requested
Chicago times, zero attendees, and no online meeting because the source did not
request one. A subsequent watcher process and bounded calendar-view read found
exactly one event with that transaction identity. The controlled event and
local test rows were then removed, the temporary watchlist entry was removed,
Gmail was restored as the active mailbox, both production timers were active,
and SQLite schema 18 passed `PRAGMA integrity_check`.

The paired ambiguous scheduling control remained non-writing with
`ambiguous_extraction` and produced no proposal. No token, cache path, opaque
Graph continuation, raw message body, account address, tenant identifier, run
identifier, or event identifier is retained in this evidence.

### Attendee-bearing live acceptance addendum — 2026-09-08

Revision `b6a2d2ffef602ea8773e459c75d16456b97bca36` was installed from its
production-profile Debian package and exercised with the native Linux desktop.
The organizer was the existing Microsoft 365 work-or-school test account; a
controlled watched-sender message named one separately licensed Microsoft test
principal as its sole attendee. The resulting proposal survived process
boundaries and reached `awaiting_confirmation`. Before activation, the native
accessibility tree exposed a persistent warning that creating the event would
send an invitation from the selected organizer to the named attendee.

The explicit **Create event** action advanced the durable write to `completed`.
A Graph read-back matched the ledger's event and transaction identities, the
requested Chicago interval, exactly one expected attendee, and no online
meeting. A subsequent watcher round and transaction reconciliation still found
exactly one matching event. The controlled Graph event and all local test rows
were then removed, Gmail was restored as the active mailbox, and the watcher
and monthly timers were active. Controlled source messages remain in the test
mailbox because the mailbox authorization is intentionally read-only.

Two malformed extraction attempts encountered during the repeat remained
fail-closed: one could not prove an attendee address from the quoted evidence,
and one did not produce valid structured output. Both reached manual review
without a proposal or Graph write. This addendum retains no account address,
tenant identifier, principal key, run identifier, transaction identifier,
event identifier, token, cache path, or raw message body.

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
- Email analysis classifies a message as `scheduling`; a separately versioned,
  strict extraction now validates candidate times, attendees, intent, and source
  evidence before the automation can reach `proposing`.
- The Microsoft calendar-read adapter and immutable automation run ledger exist.
  Read-only meeting proposal, native preview, explicit confirmation, and durable
  write/reconciliation are implemented. A systemd `OnCalendar` timer remains an
  unrelated use of the word "calendar."

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
Microsoft principal selected for the automation. The durable identity key uses
MSAL's opaque home-account identifier and tenant-local account identifier. Tenant
and object claims, when returned, are validated against concrete cache identity
and retained as provenance; their later omission cannot change the key. The
multi-tenant authority alias `organizations` is not treated as a concrete tenant
claim. A normalized email address is display/diagnostic metadata, not an identity
key. Schema migration 18 atomically rewrites verified legacy principal-key
references across grants, delta state, automation runs/events, and write
reservations before runtime authorization can compare them. Tenant-local object
identifiers are case-normalized for the v2 key. A later verified reconnect also
rekeys legacy run references when every grant was previously disconnected and
therefore retained no identity metadata for startup migration. If that interactive
authorization omits the concrete tenant/object claims required to reproduce the
v1 key while authorization-dependent legacy work remains, setup fails closed with
`calendar_principal_recovery_required`; it neither installs the grant/token nor
guesses from the `organizations` alias. The user may retry interactive setup.
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

### Landing-item 2 executable boundary

Landing item 2 exposes three independent consent lifecycles through the engine:

```text
calendar.read.status | calendar.read.connect | calendar.read.disconnect
calendar.proposal.status | calendar.proposal.connect | calendar.proposal.disconnect
calendar.write.status | calendar.write.connect | calendar.write.disconnect
```

Each operation accepts only the selected Microsoft provider and generated account
ID. Setup and use require the capability-exchange entitlement, but status and
disconnect remain available after entitlement loss. A profile operation acquires
only its exact scope from the authorization table, writes only its own private
cache, and may not report `ready` unless its immutable principal matches both the
durable grant and the selected mailbox principal. Disconnect removes that cache,
resets only that grant, and, for the read profile, deletes its locally copied
calendar projection and cursor. It does not remove the mailbox or either other
calendar profile.

Status may remain temporarily unavailable when a principal cannot be checked,
but a definitive mismatch between the durable grant and either locally
authorized principal transitions that profile to `revoked`; it never reports a
mismatched profile as `ready` with only `available=false` carrying the error.

Calendar reads use two engine operations:

```text
calendar.read.sync(provider, account_id, window_start, window_end)
calendar.read.events(provider, account_id, window_start, window_end)
```

Both window boundaries are RFC 3339 instants with an explicit `Z` or `±HH:MM`
offset and no more than six fractional-second digits. The parser rejects a
missing or malformed offset, `end <= start`, and a range longer than 366 days.
Accepted boundaries are canonicalized to UTC with microsecond precision; the
exact canonical pair and immutable principal key form the projection identity.
Version 1 retains only the most recently completed window for each account and
returns its complete bounded projection rather than accepting pagination state,
a caller-supplied Graph cursor, or a Graph URL. A successful initial sync for a
different window atomically replaces the prior window, cursor, and event rows.
`calendar.read.events` is an offline projection read: it performs no Graph or
token-endpoint request and does not acquire the global mailbox mutation lock.
It validates the entitlement, selected account, durable ready grant, local
mailbox and calendar cache presence, and projection identity using local state
only. It reads the completed window and all of its events from one SQLite read
snapshot, so a concurrent sync continues to expose the previous complete
projection until the replacement transaction commits. It refuses an
incomplete, missing, differently windowed, differently principaled, or
unavailable grant.

The initial delta request contains only the canonical `startDateTime` and
`endDateTime` query parameters and sends `Prefer: odata.maxpagesize=100` plus the
immutable-ID preference. Continuations are the complete opaque URL returned by
Graph. A continuation is admitted only when it is HTTPS, has no user information,
explicit port, or fragment, names exactly `graph.microsoft.com`, has the exact
case-insensitive `/v1.0/me/calendarView/delta` path, and carries exactly one
non-empty `$skiptoken` or `$deltatoken` appropriate to the call. Application code
must not reconstruct, decode, log, or return the token.

One delta round is bounded before persistence by all of the following:

- at most 100 returned entries per requested Graph page;
- at most 64 Graph pages;
- at most 3,200 event or tombstone entries;
- at most 2 MiB of response bytes per page and 16 MiB in the complete round; and
- at most 32 KiB in any accepted continuation URL; and
- at most 300 seconds for the complete round, with each HTTP call capped by the
  smaller of the existing per-request timeout and the remaining round time and
  the absolute deadline rechecked while every response body is streamed.

Crossing any bound, receiving redirects, receiving both or neither continuation
fields, receiving malformed event data, or receiving an unexpected Graph status
fails the sync without changing the last completed projection or cursor. HTTP 401
revokes the read grant. HTTP 429 and 5xx remain retryable failures. A stale delta
token (`410 Gone`, `syncStateNotFound`, or `ErrorSyncStateNotFound`) triggers at
most one full initial round for the same window; the old completed projection
remains readable until that replacement commits successfully.

The adapter buffers one bounded round and commits it in one SQLite transaction.
For an initial or stale-token recovery round, the transaction replaces that
window's projection. For an incremental round, it applies response entries in
order, with an event upserting its bounded projection and `@removed` deleting the
same event ID, then advances to the returned `@odata.deltaLink`. Replayed entries
are idempotent. A crash, parser failure, or injected database failure before commit
preserves the prior events and cursor together; a completed commit exposes both
together.

The projection stores no raw Graph document, body, attendee list, organizer, or
token. Each row contains only the immutable event ID, bounded subject, bounded
start/end date-time and zone strings, all-day flag, and bounded location display
name. The UTF-8 byte ceilings are 512 each for event ID, subject, and location,
64 for each date-time, and 128 for each zone. `calendar.read.events` has a 16-MiB
encoded-response ceiling. The engine emits UTF-8 JSON without ASCII escaping;
its field and entry bounds keep every valid projection representable under that
ceiling even when every string character requires JSON escaping. It fails rather
than silently dropping events. Read disconnect removes these copied rows and
their cursor in the same database transaction that resets the grant.

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

Version 1 treats every extracted attendee as required. Every
`findMeetingTimes` request sets `minimumAttendeePercentage` explicitly to `100`;
it never relies on Graph's lower default. A suggestion may advance to
confirmation only when the bounded response proves that every required attendee
is available for the complete proposed interval. Missing, malformed, unknown,
partial, or conflicting attendee availability cannot become a confirmation and
instead follows the same durable `manual_review` path as no suggestions.
Graph's ordered response collection determines suggestion preference. The
redundant per-suggestion `order` field is not required because the live v1.0
service may omit it.

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
run. A retry or reconciliation request that is rejected, including an
authentication, authorization, transport, or request error, does not prove that
the earlier ambiguous POST failed and leaves the original run unresolved. A
duplicate-suppression response or retry that identifies the event already
created with the stable transaction ID completes the original run rather than
failing it. Only a definitive response to the original first submission that
proves no event was accepted may fail the run directly; absence from a later
search is likewise not proof of failure.

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

For a local date-time, the application resolves the canonical IANA zone through
the installed time-zone database and requires the supplied UTC offset to equal
that zone's offset at that exact local date-time. A mismatched pair—for example,
`-05:00` with `Europe/Berlin` when Berlin is not at that offset—is rejected
before proposal or confirmation; neither value silently overrides the other.
The resulting resolved instant is authoritative for the proposal hash,
confirmation, ledger, availability request, and write. Adapter conversion to a
server-supported zone must preserve that instant and the displayed local time.

## Entitlement and visibility boundary

The calendar operations in this contract are internal Email Watcher
capabilities. Calendar setup, re-consent, and calendar availability appear only
when the existing signed entitlement contains `connect.capability_exchange`.
The email-driven automation UI appears, and an automation may start or resume,
only when that same entitlement contains both `connect.capability_exchange` and
`connect.automations`. An automation-only entitlement cannot expose calendar
operations, and a capability-exchange-only entitlement cannot expose or run the
automation. Every operation also requires its corresponding Microsoft grant.
Both entitlement features and consent are rechecked immediately before
admission.

Entitlement gates new capability use, not revocation of credentials already
granted. If an entitlement is missing, expired, or revoked, each existing
calendar profile's non-secret status and **Disconnect** control remain visible
and callable. Disconnect remains an idempotent local operation that takes the
mailbox operation lock, removes only that profile's private cache, and resets its
state to `not_requested`; it performs no Graph call and cannot restart consent.
The UI and result never expose cache paths, tokens, or calendar data. All setup,
re-consent, read, proposal, write, and automation actions remain unavailable
without their required entitlement.

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

A deterministically rejected extraction receives exactly one bounded retry with
typed validation violations and the same admitted source. The run durably
records the attempt number, schema version, validation codes, and bounded result
without storing raw message content. If the second attempt is also rejected, the
run atomically transitions to `manual_review` with a structured
`validation_rejected` reason, emits the ordinary human-review notification, and
creates no proposal or Graph request. Restart reads the persisted attempt count;
it cannot reset the counter or loop indefinitely.

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
detected -> extracting -> ambiguous | manual_review
                      \-> proposing -> awaiting_confirmation
                                   \-> manual_review
awaiting_confirmation -> proposing  # revise or expire and re-propose
awaiting_confirmation -> declined
awaiting_confirmation -> write_authorized -> writing -> completed
                                               \------> failed | unresolved
detected | extracting | proposing | awaiting_confirmation -> source_unavailable
write_authorized --source removed before submission begins--> source_unavailable
unresolved -> reconciling -> completed | unresolved
```

`detected` and `extracting` are recoverable work states. Each extraction attempt
fetches the source message through the mailbox adapter and holds the raw content
only in process memory. A restart retries either state. If the provider proves
the source message is no longer retrievable, the run records a structured
`source_unavailable` terminal outcome and emits a human-review notification; it
must not remain indefinitely in `extracting`. Raw content is not added to the
message or automation tables to provide this recovery.

Each event uses an immutable non-content envelope. The current run projection
stores the corresponding non-content fields plus references to separately
deletable payload rows. An envelope records opaque run/event identifiers,
ordering and state versions, automation and schema versions, transition kind,
non-reversible content hashes, calendar account's immutable identity, decision
and transition timestamps, stable Graph `transactionId`, Graph event identity
when known, and structured failure or unresolved codes. Subject, attendees,
location, evidence, proposal content, display labels, and other copied message
data live only in the payload rows. Appending an envelope, inserting its payload,
and updating the run projection remain one transaction; deleting a payload later
neither rewrites nor removes its envelope.

Copied automation payload follows the source message's configured retention and
local privacy operations. When watcher retention, `inbox.delete`, or
`inbox.clear` removes the source, the same transaction removes copied subjects,
attendee addresses, locations, extracted evidence, and proposal content from the
automation payload rows. A run in `detected`, `extracting`, `proposing`, or
`awaiting_confirmation` becomes `source_unavailable`, emits the human-review
notification, and can never write.

The write worker and source cleanup take the same operation lock. If cleanup
wins while a run is `write_authorized` and no first POST has begun, cleanup
transitions it to `source_unavailable` and permanently cancels the authorization.
The worker may enter `writing` only while holding that lock, after rechecking the
source and durable confirmed proposal, and it holds the lock until the bounded
POST returns and the outcome commits. Therefore ordinary cleanup cannot remove
the request between authorization and first submission. If a crash makes an
in-flight write ambiguous, recovery moves it to `unresolved`; it never assumes
the POST was unsent.

A run in `writing` or `unresolved` is not relabeled failed during cleanup: it
keeps its truthful state and is compacted to a reconciliation tombstone
containing only the automation identifier/version, prior state, stable
transaction ID, target account's immutable principal ID, bounded start/end
reconciliation window, Graph event ID when known, non-reversible
trigger/proposal hashes, and timestamps. No new write retry is permitted after
the separately stored request payload has been removed; only bounded
reconciliation may continue. Terminal runs are compacted to the same non-content
form.

The tombstone contains no subject, attendee address, location, evidence,
message body, raw provider message ID, token, or cache path. It expires no later
than the existing maximum supported retention window measured from source
observation. Once expired, it is purged and the source is permanently ineligible
for replay or write retry. Compaction, state transition, event append, and source
deletion commit atomically so privacy cleanup cannot erase the idempotency record
while leaving a write eligible.

No state may claim `awaiting_confirmation` before the proposal is durable. No
state may claim `write_authorized` unless the confirmation event identifies the
exact durable proposal version and hash. No state may claim `completed` before
the created event identity and write outcome commit durably.

### Confirmation boundary

Email Watcher renders the proposal using its own native UI and shows the exact
subject, attendees, start, end, IANA zone, location, Teams-link choice, and target
Microsoft calendar principal. The target display includes the organizer/account
label and address plus stable tenant/account identity sufficient to distinguish
configured accounts; it never exposes tokens or cache paths. The proposal
version and hash bind the target immutable principal and target calendar ID, so
changing either invalidates prior confirmation. The confirmation states that
this principal will own the event. When attendees are present, it explicitly
states that confirmation will send meeting invitations from that organizer to
those addresses. The user may confirm that exact version, revise it into a new
proposal version, or decline it.

Confirmation is single-run, single-proposal, and non-transferable. Changing any
effect-bearing field invalidates prior confirmation. Each proposal durably
records its availability-observation time and expires at the earlier of fifteen
minutes after that observation or the proposed start. Confirmation admission
must compare the current time with that expiry and require a future start. An
expired proposal returns to `proposing`, refreshes availability, and requires a
new proposal version and confirmation; it cannot authorize a write. Repeated
clicks and stale UI versions cannot create another write. Version 1 has no
automatic write mode.

## Acceptance evidence

The current acceptance record consists of the following live and deterministic
evidence. The live evidence is summarized above, deterministic boundaries
remain regression-tested, and any branch not yet exercised live is marked
pending rather than inferred from fixtures:

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
6. An entitlement-loss fixture proves existing profile status and disconnect
   remain available after entitlement becomes missing, expired, or revoked;
   disconnect removes the selected cache without a Graph call, while setup,
   re-consent, calendar operations, and automation stay unavailable.
7. A live work-or-school Microsoft 365 test account proves calendar-read consent
   and a complete `/me/calendarView/delta` round with persisted continuation.
8. An initial-plus-subsequent delta fixture proves unchanged events remain,
   tombstones remove deleted events, and cursor/projection changes roll back
   together at an injected crash point.
   Boundary fixtures also prove an exact-366-day window passes, one microsecond
   over fails, a 64-page round passes, page 65 fails without persistence, a
   2-MiB page passes, one byte over fails before JSON parsing, and a hostile or
   wrong-path continuation cannot reach the HTTP client. A stale-cursor fixture
   proves exactly one initial replacement is attempted while the prior completed
   projection stays readable if replacement fails. A simulated clock proves the
   round deadline stops pagination even when the page-count limit has room.
9. The live account proves proposal consent and a real `findMeetingTimes` domain
   result; a no-suggestions fixture records the reason, reaches `manual_review`,
   and never creates an empty confirmation.
10. Required-attendee boundary fixtures prove the request explicitly sends
    `minimumAttendeePercentage: 100`, a fully available set may advance, and a
    response with one conflicting, partial, missing, unknown, or malformed
    attendee cannot reach confirmation.
11. A watched-sender scheduling fixture reaches a durable proposal and native
   confirmation UI without writing an event.
12. Atomic-admission and post-admission crash probes prove analysis completion
    cannot lose or duplicate the one run keyed to the source message and
    automation version, and restart can resume `detected` or `extracting`.
13. A missing-source fixture proves a provider-confirmed deleted or moved source
    reaches `source_unavailable`, emits human-review notice, stores no raw body,
    and does not remain stuck in an active extraction state.
14. Extraction boundary fixtures prove one deterministic rejection receives
    exactly one feedback-bearing retry, a second rejection durably reaches
    `manual_review` with `validation_rejected`, and restart cannot obtain a third
    attempt, proposal, or Graph call.
15. Ambiguous, reschedule, and cancellation negative controls record their
    non-writing outcomes, emit the review notification, and produce no proposal
    and no Graph write request.
16. A matching-offset fixture and a `-05:00`/`Europe/Berlin` mismatch fixture
    prove time-zone resolution accepts only a consistent offset/zone pair,
    rejects ambiguous and nonexistent local times, and preserves the one
    resolved instant through proposal, confirmation, ledger, and adapter.
17. A multi-account fixture proves confirmation identifies the target principal
    and organizer, binds its immutable identity and calendar to the proposal
    hash, and invalidates confirmation when either target changes.
18. **Live no-attendee and attendee-bearing writes accepted.** Separate live,
    explicitly confirmed proposals created one event each through the isolated
    write grant and retained their event identity and provenance. Before the
    attendee-bearing write, the native accessibility tree exposed the invitation
    effect, organizer, and sole attendee. Graph read-back matched the durable
    identities, requested interval, and expected attendee; a later reconciliation
    found exactly one matching transaction. Both controlled events were removed.
19. Lost-response and repeated-confirmation probes reuse one transaction ID,
    cannot produce duplicate event work, and prove reconciliation can move an
    unresolved run to `completed` only when it finds or receives the matching
    event. A later retry or reconciliation rejection leaves the original run
    unresolved, while a definitive rejection of the first submission may fail
    it.
20. An expired-proposal fixture proves a delayed confirmation performs no write,
    refreshes availability into a new proposal version, and requires new
    confirmation; a proposal whose start has passed cannot be confirmed.
21. Separate disconnect fixtures remove each calendar cache, disable its
    dependent capability, reset its state to `not_requested`, and leave unrelated
    grants intact; mailbox disconnect disables email-driven automation without
    silently deleting calendar grants.
22. An entitlement matrix proves that capability exchange alone exposes
    calendar setup and, when the corresponding read grant is ready, calendar
    availability; automations alone exposes neither calendar nor automation;
    and both features expose automation subject to the corresponding Microsoft
    grants. Missing, pending, or revoked consent leaves mailbox monitoring
    healthy and truthfully reports calendar unavailability.
23. Retention and local-delete fixtures prove all copied automation content is
    removed atomically with its source, immutable event envelopes retain no
    copied content, and every pre-submit state has an explicit non-writing
    `source_unavailable` transition. A race fixture proves cleanup before the
    first POST cancels a `write_authorized` run, while submission holding the
    operation lock commits an outcome before cleanup proceeds. An ambiguous
    write retains only the bounded non-content reconciliation tombstone without
    being relabeled failed. Boundary fixtures prove expiry purges the tombstone
    and blocks replay.

Live evidence must identify the tested application revision and sanitized
account/tenant class. A mocked Graph response cannot substitute for the required
live consent, read, proposal, and confirmed-write proof.

## Landing order

Implementation remains split into reviewed vertical slices:

1. **Implemented:** feature-aware entitlement lookup using the existing signed
   file and keyring;
2. **Implemented:** separate Microsoft calendar grants and read adapter;
3. **Implemented:** scheduling extraction and immutable automation ledger;
4. **Implemented:** desktop controls expose the three isolated Microsoft calendar
   consent profiles, native proposal preview and confirmation, and the private
   idempotent confirmed-write adapter; and
5. **Implemented:** live consent, read, proposal, confirmed-write, and negative-
   control acceptance evidence recorded above.

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

---

# Durable Local Connect provider-admission queue contract

**Status:** issue #117 durable storage, engine pump, and Tauri host/UI behavior
implemented; exact-current Invoice Processor operational proof remains.

## Verified baseline

This contract was derived from current merged Email Watcher code and from the
current canonical/provider contracts, not from the issue description alone.

- Local Connect deliberately supplies no broker, provider-side queue, scheduler,
  or automatic retry service. A provider may accept one job and answer another
  submission with `PROVIDER_BUSY` and `retryable: true`
  (`connect-contracts/adr/0001-connect-v0.md`, lines 82-98).
- Connect v2 makes the caller-created `job_id` the idempotency identity and makes
  `instance_id` the provider's durable job-state namespace across process
  restarts (`connect-contracts/adr/0002-connect-v2-generic-capabilities.md`,
  lines 79-96).
- Invoice Processor correctly refuses a second concurrent job rather than
  queueing it (`docs/contracts/SLICE-5.md`, S5-JOB-2 and D8). Its
  `PROVIDER_BUSY` response is an authoritative pre-admission refusal.
- Email Watcher persists the request, input identity, selected provider,
  capability, status, output, and error in `connect_attachment_jobs`, but its
  durable status set has no distinction between waiting, dispatching, and
  reconciling (`src/eom_email_watcher/db.py`,
  `_CONNECT_JOBS_TABLE_SQL`).
- `connect.attachment.invoke` calls the provider synchronously. A retryable
  `ConnectError` is re-raised while the job remains active; no component later
  selects and resubmits that job (`src/eom_email_watcher/engine_api.py`,
  `_run_generic_connect_job`).
- The desktop suppresses only another click for the exact in-memory invocation
  key. Another attachment or capability can therefore submit concurrently to
  the same provider instance (`desktop/src/main.ts`, attachment invocation
  handler).

The claim in issue #117 is therefore confirmed. The provider is honoring the
shared contract; the missing behavior is consumer-owned durable admission and
retry policy.

## Root cause and correct-fix boundary

The root cause is not the provider's one-job capacity and not one missing UI
error mapping. Email Watcher has a durable job ledger but no durable dispatch
state, no provider-instance serialization boundary, and no host-owned queue
pump. A correct fix must add all three while preserving the stable job identity
and existing provider reconciliation rules.

This change must not add a provider-side queue, alter Local Connect schemas,
select a different provider silently, store attachment bytes, or turn Connect
into a workflow engine.

## Queue ownership and identity

This contract applies to Connect v2 jobs. The legacy v1 compatibility path is
unchanged.

One queue lane is identified by:

```text
(protocol_version=2, provider_app_id, provider_instance_id)
```

All capabilities exposed by that instance share the lane because capacity is a
provider-process/resource property, not a capability-label property. Two
different durable provider instances may run concurrently. Two capabilities on
one instance may not.

An active logical invocation is identified by the existing v2 invocation
fingerprint together with its message and attachment. That fingerprint binds
the selected provider/version/instance, capability/version, trusted artifact
identity, and canonical parameters; it excludes the caller-generated `job_id`.
Enqueue performs active-fingerprint lookup and insertion in one immediate
transaction backed by a partial unique index over nonterminal v2 jobs. It does
this before applying the lane cap. If two processes supply different UUIDs for
the same active logical invocation, exactly one row wins and both callers
receive that row's original stable `job_id`. A terminal failure permits a new
explicit invocation; process-local click suppression is never the deduplication
boundary.

If a legacy database already contains multiple active rows for one fingerprint,
migration preserves every identity as `reconciling` rather than discarding work
that may have reached the provider. The immediate enqueue transaction prevents
new duplicates while the partial unique index is deferred; initialization adds
the index once reconciliation leaves no duplicate active group.

The selected application version, capability identifier/version, parameters,
artifact identity, and request JSON remain bound to the existing job. A queued
job never migrates to a new provider instance or newly discovered application
version. If that exact durable instance cannot return, the user may make a new
explicit invocation against another provider; Email Watcher must not rewrite
the old provenance.

## Durable dispatch state

Provider transport status remains:

```text
requested -> accepted -> processing -> completed | failed
```

Email Watcher adds a separate durable dispatch state for every active v2 job:

```text
waiting       eligible for a future admission attempt
dispatching   owns the provider lane and is making/reconciling one attempt
reconciling   submission outcome is ambiguous; GET must precede any POST
provider_owned provider returned accepted or processing
terminal      completed or failed
```

The dispatch record stores only bounded metadata:

- `job_id`;
- dispatch state and monotonically increasing attempt count;
- `next_attempt_at`, consecutive reconciliation-failure count, and
  queue-admission deadline;
- whether provider submission may have occurred and whether the source is
  still available;
- the highest authoritative provider state reached, so a later contradictory
  response cannot erase prior acceptance;
- the last bounded retryable code/message;
- timestamps.

It stores no attachment bytes, message body, mailbox credential, Connect bearer
token, or provider-private path. Queue position is computed from durable order;
it is not mutable stored truth.

Migration is fail-closed. Existing `requested` v2 jobs become `reconciling`,
never `waiting`, because current data cannot prove whether a POST reached the
provider. Existing `accepted`/`processing` jobs become `provider_owned`; terminal
jobs become `terminal`. V1 rows receive no queue behavior.

The Connect job row and its dispatch row are one state machine, not two
independently committed ledgers. Enqueue creates both in one SQLite
transaction. Every provider update atomically writes transport status/result
and the compatible dispatch transition in one SQLite transaction:

```text
requested                     waiting | dispatching | reconciling
accepted | processing         provider_owned
completed | failed            terminal
```

No store API exposes a partial transition. Startup treats an impossible pair
as recovery-required and repairs it from the provider before releasing the
lane; it never advances the queue from one side of a split write. Migration
creates compatible pairs in the same transaction.

If a new process acquires a lane whose durable head is still `dispatching`, the
prior process can no longer own the native lock. Recovery changes that head to
`reconciling` before network access; it never assumes the interrupted attempt
stopped before POST.

## Ordering, bounds, and cross-process exclusion

- A provider lane admits at most **25 nonterminal v2 jobs**, including the job
  holding the lane. The twenty-sixth distinct invocation is rejected before a
  job row is created with a bounded `connect_queue_full` error. Repeating an
  already admitted `job_id` is not a new queue entry.
- Waiting jobs are ordered by `created_at`, then `job_id`. An existing
  `reconciling` or `provider_owned` job always resumes before a new waiting job.
- Each waiting job has a **two-hour queue-admission deadline** fixed when its
  durable row is created. Retrying or restarting does not extend it.
- `PROVIDER_BUSY` uses deterministic exponential backoff of **2, 4, 8, 16, then
  30 seconds maximum**. The fixed deadline, not attempt count, is the terminal
  bound.
- A native per-lane process lock, derived from a SHA-256 digest of the lane
  identity and stored beside the private database, is held across selection,
  reconciliation, submission, polling, and durable outcome recording. After
  acquiring it, a process must re-read the queue and operate only on the
  authoritative head. Process exit releases the lock; no stale lock-file
  deletion is used as ownership evidence.
- If the platform can provide only a soft/advisory fallback rather than the
  existing supported native lock, enqueue fails before creating a job with
  `connect_queue_unavailable`. A legacy or recovered waiting row that cannot
  obtain supported exclusion still expires at its original admission deadline.

The lock and the transactional head selection together are the execution model:
for every admitted interleaving, at most one Email Watcher process can issue or
reconcile work for one provider lane, and a later job cannot overtake an earlier
eligible job after acquiring the lock.

Deadline maintenance is distinct from dispatch selection. In one immediate
transaction, it conditionally fails every row whose dispatch state is still
`waiting`, whose submission-possible flag is false, and whose admission
deadline is due, including non-head rows behind provider-owned work. It never
changes `dispatching`, `reconciling`, or `provider_owned` rows. The conditional
state predicate makes the sweep safe if a head claim races it, and expired
tails cannot consume lane capacity indefinitely.

## Refusal, ambiguity, and retry rules

### Authoritative pre-admission refusal

`PROVIDER_BUSY` with `retryable: true` proves the provider did not accept the
job. Email Watcher records the exact bounded code/message, returns the job to
`waiting`, schedules the next attempt with the same `job_id`, and keeps the UI
in a waiting state.

If the two-hour deadline passes for **any** job that has never possibly reached
the provider, the job becomes `failed` with
`connect_queue_deadline_exceeded`. This includes no attempt, provider absence,
lock unavailability after enqueue, and authoritative busy refusals. The job
retains its last bounded diagnostic/refusal message for display. Deadline
expiry is evaluated before every waiting attempt and by the host's due-time
wakeup, so an absent provider cannot leave the lane head waiting forever.

### Ambiguous outcome

A timeout, connection loss, malformed response, or other failure after a POST
may have reached the provider. It is not proof of refusal. The job becomes
`reconciling`, keeps the same identity, and continues to hold the provider lane.
Every later attempt must:

1. rediscover and authenticate the same durable provider instance;
2. issue `GET /v2/jobs/{job_id}` first;
3. persist any authoritative provider state; and
4. treat `JOB_NOT_FOUND` as proof of non-retention only when that job has never
   returned authoritative `accepted` or `processing`; and
5. before any same-identity resubmission, recheck the original admission
   deadline, current Connect entitlement, source retention/availability, and
   trusted artifact identity, then use the same request and `job_id`.

If `JOB_NOT_FOUND` arrives after the admission deadline for a job that was
never authoritatively accepted, the now-proven-unsubmitted job fails
`connect_queue_deadline_exceeded` without a POST. If it arrives after an
authoritative acceptance, it is contradictory evidence: the job remains
`reconciling`, retains the lane, and exposes the bounded diagnostic for operator
visibility. A GET error, malformed response, identity mismatch, or nonterminal
contract violation after possible submission likewise cannot prove that
provider work stopped and cannot release the lane. Only a valid terminal
provider job status can make possibly submitted work terminal.

An ambiguous job is never terminally failed merely because its queue-admission
deadline elapsed. It remains visibly `reconciling` until the provider returns
authoritative state. This preserves the existing no-duplicate-work contract and
prevents later jobs from bypassing work that may already own provider capacity.

Every unsuccessful reconciliation `GET` records `next_attempt_at` using the
same deterministic **2, 4, 8, 16, then 30 second maximum** schedule. Provider
absence uses 30 seconds. An authoritative `accepted` or `processing` response
resets the failure count and schedules the next status poll after 2 seconds.
These due times survive restart and are host wakeups; neither reconciliation
nor provider-owned polling may spin immediately or wait for an unrelated
mailbox event.

### Provider absence and other errors

- If the exact provider instance is absent before any POST attempt, the job
  remains `waiting` and is rediscovered after a fixed 30-second backoff within
  the same admission deadline.
- If that instance disappears after a possible or confirmed submission, the job
  is `reconciling`, not newly dispatched elsewhere.
- A valid nonretryable refusal to a POST is terminal only when it
  authoritatively proves pre-admission refusal. A valid terminal `failed` job
  status is terminal. Errors returned by later GETs remain `reconciling`
  because error retryability does not prove that accepted work stopped.
- A retryable error not explicitly proven to be pre-admission follows the
  ambiguous-outcome path. The word `retryable` alone never authorizes a second
  POST.
- `accepted` and `processing` retain the provider lane and use the existing
  status polling/restart reconciliation. Their processing time does not consume
  or extend the queue-admission deadline.

Every POST—initial or same-identity resubmission—revalidates the current signed
Connect entitlement immediately before handoff. GET-only reconciliation
continues after entitlement loss because it can only learn the outcome of data
already handed off. If the provider is proven not to have accepted the job and
entitlement is no longer active, the job fails terminally with the existing
`CONNECT_ENTITLEMENT_REQUIRED` error and no POST.

## Artifact and retention behavior

Email Watcher does not persist attachment content for the queue. It re-fetches
the selected attachment once during enqueue, computes its SHA-256, and
persists only the trusted size/hash identity before returning the queued job.
After the job reaches the queue head and owns the provider lane, it re-fetches
the bytes and verifies both values against that enqueue-time identity before
handoff. A digest first computed at dispatch is not identity verification.

Enqueue and final handoff both compare the source message's received time with
the current configured retention boundary. A source outside that boundary is
definitively unavailable even if purge has not yet removed its local row or the
mailbox still returns bytes; it fails without a POST. A missing source or an
enqueue-time/final size or digest mismatch is likewise definitive. By contrast,
a timeout, temporary mailbox outage, or refreshable authorization failure
before POST returns the job to `waiting` with the deterministic
2/4/8/16/30-second backoff under its original admission deadline. Transient
source fetch failure is neither provider ambiguity nor proof that the source is
absent.

Enqueue, handoff, and source deletion share a native per-message source lock
stored beside the private database. Enqueue holds it across the source
existence check, first fetch/hash, and atomic job/dispatch insert. A waiting
handoff holds the lane lock and then the source lock across its final database
source check, re-fetch/hash verification, POST, and atomic recording of the
POST outcome. Manual deletion and retention acquire the source lock before
their deletion transaction. That order is fixed—lane then source for a pump;
source only for cleanup—so it cannot form a lock cycle.

If the source message or attachment is unavailable before any possible
provider submission, the job fails with `connect_source_unavailable` without a
POST. If cleanup wins the source lock, no later waiting dispatch can submit. If
handoff wins, cleanup waits until the submission outcome and dispatch state are
durable; it cannot delete the ledger beneath already-loaded bytes.

Cleanup atomically deletes unsubmitted waiting and terminal Connect rows with
the message. It does **not** delete a `dispatching`, `reconciling`, or
`provider_owned` identity, because the provider may already own that work.
Instead it deletes the message and attachment metadata, marks the bounded
non-content reconciliation record `source_available = false`, and retains the
job/lane identity until an authoritative terminal provider outcome. Such a
tombstone retains only the request/provenance hashes and bounded dispatch
metadata already listed above—never attachment bytes or the email body. If a
later authoritative `JOB_NOT_FOUND` would normally permit resubmission, source
unavailability makes it terminal `connect_source_unavailable` and no POST is
sent. A late terminal response for a source-unavailable tombstone is validated,
then the job and dispatch tombstone are deleted atomically to release the lane;
its content-bearing `result_json` and output payload are never inserted. A
queued job never keeps the source message, attachment content, or late result
content beyond configured retention.

## Host and UI contract

`connect.attachment.invoke` becomes enqueue-or-resume behavior. It persists or
reuses the stable job first and may return a nonterminal queue result without
waiting for another provider job to finish.

The Tauri host owns queue pumping:

- immediately after enqueue;
- immediately after a lane job becomes terminal;
- when a recorded backoff becomes due;
- at desktop startup/restart; and
- opportunistically after an ordinary watcher check.

Each pump response carries the next durable wake time in Unix milliseconds, or
`null` when no active queue remains. The host coalesces explicit wake signals,
waits until that engine-supplied time, and asks the engine to re-read durable
state; it does not reproduce queue ordering or retry policy in Rust or
JavaScript. Queue progress emits a host event that causes the native Inbox to
reload its durable result rows.

If a pump cannot acquire a lane lock, the host schedules a coalesced retry for
that lane after 2 seconds even though it does not mutate the owning process's
dispatch row. Lock release is not treated as a notification. This bounded
contention wakeup guarantees that another live process revisits a head left
`dispatching` when the owner dies; startup remains the recovery wakeup if that
host also exits.

Only one pump may own a lane because the engine enforces the native lane lock;
frontend timers are wakeups, not correctness locks. Closing Connect or stopping
a provider never stops mailbox monitoring.

Native Email Watcher UI renders durable state:

```text
Waiting for <provider>, <N> ahead
Reconnecting to <provider>
Running <action>
<last provider refusal> (after the admission deadline)
```

Provider names and messages remain untrusted text rendered only with
Email-Watcher-owned text components. Repeated clicks reuse the active durable
job. The in-memory click set may improve responsiveness but is not an
idempotency or concurrency boundary.

## Acceptance evidence

Implementation is not complete until current merged code demonstrates:

1. two invocations against one provider instance produce one lane owner and one
   durable waiting job, with the second job showing one ahead;
2. a real or contract-faithful `PROVIDER_BUSY` refusal is resubmitted after the
   bounded backoff with the original `job_id` and completes without another
   click;
3. consumer restart while the second job waits preserves order, deadline, last
   refusal, and eventual completion;
4. process death while dispatching releases the native lane lock, after which
   recovery queries the same `job_id` before any resubmission;
5. a lost POST acknowledgement cannot produce duplicate provider work;
6. every never-submitted waiting path—including no provider, unavailable
   native exclusion, and repeated authoritative busy refusals—crosses the
   two-hour deadline and fails with `connect_queue_deadline_exceeded` plus the
   last diagnostic;
7. nonretryable refusal remains an immediate terminal failure;
8. the exact 25/26 lane-cap boundary is enforced, while replay of an admitted
   identity does not consume another slot;
9. two engine processes racing one lane cannot issue concurrent provider work,
   while distinct provider instances may progress independently;
10. enqueue records a trusted digest without retaining bytes, dispatch rejects
    changed bytes, and source removal racing the final pre-POST boundary either
    wins with no POST or waits for a durable submission outcome;
11. cleanup of `reconciling` or `provider_owned` work preserves a non-content
    tombstone and lane ownership until authoritative terminal reconciliation;
12. repeated reconciliation failures follow durable bounded backoff and recover
    automatically without a hot loop or unrelated mailbox event;
13. transport and dispatch transitions remain compatible after injected crashes
    at every persistence boundary;
14. concurrent requests with different UUIDs but one active invocation
    fingerprint return one durable job and execute provider work once;
15. expired waiting tails fail and stop consuming capacity while a submitted
    head remains nonterminal;
16. `JOB_NOT_FOUND` after deadline produces no POST, while `JOB_NOT_FOUND` or
    other GET errors after authoritative acceptance retain the lane;
17. entitlement revocation between enqueue and handoff blocks every proven-new
    POST without blocking GET-only reconciliation;
18. transient source-fetch failures retry under the original deadline, while a
    definitive missing, changed, or retention-expired source produces no POST;
19. a terminal response arriving after source cleanup releases the lane and
    deletes its tombstone atomically without persisting result content;
20. a live lock contender schedules a bounded retry and recovers a
    `dispatching` head after the lock owner exits;
21. the desktop displays waiting, reconciling, running, completed, and failed
    states from durable engine data rather than inferred frontend state; and
22. an end-to-end two-invoice proof against the real Invoice Processor shows one
    active job, one automatically retried waiting job, two terminal results, and
    no provider-side queue or operator retry.

Boundary tests must cover zero/one/25/26 entries, equal timestamps, a request
already present at the cap, just-before/at/after deadline, every backoff edge,
provider disappearance before and after possible submission, lock contention,
process death, repeated reconciliation `GET` failures, changed attachment bytes,
source deletion before/during/after handoff, retention during provider-owned
work, split-write crash injection, different UUIDs for one active fingerprint,
expired non-head waiters, accepted-then-not-found responses, entitlement
revocation before resubmission, transient source fetches, startup after the
retention boundary, late terminal result content, contender wakeup after owner
death, and malformed or nonretryable provider errors.

## Landing order

1. **This commit:** contract only; no runtime behavior changes.
2. SQLite migration, dispatch metadata, bounded queue admission, deterministic
   ordering, and provider-lane lock/head claim.
3. Engine enqueue/drain/reconciliation behavior and focused cross-process tests.
4. Tauri wakeups and native durable queue presentation.
5. Exact-current installed-app proof with the Invoice Processor, followed by
   sanitized evidence in this contract.

Each implementation slice must remain independently reviewable and preserve
ordinary Gmail, Microsoft 365, IMAP, Inbox, notification, retention, calendar,
and EOM monthly-reminder behavior.

## Explicit non-scope

- provider-side queueing or an Invoice Processor change;
- Local Connect schema, registration, manifest, or error-taxonomy changes;
- Connect v1 redesign;
- automatic provider selection or migration to another provider instance;
- storing raw email bodies or attachment bytes;
- workflow/automation rules, batch policy, priorities between applications, or
  a general scheduler;
- cross-machine Connect, cloud transport, or inference-gateway work;
- cancellation, manual ambiguous-job resolution, or retry-history UI; and
- unrelated mailbox, model, calendar, release, or EOM business changes.

## Rejected alternatives

- **Queue inside Invoice Processor:** rejected because the provider contract
  deliberately refuses excess work and assigns retry policy to the caller.
- **Treat `PROVIDER_BUSY` as a red error:** rejected because it discards the
  provider's explicit retryable pre-admission semantics and requires another
  click.
- **Serialize only in TypeScript:** rejected because another window, restart,
  CLI process, or concurrent sidecar request bypasses frontend memory.
- **Use one global Connect lock:** rejected because independent provider
  instances have independent capacity and should not block one another.
- **Delete stale lock files:** rejected because path existence is not lock
  ownership; native process locks release on process death.
- **Resubmit every retryable error:** rejected because transport failure can hide
  a successful acceptance. Only authoritative not-found evidence permits a
  second POST after ambiguity.
- **Persist queued attachment bytes:** rejected because the mailbox remains the
  content owner and current retention/privacy contracts intentionally avoid a
  second raw-content store.
