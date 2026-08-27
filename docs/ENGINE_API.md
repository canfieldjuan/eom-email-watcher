# Engine API v1

`eom-mail-engine` is the machine-facing boundary for desktop hosts. It is a one-shot process:
write one JSON request to stdin, read one JSON response from stdout, then inspect the exit status.
Human diagnostics go to stderr.

The first technical-user build may invoke the command from the installed Python environment. A
future packaged sidecar must preserve this protocol.

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
| `inbox.recent` | optional `limit` | Existing SQLite inbox rows; no raw bodies |
| `watchlist.list` | `{}` | Normalized configured senders |
| `watchlist.add` | `email`, optional `name` | Add and return one normalized sender |
| `watchlist.remove` | `email` | Remove and return one normalized sender |
| `settings.get` | `{}` | Safe public settings and token-presence boolean |
| `notifications.pending` | optional `limit` | Durable native-notification intents |
| `notifications.ack` | intent identity fields | State-checked, idempotent delivery acknowledgement |

Watchlist mutation is serialized and uses same-directory atomic replacement through the engine; the
frontend never parses or edits TOML. Adding a normalized duplicate returns `conflict`, removing an
address that is not watched returns `not_found`, and malformed payload values return
`invalid_request`. Configuration may contain zero senders for first-run onboarding. In that state,
`watcher.check` returns `active: false` with zero work without accessing Gmail; adding the first
sender activates later checks. Settings mutation remains intentionally absent.

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
