# Calendar capability and email automation contract

Status: **partially implemented: landing item 1 of 5 implemented; items 2–5
pending**

This document freezes the boundary for adding Microsoft 365 calendar operations
and the first email-driven automation to Email Watcher. It is an implementation
gate, not a claim that calendar or automation behavior exists today.

Feature-aware entitlement lookup is implemented. The existing mailbox watcher,
Local Connect consumer, and monthly Gmail sender remain authoritative while the
calendar grants, adapters, automation, UI, and live acceptance evidence remain
pending.

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
Version 1 returns the complete bounded projection for that identity rather than
accepting pagination state, a caller-supplied Graph cursor, or a Graph URL.
`calendar.read.events` performs no Graph request and refuses an incomplete,
missing, differently windowed, differently principaled, or unavailable grant.

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
- at most 6,400 event or tombstone entries;
- at most 2 MiB of response bytes per page and 16 MiB in the complete round; and
- at most 32 KiB in any accepted continuation URL.

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
name. `calendar.read.events` has an 8-MiB encoded-response ceiling; a projection
that cannot be returned under that ceiling fails closed rather than silently
dropping events. Read disconnect removes these copied rows and their cursor in the
same database transaction that resets the grant.

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

## Acceptance evidence for later implementation

Calendar and automation implementation is not complete until all of the
following pass against current merged code. Landing item 1 has its own merged
evidence; items 2–5 remain pending:

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
   projection stays readable if replacement fails.
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
18. A live, explicitly confirmed new-meeting proposal creates one event through
    the separate write grant, visibly warns that attendee invitations will be
    sent, and records its event identity and provenance.
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
2. **Pending:** separate Microsoft calendar grants and read adapter;
3. **Pending:** scheduling extraction and immutable automation ledger;
4. **Pending:** native proposal/confirmation UI and private confirmed-write
   adapter; and
5. **Pending:** live acceptance evidence.

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
