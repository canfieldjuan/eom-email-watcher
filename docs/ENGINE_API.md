# Engine API v1

`eom-mail-engine` is the machine-facing boundary for desktop hosts. It is a one-shot process:
write one JSON request to stdin, read one JSON response from stdout, then inspect the exit status.
Human diagnostics go to stderr.

Source development may invoke the command from the installed Python environment. The Linux desktop
package includes the same engine protocol as a Tauri sidecar.

## Envelope

Every request includes the protocol version, operation, engine config path, and an operation
payload:

```json
{
  "protocol": 1,
  "operation": "inbox.recent",
  "config_path": "/platform/app/config/config.toml",
  "payload": {"limit": 20}
}
```

Success and error responses are deterministic JSON objects:

```json
{"data":{"items":[]},"ok":true,"operation":"inbox.recent","protocol":1}
```

```json
{
  "error":{"code":"invalid_request","message":"limit must be an integer between 1 and 500"},
  "ok":false,
  "operation":"inbox.recent",
  "protocol":1
}
```

Exit status is `0` for success and `2` for a handled error. Requests are limited to 1 MB. Unknown
top-level and payload fields are rejected. Responses never contain OAuth tokens, model token
contents, token paths, the ntfy topic, or raw email bodies. Detailed Gmail diagnostics are written
only to stderr.

## Operations

| Operation | Payload | Result |
|---|---|---|
| `config.initialize` | `timezone`, loopback `model_base_url`, `model_name` | Create a private zero-sender first-run config and return safe settings |
| `health.get` | `{}` | Database, generic mail-account catalog, legacy Gmail status, local-model health, notification mode, watchlist count, last check |
| `mail.accounts.list` | `{}` | Available mail providers and retained local accounts, without credential values or paths |
| `mail.accounts.connect` | `provider` | Run that provider's account flow and safely register or reuse the resulting mailbox identity |
| `mail.accounts.reconnect` | `provider`, `account_id` | Reauthorize exactly one retained account without changing its identity or mailbox cursor |
| `mail.accounts.disconnect` | `provider`, `account_id` | Remove that account's local read token while retaining local history and mailbox state |
| `mail.accounts.activate` | `provider`, `account_id` | Select one connected account for watcher polling |
| `gmail.authorize` | `{}` | Compatibility wrapper for the original read-only Gmail setup flow |
| `watcher.check` | optional `dry_run` boolean | Poll the active mail account with native delivery deferred to the host and the exact pending-intent count |
| `inbox.query` | optional bounded `limit`, opaque `cursor`, mail `provider`/`account_id`, sender/priority/category/status/keyword filters | Stable keyset page of matching local SQLite inbox rows plus `next_cursor` |
| `inbox.recent` | optional `limit` | Existing SQLite inbox rows with ordered attachment metadata; no raw bodies or attachment bytes |
| `inbox.delete` | `message_id` | Delete one message and all message-owned local state without changing source mail |
| `inbox.clear` | `{}` | Delete all local inbox messages and message-owned state while preserving mailbox and outbound state |
| `analysis.requeue` | `message_id` | Explicitly requeue one permanently paused analysis with a fresh request identity |
| `attachment.export` | `message_id`, `part_id`, `destination_dir` | Fetch one inventoried attachment into a private random file for a trusted host |
| `connect.entitlement.status` | `{}` | Claim-free shared-license state and active boolean |
| `connect.entitlement.install` | absolute `source_path` | Verify and atomically install an active signed license at the internally derived shared path |
| `connect.attachment.capabilities` | `message_id`, `part_id` | Every live v2 capability compatible with the inventoried attachment; transport credentials are not exposed |
| `connect.attachment.invoke` | stable `request_id`, attachment, provider/capability refs, parameters, `confirmed` | Revalidate and invoke one selected v2 capability; return or reuse its durable terminal result |
| `connect.output.present` | attachment, `job_id`, `artifact_id` | Return a validated native presentation for a completed output, or classify it as opaque |
| `connect.output.export` | attachment, `job_id`, `artifact_id`, `destination_dir` | Export one validated completed output to a private random `.bin` file for the trusted host |
| `connect.capabilities` | `{}` | Legacy v1 document-summary discovery |
| `connect.attachment.summarize` | `message_id`, `part_id` | Legacy v1 document-summary invocation |
| `watchlist.list` | `{}` | Normalized configured senders |
| `watchlist.add` | `email`, optional `name` | Add and return one normalized sender |
| `watchlist.remove` | `email` | Remove and return one normalized sender |
| `settings.get` | `{}` | Safe public settings, polling interval/support, and token-presence boolean |
| `settings.update` | one or more safe setting fields | Persist and return safe desktop settings |
| `host.operation_lock` | `{}` | Trusted-host-only canonical native operation-lock path |
| `notifications.pending` | optional `limit` | Durable native-notification intents |
| `notifications.pending_under_host_lock` | optional `limit` | Trusted-host-only intents while that lock is held |
| `notifications.count_under_host_lock` | `{}` | Trusted-host-only authoritative post-delivery queue count |
| `notifications.ack` | intent identity fields | State-checked, idempotent delivery acknowledgement |

