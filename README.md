# EOM Email Watcher

A private, local-first watched-sender email application. It checks one selected Gmail, Microsoft
365, or IMAP mailbox, selects messages only from an exact sender allowlist, asks a configured local
model for a short structured summary, and sends a desktop notification (and, optionally, a phone
push via [ntfy](https://ntfy.sh)).

Email content is never sent to a cloud model. Optional ntfy phone notifications are a separate,
explicitly acknowledged disclosure to the configured ntfy service, described below. Mailbox grants
are read-only, attachment content is downloaded only when the user explicitly opens it or requests
an available local capability, non-matching message metadata is not stored, and message bodies are
discarded after each local inference request.

## Behavior

- Starts at the provider's current cursor; setup does not backfill old mail.
- Polls Gmail History, Microsoft Graph inbox delta, or IMAP UID changes for newly created messages.
- With an active paid Connect entitlement, also polls the Sent folder (Gmail's `SENT` label, Microsoft
  Sent Items, the IMAP `\Sent` folder or a configured name) and records which admitted folders each
  captured message is in, with its To and Cc recipients, for vendor threads. An IMAP account with no
  Sent folder shows "Sent mail unavailable" in Health.
- Verifies the parsed `From` address against a case-insensitive exact allowlist in trusted config.
- Fetches message bodies only after a sender matches.
- Extracts `text/plain`, or text from HTML as a fallback, capped at 20,000 characters.
- Records attachment filenames, provider-owned attachment IDs when available, media types, and
  provider-reported byte sizes. An explicit Open action fetches only that attachment into a
  mode-0600 process-owned temporary file.
- With an active paid Connect entitlement, discovers the provider-neutral `document.summarize`
  capability at runtime. For a compatible PDF, an explicit Summarize action fetches only that
  attachment and streams its bytes to the selected authenticated exact-loopback provider. It does
  not transfer mailbox paths, sender, subject, message ID, email body, or mailbox credentials.
- Stores message metadata, attachment metadata, and model summaries in SQLite for 180 days. Bodies
  are never stored.
- Deduplicates by mail provider, local account, and provider message ID. Existing single-account
  Gmail history retains its original local identifiers during the offline schema migration.
- Recovers an expired provider cursor beginning five minutes before the last successful check,
  bounded by retention, then saves a fresh cursor. Exact sender admission still occurs locally.
- If the configured model endpoint is unavailable or times out, sends one metadata-only fallback
  notification and retries the summary later with backoff.

## Requirements

- Python 3.13 and [`uv`](https://docs.astral.sh/uv/)
- Git: `uv sync` resolves the pinned `connect-automate` dependency over Git, so a
  Git executable must be on `PATH` at install time
- An administrator-managed on-prem inference gateway, or LM Studio `llmster` with its API bound to
  `127.0.0.1`
- `notify-send` (normally provided by `libnotify-bin`)
- A Gmail/Google Workspace account with a Google Desktop OAuth client, a Microsoft 365 work or
  school account with an Entra public desktop-client registration, or an IMAP account using TLS or
  STARTTLS
- Optional: an [ntfy](https://ntfy.sh) topic for phone push notifications alongside the desktop
  one -- read [Optional ntfy phone push and privacy](#optional-ntfy-phone-push-and-privacy)
  before enabling it

## Install

```bash
uv sync --locked --all-groups
state_dir="${STATE_DIRECTORY:-${XDG_STATE_HOME:-$HOME/.local/state}/eom-email-watcher}"
mkdir -p ~/.config/eom-email-watcher "$state_dir"
cp config.example.toml ~/.config/eom-email-watcher/config.toml
chmod 700 ~/.config/eom-email-watcher "$state_dir"
chmod 600 ~/.config/eom-email-watcher/config.toml
```

Email Watcher resolves that state directory for every database, credential, token, operation lock,
and scheduled monthly-hours artifact. A systemd user service uses its absolute
`$STATE_DIRECTORY`; interactive CLI and desktop runs use `$XDG_STATE_HOME/eom-email-watcher`, or
`~/.local/state/eom-email-watcher` when XDG state is unset. The supplied user units and interactive
runs therefore converge when the user manager has a custom XDG state home.

Edit the private config with the real exact sender list and one of the inference configurations
below. Never commit that config; the repository example intentionally contains placeholders.

### Optional ntfy phone push and privacy

ntfy is disabled by default. Enabling it requires both a valid `ntfy_topic` and the literal
`ntfy_content_disclosure_acknowledged = true` in the private `config.toml`. Email Watcher refuses
to start with a topic but without that acknowledgement.

For every phone notification, Email Watcher sends one HTTPS JSON request to the configured
`ntfy_url` containing exactly:

- `topic`: the configured topic;
- `title`: the watched sender's configured label; otherwise the message-supplied display name;
  otherwise the email address; followed by the email subject;
- `message`: for a completed analysis, the local model summary plus any suggested action and
  normalized deadline, and, when the summary covers only part of a long email, a fixed note saying
  so; for a fallback, fixed retry text; for scheduling review, review text that may contain an
  email-derived summary; and
- `priority`: a numeric value derived from the local analysis, or the normal priority for fallback
  and review notifications.

The application does not redact or encrypt these fields at the application layer. HTTPS protects
the request while it travels to the server, but the configured ntfy service can read the topic,
configured sender label, message-supplied display name, email address, subject, summary, suggested
action, deadline, partial-summary note, and review text. A long random
topic limits who can subscribe or publish; it does not hide content from the service. The service may
also retain this content in logs, storage, or backups according to its own policy.

For legal, medical, financial, or other confidentiality-sensitive mail, leave `ntfy_topic` unset
unless that disclosure is acceptable. Desktop notifications remain available independently. A
future opaque/wake-only mode is tracked in
[issue #137](https://github.com/canfieldjuan/eom-email-watcher/issues/137).

### Secure shared inference gateway

For an administrator-managed gateway, set `model_backend = "gateway"`, use its private-LAN HTTPS
URL, and install the administrator-provided application credential and CA certificate at the paths
named by `model_api_token_file` and `model_ca_file`. The credential file must be owner-only; the CA
file must not be group/world-writable. Do not put the credential value in TOML.

Gateway mode sends only the same bounded text inference request used by the local watcher. The
application does not select a worker or model, and mailbox credentials, raw message storage, and
Local Connect tokens remain outside the inference boundary. Use the desktop Health view to confirm
that `email.analyze` is available before enabling normal polling. Scheduling automation additionally
requires the same application credential to be granted `email.schedule.extract@1`; a missing grant
fails closed and does not fall back to a direct model runtime.

### Secure LM Studio

The watcher rejects any non-local model URL. In LM Studio, use:

- network interface `127.0.0.1`
- CORS disabled
- sensitive-data and incoming-token logging disabled
- just-in-time model loading enabled
- **Require Authentication** enabled with a dedicated inference token

Start the local service:

```bash
lms daemon up
lms server start --bind 127.0.0.1 -p 1234
lms server status
```

The configured model may load on the first matching email. LM Studio unload behavior is controlled
by its JIT/TTL settings.

In LM Studio 0.4+, open **Developer > Server Settings**, enable **Require Authentication**, create
a token for this watcher, and paste only the token value into:

```bash
state_dir="${STATE_DIRECTORY:-${XDG_STATE_HOME:-$HOME/.local/state}/eom-email-watcher}"
install -m 600 /dev/null "$state_dir/lmstudio-api-token"
# Edit the file and paste the token on one line; do not put it in config.toml.
```

The watcher refuses to send email text to LM Studio when authentication is required but this token
file is missing. LM Studio's CLI can start the server but token creation is currently a GUI action.

## Gmail read-only OAuth setup

1. In Google Cloud Console, create or select a project.
2. Enable the Gmail API.
3. Configure the OAuth consent screen as Internal for the Workspace organization when available.
4. Create an OAuth Client ID with application type **Desktop app**.
5. Download the client JSON to `credentials.json` inside the runtime state directory resolved during
   installation.
6. Lock it down and authorize:

```bash
state_dir="${STATE_DIRECTORY:-${XDG_STATE_HOME:-$HOME/.local/state}/eom-email-watcher}"
chmod 600 "$state_dir/credentials.json"
uv run eom-mail-watch setup
```

The browser consent request asks only for
`https://www.googleapis.com/auth/gmail.readonly`. The token is stored mode `0600`. Setup saves the
current Gmail cursor, so only future arrivals are processed.

## Microsoft 365 read-only OAuth setup

Microsoft 365 uses the same native **Add account** control in the desktop Health view. A release
build may embed this non-secret public-client file; a source build can place it at
`microsoft-oauth-client.json` inside the resolved runtime state directory:

```json
{
  "client_id": "00000000-0000-0000-0000-000000000001",
  "tenant": "organizations"
}
```

The Entra registration must be a public mobile/desktop client, allow the `http://localhost`
redirect URI, and have delegated Microsoft Graph `Mail.Read` permission. `tenant` may be
`organizations` for a multi-tenant work/school release or one tenant UUID. Client secrets are
rejected: desktop public clients cannot keep one, and this file is reusable application identity,
not a user grant. The browser flow stores each user's MSAL cache only in private local account
state. It requests no `Mail.ReadWrite`, `Mail.Send`, or application permission. Setup records a
local start boundary without importing existing mail; the first watcher check completes that delta
round and later checks persist Graph's opaque delta URL. This keeps mail that arrives while account
setup is finishing inside the normal exact-sender gate.

## Commands

```bash
uv run eom-mail-watch doctor
uv run eom-mail-watch check --dry-run
uv run eom-mail-watch check
uv run eom-mail-watch recent --limit 20
uv run eom-mail-watch requeue-analysis MESSAGE_ID
```

`--dry-run` does not advance the active mailbox cursor, add database rows, or send real
notifications.

Run the opt-in real-IMAP integration proof with Docker and OpenSSL available:

```bash
bash scripts/test-imap-greenmail.sh
```

The script starts a pinned GreenMail container on random loopback-only ports, creates a temporary
TLS certificate, and proves real authentication, baseline cursor setup, new-message polling, MIME
and PDF attachment reads, and unchanged source message flags. It stops the container and removes
the temporary certificate material when the test exits. SMTP is used only to seed the isolated
test mailbox; the application does not expose an SMTP or source-mail write adapter.

Desktop hosts use the versioned one-shot JSON contract documented in
[`docs/ENGINE_API.md`](docs/ENGINE_API.md). The existing human CLI remains the Linux/systemd entry
point.

The Linux Tauri application provides a local Inbox with confirmed local-only delete/clear controls,
GUI watchlist management, a Vendors list that groups the exact addresses each vendor writes from
(adding vendors and addresses needs an active Connect entitlement; viewing and removing never do),
safe polling, source-time retention and notification settings, live
health status, a safe one-shot `Check now` action, and
contextual local capabilities for attachments. Its Debian package includes the Python engine as a
Tauri sidecar, so the installed application does not depend on a source checkout or `uv`. It still
requires an existing private config; see [`desktop/README.md`](desktop/README.md). A release build
may include approved Google and Microsoft public desktop-client identities; source and development
builds without them require the corresponding external client files. In either case, Health can run
the read-only browser authorization, list retained accounts, connect or reconnect Gmail and
Microsoft 365 accounts, disconnect only their local read token/cache, and explicitly choose the one
account polled by the watcher. Account tokens remain in the user's private local state, and disconnecting an account
does not remove its local Inbox history or the separate EOM send authorization. Both adapters use
the same provider-neutral account and Inbox UI. The app does not replace the systemd
scheduler or the existing CLI's optional ntfy delivery.

## Local Connect capability

Inbox attachment cards show every compatible capability advertised by live Local Connect v2
providers only when this installation has an active signed entitlement containing
`connect.capability_exchange`. One provider produces a normal action; multiple providers produce
an explicit native picker. The host revalidates both entitlement and the exact provider,
capability version, parameters, size, and effect confirmation before handoff. Denied invocation
stops before source attachment download or Connect-job persistence. Actions disappear when the
entitlement expires or providers stop, while mailbox monitoring and the rest of the desktop remain
usable. Completed persisted results remain readable. Jobs, results, errors, and complete
provider/input/output provenance remain in Email Watcher's private SQLite database under a stable
caller request ID.

On Linux, the entitlement is read on every discovery/invocation from
`$XDG_CONFIG_HOME/local-connect/entitlement-v1.json`, or from
`$HOME/.config/local-connect/entitlement-v1.json` when `XDG_CONFIG_HOME` is unset or empty. The directory
must be owner-only mode `700`; the regular non-symlink file must be owner-only mode `600`. Validity
is exact (`issued_at <= not_before <= now < expires_at`) with no hidden grace. Replacing the file with a valid
signed entitlement restores capability discovery without restarting Email Watcher. Mailbox OAuth is
not a Connect license and is never shared with a provider.

On Windows, the shared entitlement is
`%LOCALAPPDATA%\LocalConnect\entitlement-v1.json`, and v1/v2 provider registrations are read from
`%LOCALAPPDATA%\LocalConnect\runtime\v1|v2\providers`. The implementation relies on the current
user's Local AppData ACL boundary, rejects reparse-point indirection, and fails closed when the
root or a Connect descendant grants content or mutation access beyond the user, SYSTEM, or built-in
Administrators. It does not require XDG variables.

The desktop Health view reports only the license state and whether Connect is active. Its
**Activate** action accepts a user-selected license, while the Python engine independently reads
bounded regular-file bytes without following a final symlink, verifies the same signature,
feature, and time contract used by discovery, and writes only to the fixed shared path. Email
Watcher and other Connect apps serialize activation through `.entitlement-v1.lock`; the engine
flushes and atomically promotes a same-directory temporary file, then re-evaluates the installed
file before success. Linux additionally enforces owner-only modes and syncs the directory; Windows
uses a non-blocking byte-range lock and atomic replacement under Local AppData. Invalid or inactive
sources and expected failures before promotion preserve both the selected source and any existing
entitlement. Status refreshes while the app remains open, so another app's activation, replacement,
removal, or expiry does not leave the Health card indefinitely stale.

The current machine contract, including generic discovery, invocation, durable reconciliation, and
safe output presentation/export, is documented in [`docs/ENGINE_API.md`](docs/ENGINE_API.md). The
legacy v1 summary-only boundary remains documented in [`docs/CONNECT_V1.md`](docs/CONNECT_V1.md).
Language-neutral wire schemas and fixtures live in the separate `connect-contracts` repository.

Run the opt-in consumer conformance check against the pinned canonical v2 corpus from the repository
root:

```bash
CONNECT_CONTRACTS_DIR=/absolute/path/to/connect-contracts \
  uv run pytest -q conformance/test_connect_v2_contracts.py
```

The check reads fixtures from canonical Git revision
`4d46af25ef5112f76daf841c7622987f05d25142`; it does not trust or copy the contracts checkout's
working tree. Updating that pin requires an explicit compatibility change.

The signed-entitlement conformance check now lives with the entitlement gate in
the [`connect-automate`](https://github.com/canfieldjuan/connect-automate)
package that this app depends on. Run it from a checkout of that repository,
against its independently pinned canonical revision:

```bash
CONNECT_CONTRACTS_DIR=/absolute/path/to/connect-contracts \
  uv run pytest -q tests/test_entitlement.py::test_canonical_entitlement_v1_fixtures
```

That check reads entitlement fixtures from Git revision
`c5405935bd1354cf6a4c8539425a53dfd7f52949`, which also contains the accepted
activation contract.

## Two-hour user timer

After mailbox setup succeeds, select the concrete sidecar shipped with the desktop:

```bash
./scripts/install-user-services.sh --engine /absolute/path/to/desktop/eom-mail-engine
systemctl --user start eom-email-watcher.timer
systemctl --user status eom-email-watcher.timer
journalctl --user -u eom-email-watcher.service --since today
```

The selected native engine owns its paired alias and all five scheduled unit files.
Installation never searches PATH for a desktop. Before every packaged Linux database
open, the manager environment resolves deployment paths and one bounded unit snapshot
checks the shipped graph, loaded scheduled commands, unit-file bytes and reader
identities. A shared deployment lease stays held until the SQLite connection closes.
Native and source installers retain an exclusive lease across publication, reload and
verification. A busy deployment refuses access after a bounded wait: retry a reader
after the update; close the app or stop the timers before retrying an installer.
The real account-home directory inode is the shared/exclusive coordination anchor;
no state lock file is used or created. Its identity is checked before and after
acquisition, so clearing old state lock files cannot release a live connection's
lease. Close sessions from earlier builds and stop timers before installing this
version, because those builds used a different lock anchor. Replacing the home
directory is not supported during a session. A home filesystem that cannot open or
flock this directory intentionally refuses access, with no state-file fallback.
An unrelated process locking the home directory also delays access until the normal
bounded deployment refusal.
Select the concrete native sidecar for installation; selecting its scheduled alias or
a path through that alias refuses publication before writes. Modified or redirected
units, persistent drop-ins, pending reloads,
partial installation and incompatible running readers refuse access. Let an old
reader finish and reinstall using the updated desktop sidecar. Compatible paired
readers retain the existing operation locks and dedupe. See the
[deployment contract](plans/PR-Paired-Scheduled-Engine.md) for the exact admission policy.

**A masked unit or missing user manager intentionally blocks every packaged desktop
database open.** Repair the unit/manager and reinstall the paired deployment before
retrying. A deployment refusal is reported as `deployment_refused`, rather than a
stale or concurrent update. The LM Studio helper is part of the pinned graph; its
canonical retained active state is allowed once its execution PIDs are zero.

The paired deployment requires a user manager whose effective units have no
drop-ins, including vendor-wide `service.d` or `timer.d` overrides. A host with
those overrides is refused until they are removed. The manager's `HOME` and
`XDG_CONFIG_HOME` own the alias and unit directory, including when the desktop was
launched with a different home. Before writing any lock, alias or unit, the resolver
requires the exported `HOME` to equal the real account home and the unit directory
to appear in the user manager's `UnitPath`. Missing or mismatched identities refuse
access before publication. One decoder handles the manager environment's ANSI-C
quoting and UnitPath's generic double quoting, including spaces and non-ASCII paths.

The five shipped units also pin their direct activation dependencies. Unshipped
services refuse admission. Additional OS defaults may be passive targets or slices
only when the manager reports protected vendor/generated inputs, without user-write
authority or untrusted overrides. This permits new vendor default names after an
OS upgrade. The shipped, inactive network target may be absent. Transitive platform
target dependencies are outside this five-unit policy.

Auxiliary dependency and override inputs across the manager's load paths must
match the shipped deployment too. Retained `.wants`, `.requires`, `.upholds` or
nonempty drop-in directories refuse before installation writes, even when the base
units are absent. Remove the unshipped inputs and reload the manager before retrying.
Pending reloads require
`systemctl --user daemon-reload`; masked units require `systemctl --user unmask`.
For a partial graph, remove its partial five-unit installation, reload the manager,
then reinstall the complete pair. Foreign fragments must be removed before the
same reload/install sequence. The installer also enforces this admission policy.

For an explicitly source-only installation, use the locked production uv snapshot:

```bash
./scripts/install-user-services.sh --source
```

A paid Connect-enabled source snapshot also needs the approved public-key ring:

```bash
LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE=/secure/path/connect-public-keyring.json \
  ./scripts/install-user-services.sh --source
```

The source-only lane is exempt from native admission and does not establish paired
desktop safety. A desktop bundle embeds its authority at build time; do not pass a
keyring to its installer. Both modes consume the same shipped unit payload owner.
Source installation refuses to replace an existing native scheduled alias; use
the concrete desktop `--engine` installer to update that paired deployment.
Service cadence, sandbox, ntfy delivery and attachment confirmation remain as before.
Logs contain message IDs and sanitized failure classes, not bodies or credentials.
Post-send reservation recovery after a deployment refusal is tracked in
[#219](https://github.com/canfieldjuan/eom-email-watcher/issues/219); do not resend an
uncertain delivery before manual reconciliation.

## Model output and safety

The model must return a validated JSON object with category, priority, summary, action flag,
suggested action, deadline text/date, and confidence. The prompt treats the entire email as
untrusted data and denies instructions embedded in it. A model-proposed deadline before the
message date is removed while its original deadline text remains available for human review.

The local model has no mailbox tools and cannot send, delete, label, archive, or reply to mail. The
watcher requests no mailbox write scope.

## Monthly Firefly hours request

The optional outbound job uses a separate OAuth token with only `gmail.send`. It does not widen the
email watcher's read-only token. Configure `monthly_hours_recipient` privately, then authorize and
install the timers:

```bash
uv run eom-mail-watch setup-send
uv run eom-mail-watch send-hours --dry-run
./scripts/install-user-services.sh --source
systemctl --user start eom-monthly-hours.timer
```

The timer sends at 9:00 AM America/Chicago on the first of every month and automatically requests
the previous month's Firefly hours. SQLite duplicate protection prevents more than one production
send for a billing month. `--test-to address@example.com` sends a clearly marked test and does not
consume the production duplicate key.

If a production send is left `reserved` or `ambiguous`, do not rerun it blindly. Inspect the exact
dedupe key printed by the error, then independently check Gmail Sent before choosing a resolution:

```bash
uv run eom-mail-watch outbound-status monthly-hours:2026-07
uv run eom-mail-watch outbound-resolve monthly-hours:2026-07 --confirm-sent GMAIL_MESSAGE_ID
uv run eom-mail-watch outbound-resolve monthly-hours:2026-07 --confirm-unsent
```

These reconciliation commands never contact Gmail or send mail. `--confirm-sent` records the Gmail
message ID and preserves the duplicate block. Use `--confirm-unsent` only after proving no message
was accepted; it releases the reservation so a later timer or manual `send-hours` run may send.
Production sends and mutating reconciliation refuse to overlap on this machine. Stop the monthly
timer first if later eligibility would still be unsafe during reconciliation.

## Development

```bash
uv run ruff check .
uv run pytest --cov=eom_email_watcher --cov-report=term-missing
```

The synthetic, local-only CPU model benchmark and its privacy boundary are documented in
[`docs/MODEL_BENCHMARK.md`](docs/MODEL_BENCHMARK.md). Benchmarking is a developer/operator tool;
it does not run during normal mail checks.

The pinned mainline llama.cpp request-shape and bounded-concurrency proof is documented in
[`docs/LLAMA_CPP_COMPATIBILITY.md`](docs/LLAMA_CPP_COMPATIBILITY.md). It records compatibility and
the required reasoning setting; it does not switch the current production runtime.

Desktop checks are documented with the desktop proof because they require the Rust/Node/WebKit
toolchain in addition to Python.
