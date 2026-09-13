# Require informed opt-in for ntfy content

## Why this slice exists

Email Watcher issue #134 is the only open Email Watcher issue in the `First
Public Release` milestone. ntfy is disabled unless a topic is configured, but
the existing setup text describes topic secrecy without saying that the ntfy
service receives email-derived content. A topic alone therefore acts as an
uninformed enable switch.

The estimated diff exceeds the 400-line soft cap because the repository requires
this plan contract and the privacy boundary needs complete payload assertions
for all three notification kinds plus opposite-side config/caller probes. The
runtime mechanism itself remains limited to config validation, one delivery
guard, and four production call sites; splitting the tests or disclosure from
the guard would make the release claim unreviewable.

### Problem-derived contract

- **Root cause:** `load_config` accepts `ntfy_topic` without a separate
  acknowledgement of the disclosure, while the notification title can send a
  configured sender label, a message-supplied display name when no label exists,
  or the sender address, followed by the subject. `send_analysis` also sends the
  model summary, optional suggested action, and optional deadline.
  `send_review` can also send review text. The notification tests assert only
  selected JSON members, so they do not close the outbound payload shape.
- **Correct fix must touch/change:** add an exact boolean acknowledgement to the
  typed config; reject a configured topic unless that value is literally
  `true`; carry it through every production ntfy caller; enforce it again
  immediately before notification-channel side effects; document every emitted
  JSON field, every sender-label fallback, and which values contain email-derived
  content; and assert the
  complete analysis, fallback, and review payloads.
- **Must not change:** the content-bearing payload selected by the operator,
  desktop notification independence, topic format, HTTPS requirement,
  delivery/retry semantics, mailbox/model behavior, Local Connect, or the
  desktop host's rule that CLI owns configured ntfy delivery.

### Assumptions and blockers

- The operator explicitly accepted content-bearing ntfy as an opt-in first-release
  mode on 2026-09-13.
- ntfy setup remains private-config/CLI behavior; the desktop application does
  not currently configure or deliver ntfy.
- A wake-only mode and any phone-to-PC detail retrieval are later product work
  tracked by issue #137. They do not block this first-release disclosure fix.

## Scope (this PR)

Ownership lane: Email Watcher first-release ntfy privacy
Slice phase: first-release privacy slice

1. Require informed acknowledgement before any configured ntfy topic is usable.
2. Put the exact content disclosure next to the opt-in setting and in the main
   setup documentation.
3. Prove the complete outbound JSON shape and production acknowledgement
   propagation without sending a real notification.

### Review Contract

Acceptance criteria:

1. `tests/test_config.py` proves no topic remains disabled by default; a topic
   with an absent, false, or non-boolean acknowledgement fails; and a valid
   topic with literal `true` loads.
2. `tests/test_notifications.py` proves a topic without acknowledgement is
   rejected before desktop or HTTP side effects, while an acknowledged topic
   emits exactly `topic`, `title`, `message`, and `priority`.
3. Exact payload assertions cover analysis content, metadata-only fallback, and
   scheduling-review content. Equality of the complete mapping proves other
   `Analysis` fields are not separate JSON members.
4. Service and CLI tests prove the typed acknowledgement reaches every
   production `send_analysis`, `send_fallback`, and `send_review` path;
   engine API fixtures that intentionally configure ntfy include the same
   acknowledgement required from deployed TOML.
5. `README.md` and `config.example.toml` enumerate the configured-label,
   message-supplied-display-name, and address title fallbacks; state that HTTPS is
   transport encryption only; explain that the topic and ntfy service can read and
   may retain or log the content; and tell confidentiality-sensitive operators
   they can leave ntfy unset and keep desktop notifications.
6. The existing notification, config, CLI, service, and full repository suites
   remain green.

Reachability proof: a config loaded from the real TOML entrypoint supplies the
acknowledgement to the real CLI/service notification path; a captured
`httpx.post` call observes the exact JSON that would cross the network while
the test performs no network request.

Affected surfaces: private TOML configuration, CLI/service notification
callers, ntfy delivery admission, setup documentation, and notification/config
tests.

Risk areas: implicit truthiness, existing configured topics, bypass through one
of the three notification kinds, disclosure text drifting from code, and
desktop-only regression.

Reviewer rules triggered: R1, R2, R3, R5, R6, R10, R11, R13, and R14.

### Boundary-change enumeration

- Boundary path/seam: TOML `ntfy_topic` plus
  `ntfy_content_disclosure_acknowledged` -> typed `Config` -> CLI/service
  notification call -> `_deliver` -> `_send_ntfy`.
- Replaced-path behaviors: a topic alone formerly sent content; it now fails
  closed before any channel side effect. A topic plus literal `true` preserves
  the existing payload.
- Guard-relevant fields: topic absent/present, acknowledgement absent, `false`,
  `true`, and wrong TOML type.
- Caller x input shape: analysis, fallback, and review notifications; CLI
  fallback and service delivery entrypoints; desktop-only delivery with no
  topic.

### Deployed-config probing

- Deployed/default config values: topic absent and acknowledgement absent keeps
  ntfy disabled.
- Explicit value probe: a valid topic plus literal `true` reaches the captured
  HTTP call with the exact existing payload.
- Absent value probe: a configured topic without acknowledgement raises an
  actionable `ConfigError`; direct notification admission without it raises
  `NotificationError`.
- Default-session/default-context probe: desktop-only notification delivery
  remains accepted without acknowledgement.
- Side-effect ordering: the delivery guard runs before `notify-send` and
  `httpx.post`; tests make both fail if reached on rejection.

### Files touched

- `README.md`
- `config.example.toml`
- `docs/ENGINE_API.md`
- `plans/PR-Ntfy-Privacy-Opt-In.md`
- `src/eom_email_watcher/cli.py`
- `src/eom_email_watcher/config.py`
- `src/eom_email_watcher/notifications.py`
- `src/eom_email_watcher/service.py`
- `tests/test_cli.py`
- `tests/test_config.py`
- `tests/test_engine_api.py`
- `tests/test_notifications.py`
- `tests/test_service.py`

## Mechanism

`Config` carries an exact boolean
`ntfy_content_disclosure_acknowledged`. Configuration parsing rejects any
non-boolean value and rejects `ntfy_topic` unless the acknowledgement is
literally `true`. CLI and service callers pass the typed value to each
notification helper. `_deliver` repeats the topic-plus-acknowledgement check
before attempting either desktop or ntfy delivery, closing direct internal
callers as well as configuration-based entrypoints.

The ntfy JSON stays content-bearing and unchanged. Setup text enumerates its
four keys and explains every title-label fallback plus the message contents for
analysis, fallback, and review notifications. Tests compare complete mappings
rather than individual members, so adding another outbound key requires an
explicit test and disclosure change.

## Intentional

- Existing private configs with `ntfy_topic` must add the acknowledgement
  before the watcher will start. Silent grandfathering would defeat informed
  opt-in.
- No sanitization or application-level encryption is claimed. The configured
  ntfy service can read content even when HTTPS protects it in transit.
- The setting does not imply that the ntfy service deletes content or avoids
  logs/backups; operators must evaluate their selected service.
- The desktop host UI is unchanged because it does not configure ntfy and
  already refuses host-deferred delivery when a topic is present.

## Deferred

- Issue #137 owns opaque/wake-only ntfy mode, authenticated detail retrieval,
  business/privacy profiles, and confidentiality-sensitive recommendations.
- ntfy server retention controls, end-to-end encryption, custom mobile clients,
  and private-network access are outside this release slice.

Parking predicate: new relay/client infrastructure, business-specific policy
automation, and notification-content redesign remain parked unless the informed
opt-in cannot be enforced without them.

Parked hardening: issue #137 records the later opaque notification mode.

## Verification

- Fail-first boundary probe: `9 failed, 2 passed`; it proved current config
  accepted missing/false/string acknowledgements and unacknowledged delivery
  reached a channel side effect.
- `uv run pytest -o addopts='' --tb=short tests/test_config.py -k ntfy` —
  `7 passed, 100 deselected`.
- `uv run pytest -o addopts='' --tb=short tests/test_notifications.py` —
  `15 passed`.
- CLI/service/engine reachability probes — `4 passed`.
- Affected config/notification/CLI/service/engine suites before the final
  expanded boolean parameterization — `411 passed in 56.92s`.
- `uv run pytest -o addopts='' --tb=short` — `1309 passed, 16 skipped in
  84.19s`.
- `uv run ruff check .` — `All checks passed!`.
- Documentation-to-code trace: `service.py:1064` and `service.py:1356` select
  configured label, then message-supplied display name, then sender address;
  both disclosure surfaces enumerate that order.
- `git diff --check` — clean.
- Cold diff reconstruction found and removed formatter-only changes outside the
  contract. The remaining 13-file diff traces only to config admission,
  delivery enforcement/callers, exact payload tests, and disclosure text; no
  contract gap remains.

## Estimated diff size

| File | LOC |
|---|---:|
| `README.md` | 42 |
| `config.example.toml` | 18 |
| `docs/ENGINE_API.md` | 4 |
| `plans/PR-Ntfy-Privacy-Opt-In.md` | 225 |
| `src/eom_email_watcher/cli.py` | 1 |
| `src/eom_email_watcher/config.py` | 10 |
| `src/eom_email_watcher/notifications.py` | 9 |
| `src/eom_email_watcher/service.py` | 7 |
| `tests/test_cli.py` | 16 |
| `tests/test_config.py` | 37 |
| `tests/test_engine_api.py` | 6 |
| `tests/test_notifications.py` | 133 |
| `tests/test_service.py` | 28 |
| **Total** | **536** |
