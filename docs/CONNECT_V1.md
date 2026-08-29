# Local Connect v1 consumer

Email Watcher is a Connect v1 consumer for one semantic capability:

```text
document.summarize@1.0
accepts: application/pdf
produces: application/vnd.local-connect.document-summary+json
```

It discovers capabilities, not installed application names. A differently named application can
satisfy the contract without an Email Watcher code change. Connect is optional: all existing mail,
watchlist, health, notification, and attachment-open behavior remains available without it.

The canonical language-neutral decision, JSON Schemas, and positive/negative fixtures are owned by
the separate `connect-contracts` repository. This document records Email Watcher's implemented
consumer behavior.

## Discovery

Providers atomically publish owner-only registrations under:

```text
$XDG_RUNTIME_DIR/local-connect/v1/providers/
```

The consumer has no fallback to a shared temporary directory. It accepts only owner-owned,
owner-readable regular registration files in an owner-only runtime directory; symlinks, unsafe
modes, unsupported protocols, malformed timestamps, non-loopback URLs, and oversized files are
ignored.

A registration is only a candidate. Availability requires an authenticated `GET /v1/manifest`
from the exact loopback endpoint and matching instance/app attribution. HTTP proxies are disabled,
redirects are refused, response bodies are bounded while streaming, and timestamps require a time
and UTC offset.

Selection is deterministic:

- zero compatible providers: no Summarize action;
- one compatible provider: show Summarize for PDFs within its declared limit;
- more than one: no implicit winner and an `ambiguous_provider` diagnostic.

Discovery occurs with Inbox refresh/window focus and again immediately before handoff. A runtime
file for a stopped provider is stale and is ignored.

## Explicit artifact handoff

The user selects Summarize on an inventoried attachment. Only then does the engine use its own
read-only Gmail grant to fetch that exact attachment and verify its stored byte count. It computes
the SHA-256 from those bytes and submits a multipart request with the bounded JSON descriptor first
and the PDF byte stream second.

The descriptor includes only generated job/artifact IDs, `application/pdf`, exact size, SHA-256,
sanitized display filename, and `source_app_id = email-watcher`. It excludes private paths, Gmail
message/attachment IDs, sender, subject, body, mailbox state, and credentials. The provider owns any
durable copy it imports; Email Watcher never reads the provider's storage or database.

## Job and result contract

The provider lifecycle is:

```text
accepted -> processing -> completed | failed
```

Email Watcher adds a local pre-handoff `requested` checkpoint. Every observed change is durably
persisted. Polling has bounded intervals and a wall-clock timeout; v1 has no callback, background
retry queue, or workflow scheduler.

A completed response is admitted only when job/capability/provider attribution and exact input
provenance match. The single inline output must have the expected media type, a supported summary
version, bounded text/warnings, and a canonical JSON size/SHA-256 match. The same integrity and
provenance checks run when a stored result is loaded after database reopen.

SQLite schema v4 stores jobs separately from email analysis. A partial unique index permits one
active job for an attachment and capability version; expected-state updates reject stale or
regressive transitions. Artifact/result fields and status shape are committed atomically. A
completed result is idempotently reused. A provider crash, timeout, malformed result, integrity
failure, or domain rejection becomes a durable failure and cannot produce a completed summary.

## UI boundary

The Tauri commands are thin adapters to the Python engine. The TypeScript frontend knows only the
semantic capability ID/version, accepted media type, declared limit, and attachment-local result.
It never receives the provider endpoint, token, registration, provider app ID, Gmail token, or PDF
bytes. Rendered summary/error text uses DOM text content rather than HTML interpretation.

Capability discovery failure is isolated from Inbox loading. Removing or stopping the provider
removes the contextual action after refresh while the same Email Watcher process continues normal
operation. Restoring a compatible provider restores the action without an Email Watcher change.

## Packaging and trust boundary

The Debian bundle includes `eom-mail-engine` as a target-triple Tauri sidecar. The installed desktop
runtime does not execute from a repository or require `uv`. The environment override and source/uv
fallback remain development/operator mechanisms and are not frontend-controlled.

The v1 protection is same-OS-user possession of a fresh per-process bearer token in an owner-only
runtime registration. The provider rejects browser-Origin requests; the consumer rejects remote
endpoints and redirects. This does not cryptographically authenticate an application against a
hostile process running as the same user. Email Watcher grants no mailbox or credential-store access:
only an explicitly selected attachment's bytes cross the boundary.

## Deferred

- broker, launch-on-demand, provider picker, leases, and callbacks;
- workflow engine/editor, automation rules, scheduling, and generated capability chains;
- Windows named pipes, macOS packaging, remote or multi-machine execution;
- package-identity attestation, third-party plugin SDK, marketplace, and enterprise RBAC;
- cloud sync/accounts, analytics, billing, licensing, and auto-update policy;
- automatic retries, re-summarize UX, OCR/vision, citations, and richer output rendering.
