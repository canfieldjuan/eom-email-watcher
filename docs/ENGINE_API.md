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
| `health.get` | `{}` | Database, Gmail token presence, local-model health, notification mode, watchlist count, last check |
| `watcher.check` | optional `dry_run` boolean | One Gmail poll with native delivery deferred to the host and the exact pending-intent count |
| `inbox.recent` | optional `limit` | Existing SQLite inbox rows with ordered attachment metadata; no raw bodies or attachment bytes |
| `analysis.requeue` | `message_id` | Explicitly requeue one permanently paused analysis with a fresh request identity |
| `attachment.export` | `message_id`, `part_id`, `destination_dir` | Fetch one inventoried attachment into a private random file for a trusted host |
| `connect.capabilities` | `{}` | Compatible capability declarations available now; provider identity and bearer token are not exposed |
| `connect.attachment.summarize` | `message_id`, `part_id` | Explicitly fetch and hand off one inventoried PDF; return or reuse the durable terminal result |
| `watchlist.list` | `{}` | Normalized configured senders |
| `watchlist.add` | `email`, optional `name` | Add and return one normalized sender |
| `watchlist.remove` | `email` | Remove and return one normalized sender |
| `settings.get` | `{}` | Safe public settings, polling interval/support, and token-presence boolean |
| `notifications.pending` | optional `limit` | Durable native-notification intents |
| `notifications.ack` | intent identity fields | State-checked, idempotent delivery acknowledgement |

Watchlist mutation is serialized and uses same-directory atomic replacement through the engine; the
frontend never parses or edits TOML. Adding a normalized duplicate returns `conflict`, removing an
address that is not watched returns `not_found`, and malformed payload values return
`invalid_request`. Configuration may contain zero senders for first-run onboarding. In that state,
`watcher.check` returns `active: false` without accessing Gmail; local retention cleanup and exact
pending-notification counting continue so removing the final sender cannot strand prior state.
Adding the first sender activates later Gmail checks. Settings mutation remains intentionally absent.

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

`connect.capabilities` performs live runtime discovery. Zero providers returns an empty list,
exactly one compatible provider returns `document.summarize` version `1.0`, and multiple providers
return an empty list with an `ambiguous_provider` diagnostic. Transport credentials and provider
identity stay inside the engine.

`connect.attachment.summarize` accepts only an existing PDF attachment identity. It rechecks live
discovery and the provider's byte limit before fetching bytes through the existing read-only Gmail
grant. The Connect request carries generated artifact/job IDs, media type, exact byte size, SHA-256,
sanitized display name, and source-app attribution. It does not carry the Gmail message ID, sender,
subject, body, path, OAuth data, or attachment ID. The provider receives the PDF as a multipart byte
stream, never as a caller filesystem path.

Email Watcher persists `requested -> accepted -> processing -> completed | failed` in schema v4.
Expected-state updates and a partial unique index protect one active job per attachment/capability
version. A completed result is reused on later calls; terminal failures never masquerade as a
summary. Stored output size, digest, input provenance, and media type are revalidated before a
summary is returned after reopen. Provider absence or Connect failure does not affect
`inbox.recent`, watchlist, health, or normal watcher operations.

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
