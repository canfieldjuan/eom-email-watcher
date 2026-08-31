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
| `health.get` | `{}` | Database, Gmail token presence, local-model health, notification mode, watchlist count, last check |
| `gmail.authorize` | `{}` | Run the configured read-only Gmail OAuth flow and initialize a new mailbox baseline when required |
| `watcher.check` | optional `dry_run` boolean | One Gmail poll with native delivery deferred to the host and the exact pending-intent count |
| `inbox.recent` | optional `limit` | Existing SQLite inbox rows with ordered attachment metadata; no raw bodies or attachment bytes |
| `analysis.requeue` | `message_id` | Explicitly requeue one permanently paused analysis with a fresh request identity |
| `attachment.export` | `message_id`, `part_id`, `destination_dir` | Fetch one inventoried attachment into a private random file for a trusted host |
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
| `notifications.pending` | optional `limit` | Durable native-notification intents |
| `notifications.ack` | intent identity fields | State-checked, idempotent delivery acknowledgement |

`gmail.authorize` uses only the existing `gmail.readonly` authorization and never returns OAuth
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

`config.initialize` is the only operation that may run before the configuration file exists. It
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

`settings.update` accepts only `poll_interval_minutes` (1 through 1440), `retention_days` (1 through
3650), and an exact boolean `notifications_enabled`. Mutation uses the same serialized,
same-directory atomic replacement as watchlist updates and preserves all unrelated TOML fields and
comments. Empty payloads, unknown fields, wrong types, and out-of-range values return
`invalid_request` without changing the file. Model endpoint, model identifier, credentials, token
paths, Gmail settings, timezone, and EOM outbound configuration are not mutable through this
operation.

Each inbox item carries an `attachments` array. An attachment contains the Gmail MIME `part_id`,
optional opaque `attachment_id`, display `filename`, `media_type`, and `byte_size`. The engine
persists this inventory before local-model analysis, so a temporary inference failure does not lose
the user's attachment list. Attachment bytes remain in Gmail and are not fetched or stored by this
operation. The trusted desktop host may call `attachment.export` with its private absolute temporary
directory. The engine resolves only an attachment already inventoried for that message, uses the
existing read-only Gmail authorization, rejects a byte-count mismatch, and creates a random
mode-0600 file that uses at most a validated alphanumeric extension from the email filename. The
response path is host-only; the Tauri command opens it natively and does not return it to frontend
JavaScript.

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

`scripts/connect-local-proof.py` also registers a deterministic synthetic provider that is unknown
to application runtime code. It proves that a second capability, required string parameters, two
providers for one capability, generic persistence/Inbox rendering, and bounded text presentation
all travel through these same operations. It also proves an unknown output remains opaque until the
trusted host exports it to a private random `.bin` path. Removing that registration removes its
actions while the remaining provider and normal Inbox stay healthy.

`connect.attachment.invoke` requires a caller-generated UUIDv4 plus exact provider application,
version, instance, capability, version, declared parameters, and confirmation state. It revalidates
that selection immediately before a new handoff. Only then does it fetch the already inventoried
attachment through the existing read-only Gmail grant, verify its stored byte count, and persist the
request before provider submission. The provider receives generated artifact/job IDs, media type,
exact size, SHA-256, sanitized display name, source-app attribution, declared parameters, and the
artifact bytes. It does not receive the Gmail message/attachment IDs, sender, subject, body, local
path, OAuth data, or mailbox access.

Schema v7 persists v2 request identity, provider/capability versions, input provenance, parameters,
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

Non-dry `watcher.check` currently requires POSIX advisory locking. `health.get` reports
`production_check_supported` and keeps `host_delivery_ready` false on unsupported platforms. A
future Windows host must add an equivalent lock before enabling production checks; dry-run and
read-only operations remain available meanwhile.

## Native notification handoff

`watcher.check` persists analysis without invoking `notify-send`. The host then:

1. calls `notifications.pending`;
2. sends each intent through the native platform notification API;
3. calls `notifications.ack` only after the platform accepts it.

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
host-delivery intent. Retention never purges an unacknowledged host-delivery intent. Pending
notification titles prefer the configured watchlist name over untrusted message-header display
names. The existing `eom-mail-watch check` command retains its Linux notification behavior.