`host.operation_lock`, `notifications.pending_under_host_lock`, and
`notifications.count_under_host_lock` are a trusted-host interface. The first returns the
configured operation-lock path; the notification operations may be called only while the host holds
that native exclusive lock. They let the desktop keep one cross-process lock across intent
selection, platform delivery, acknowledgement, and the final queue count without exposing the path
or lock-aware operations to frontend code. Other callers use `notifications.pending`, which
acquires the lock itself.

The `mail.accounts.*` operations are the provider-neutral desktop account contract. The current
build advertises Gmail and supports multiple retained Gmail identities, with exactly one active
polling account. Connect and reconnect stage authorization in private temporary storage, verify the
provider-reported mailbox identity, and only then atomically replace the internally derived token
file. Reconnect refuses an authorization for a different address. A new account receives its own
private token path; paths and token contents never enter the response. Disconnect removes only the
selected read token, leaving its cursor, Inbox rows, and the separate EOM `gmail.send` token intact.
Activating an account requires a local read token. Mutations share the production watcher lock, so
they cannot race a check or one another. Listing and migration perform no provider network access.

`gmail.authorize` remains a compatibility operation for the original default account. It uses only
the existing `gmail.readonly` authorization and never returns OAuth
credentials, token paths, token contents, or Gmail history identifiers. A new authorization starts
at the current mailbox state. Reusing an existing valid token preserves an existing history cursor;
if watcher state is missing, the operation initializes it from the current mailbox state. The OAuth
client identity may come from an explicit configured Desktop-client credentials file or, when that
file is absent, from a release-packaged client identity. Development builds without a packaged
identity still require the configured file. Account tokens are never read from the packaged
identity and remain in the user's private local state. Token access serializes the entire browser
authorization flow and rechecks token state under that lock, so an overlapping setup reuses the
completed token instead of opening another browser flow or racing token replacement. The engine
also serializes authorization through mailbox-baseline persistence, so overlapping first-run
requests cannot advance the initial history cursor twice. An unusable stored token causes this
explicit authorization operation to run the browser flow and replace the token only after that flow
succeeds. A locally valid token is also probed against Gmail; an HTTP 401 triggers the same explicit
reauthorization path, while other Gmail errors remain failures. The browser helper's authorization
prompt is suppressed because stdout is reserved exclusively for the JSON engine envelope. This
operation does not expose or request the separate EOM `gmail.send` capability.

`connect.entitlement.status` and `connect.entitlement.install` are app-local operations rather than
Connect wire routes. They do not load watcher configuration or private mailbox state. Status
returns only `active`, `authority_unavailable`, `missing`, `invalid`, `not_yet_valid`, `expired`, or
`feature_missing` plus an active boolean; it never returns IDs, subjects, timestamps, claims, or key
material. Install accepts no destination. It reads bounded bytes from an absolute regular source
without following a final symlink, applies the same compiled-authority signature, claim, feature,
and time checks as live discovery, and admits only a currently active license. Under the shared
owner-private `.entitlement-v1.lock`, it rechecks time, writes and syncs an exclusive mode-0600
same-directory temporary file, atomically replaces the shared entitlement, syncs the directory,
and re-evaluates before returning success. Expected validation, lock, write, and pre-replacement
failures preserve the existing entitlement and selected source. Stable failure codes follow the
accepted Connect activation v1 contract. Successful replacement is visible to both apps on their
next gated operation without restart.

`config.initialize` is the only watcher-configuration operation that may run before the
configuration file exists. It
requires an explicit IANA timezone, exact-loopback HTTP model endpoint, and nonblank printable model
identifier. It creates a mode-0600 configuration with zero senders, desktop notifications enabled,
the existing polling/retention defaults, and unauthenticated loopback inference. Publication is
atomic and create-only: an existing file returns `conflict` without being parsed, replaced, or
modified. The operation never accepts or returns OAuth credentials, model tokens, token paths,
gateway trust material, ntfy configuration, or EOM outbound settings. Authenticated inference,
gateway pairing, and the frontend onboarding form remain separate contracts.

Watchlist mutation is serialized and uses same-directory atomic replacement through the engine; the
frontend never parses or edits TOML. Adding a normalized duplicate returns `conflict`, removing an
address that is not watched returns `not_found`, and malformed payload values return
`invalid_request`. Configuration may contain zero senders for first-run onboarding. In that state,
`watcher.check` returns `active: false` without accessing Gmail; local retention cleanup and exact
pending-notification counting continue so removing the final sender cannot strand prior state.
Adding the first sender activates later Gmail checks.

`settings.get` reports the configured inference endpoint and model under `local_model` without
exposing token values or paths. Its `editable` flag is true only for the exact-loopback backend.
`settings.update` accepts `poll_interval_minutes` (1 through 1440), `retention_days` (1 through
3650), an exact boolean `notifications_enabled`, and, for an editable loopback backend only,
`model_base_url` and `model_name`. The endpoint remains restricted to explicit-port HTTP on
`localhost` or `127.0.0.1`; the model identifier must be nonblank printable text. Gateway-managed
inference configuration is read-only through this operation. Mutation uses the same serialized,
same-directory atomic replacement as watchlist updates and preserves all unrelated TOML fields and
comments. Empty payloads, unknown fields, wrong types, unsafe values, and attempts to mutate
gateway-managed model settings return `invalid_request` without changing the file. Backend choice,
credentials, token paths, Gmail settings, timezone, and EOM outbound configuration are not mutable
through this operation. When retention is included, the engine serializes the settings write with
watcher checks and applies the resulting source-time cutoff to SQLite before returning.

Each inbox item identifies its source mail `provider` and local `account_id` and carries an
`attachments` array. An attachment contains the provider's opaque `part_id`,
optional opaque `attachment_id`, display `filename`, `media_type`, and `byte_size`. The engine
persists this inventory before local-model analysis, so a temporary inference failure does not lose
the user's attachment list. Attachment bytes remain in Gmail and are not fetched or stored by this
operation. The trusted desktop host may call `attachment.export` with its private absolute temporary
directory. The engine resolves only an attachment already inventoried for that message, uses the
existing read-only Gmail authorization, rejects a byte-count mismatch, and creates a random
mode-0600 file that uses at most a validated alphanumeric extension from the email filename. The
response path is host-only; the Tauri command opens it natively and does not return it to frontend
JavaScript.

`inbox.query` is the desktop Inbox read contract. It returns at most 100 rows ordered by
`received_at DESC, message_id DESC`; its opaque cursor preserves that ordering when timestamps are
equal. Optional filters are combined with `AND`: sender matches literal case-insensitive text in
the address or display name, keyword matches literal case-insensitive text in subject or summary,
and priority, category, and status use fixed values. Mail provider and account filters select exact
source attribution. `untriaged` and `unclassified` select missing priority and category values.
Filtering and pagination read only local SQLite state and never call a mail provider, inference, or
a Connect provider. Rows include their mail source, existing category, and ordered
attachment and durable capability-result metadata. `inbox.recent` remains available for existing
CLI and host compatibility.

`inbox.delete` and `inbox.clear` are local-only privacy operations. They take the same production
operation lock as watcher checks, delete message rows transactionally, and rely on the existing
message-delete triggers to remove attachment inventory and Connect jobs/results. Notification
state is stored on the deleted message row. Mailbox cursor state and the isolated EOM outbound-send
ledger remain intact, and neither operation constructs a Gmail gateway. A bounded SHA-256
suppression marker prevents a later history replay from restoring manually deleted rows; it stores
no sender, subject, body, attachment, provider result, or raw Gmail message ID and expires once the
message is older than the maximum supported retention window.

Schema v10 adds the local mail-account registry and seeds the existing Gmail identity as the active
`gmail` / `gmail-default` account without network access. It preserves the schema v9
provider/account-scoped cursors,
message source identities, and deletion-suppression identities. Existing Gmail rows, cursor state,
attachments, and Connect results migrate locally to `gmail` / `gmail-default`; migration performs
no network access and preserves the existing local `message_id` used by the UI and relationships.
Each non-dry watcher operation purges by
`received_at` before pending analysis or notification delivery, rejects malformed or already
expired metadata before body fetch, clamps future-dated metadata to its observation time, bounds
stale-cursor search to the same cutoff, and reuses one cutoff through the complete check. Existing
future-dated rows use their local discovery time only as a safe retention fallback. Expiry removes
pending analysis and notification state as well as message-owned attachment and Connect state.

Inbox rows also expose `analysis_retryable`, `analysis_error_code`, and
`analysis_retry_after_seconds`. A retryable gateway failure remains scheduled in the durable
message ledger using the server delay when present. A permanent failure remains visible but is not
automatically selected by later checks. `analysis.requeue` accepts only such a permanently paused
row, clears its prior request identity and retry directives, and makes it eligible for a fresh
attempt. It does not acknowledge or discard a pending fallback notification.

`connect.attachment.capabilities` performs live v2 runtime discovery and filters every declaration
against the inventoried attachment's media type and byte size. It returns all compatible providers,
including exact application/instance attribution and the declaration fields required for native UI
matching. It never returns the registration path, loopback endpoint, or bearer token. Zero live
providers returns an empty list; multiple compatible providers remain available for explicit host
selection rather than being collapsed into an ambiguity error.

Discovery first verifies the independently signed `connect.capability_exchange` entitlement. A
denied entitlement returns an empty catalog without contacting any provider. Invocation rechecks
the same boundary before provider discovery, Gmail attachment retrieval, or Connect-job creation.
Completed persisted results and output presentation/export remain readable after expiry.

`scripts/connect-local-proof.py` also registers a deterministic synthetic provider that is unknown
to application runtime code. It proves that a second capability, required string parameters, two
providers for one capability, generic persistence/Inbox rendering, and bounded text presentation
all travel through these same operations. It also proves an unknown output remains opaque until the
trusted host exports it to a private random `.bin` path. Removing that registration removes its
actions while the remaining provider and normal Inbox stay healthy.

`connect.attachment.invoke` requires a caller-generated UUIDv4 plus exact provider application,
version, instance, capability, version, declared parameters, and confirmation state. It revalidates
entitlement and that selection immediately before a new handoff. Only then does it fetch the already inventoried
attachment through the existing read-only Gmail grant, verify its stored byte count, and persist the
request before provider submission. The provider receives generated artifact/job IDs, media type,
exact size, SHA-256, sanitized display name, source-app attribution, declared parameters, and the
artifact bytes. It does not receive the Gmail message/attachment IDs, sender, subject, body, local
path, OAuth data, or mailbox access.

The Connect columns introduced through schema v7 persist v2 request identity,
provider/capability versions, input provenance, parameters,
state, outputs, errors, and timestamps. Repeated calls with the same active identity reconcile the
same job. A lost submission acknowledgement remains nonterminal: the next call queries that
identity, and only authenticated `JOB_NOT_FOUND` evidence permits resubmission with the same ID.
V2 provider identity follows the provider-owned durable state namespace rather than one process;
runtime PID, endpoint, and bearer token may rotate. A restarted provider can therefore expose the
authoritative terminal state for a previously accepted job under the same selected identity, and the
consumer persists that state without reopening Gmail or creating another request.
Completed and failed requests replay from durable state before live discovery, so a provider outage
cannot erase an already authoritative result. Distinct caller request IDs remain distinct work.

`connect.output.present` revalidates stored output integrity and returns only bounded UTF-8 text or
the known document-summary schema to frontend code; every other media type is opaque. The trusted
host may use `connect.output.export` to write any validated output as a mode-0600 random `.bin` file
inside its private destination and open only the containing directory. Provider filenames are never
used as executable paths. Provider absence or Connect failure does not affect `inbox.recent`,
watchlist, health, or normal watcher operations.

The v1 `connect.capabilities` and `connect.attachment.summarize` operations remain for compatibility
with the existing Linux/EOM path; new desktop capability actions use the generic v2 operations.

Non-dry `watcher.check` requires the native hard-lock backend selected by `filelock`. Linux/macOS
use the platform `flock` implementation and Windows uses its native file-lock implementation.
`health.get` probes the configured database filesystem before reporting `production_check_supported`
and keeps `host_delivery_ready` false if only the soft fallback is available; dry-run and read-only
operations remain available meanwhile. The runtime includes the first-party `tzdata` package so
IANA configuration keys work on Windows hosts that do not provide a system timezone database.

## Native notification handoff

`watcher.check` persists analysis without invoking `notify-send`. The desktop host then:

1. resolves the canonical lock through `host.operation_lock`;
2. acquires that native exclusive lock;
3. calls `notifications.pending_under_host_lock`;
4. sends each intent through the native platform notification API;
5. calls `notifications.ack` only after the platform accepts it;
6. reads the authoritative remaining count through `notifications.count_under_host_lock`;
7. releases the lock after the entire batch.

The lock spans fetch through acknowledgement, so a concurrent CLI/systemd check, retention update,
or local-history mutation cannot remove or replace the selected state between native display and
acknowledgement. The standalone `notifications.pending` operation retains its self-locking behavior.

An analysis acknowledgement must echo `message_id`, `kind`, and `analysis_at` from the pending
intent. A fallback acknowledgement uses `message_id` and `kind`. Stale identities are rejected so
a delayed fallback acknowledgement cannot consume a newer analysis notification.

Host-deferred checks fail with `unsupported_configuration` when an ntfy topic is configured. The
existing CLI remains the canonical path for ntfy delivery; silently bypassing that configured
channel would lose its delivery contract. `health.get` reports `host_delivery_ready` and whether
ntfy is configured without exposing the topic.

Acknowledgement is idempotent. Delivery is at-least-once: if the host exits after platform
acceptance but before acknowledgement, the durable intent remains and may be delivered again after
restart. An unacknowledged intent is never treated as delivered.

When notifications are disabled, the host receives no pending analysis or fallback intents,
including intents queued before the setting changed. New analysis is completed without creating a
host-delivery intent. Inside the configured retention window, an unacknowledged intent remains
durable and retryable. Once its source message crosses the hard retention boundary, the intent and
all other message-owned local state expire together. Pending notification titles prefer the
configured watchlist name over untrusted message-header display names. The existing
`eom-mail-watch check` command retains its Linux notification behavior.
