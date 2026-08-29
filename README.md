# EOM Email Watcher

A private, local-first Gmail watcher for Effingham Office Maids. It checks Gmail every two
hours, selects messages only from an exact sender allowlist, asks a local LM Studio model for
a short structured summary, and sends a Linux desktop notification (and, optionally, a phone
push via [ntfy](https://ntfy.sh)).

Email content is never sent to a cloud model. The Gmail grant is read-only, attachment content is
downloaded only when the user opens that attachment, non-matching message metadata is not stored,
and message bodies are discarded after each local inference request.

## Behavior

- Starts at the current Gmail `historyId`; setup does not backfill old mail.
- Polls Gmail History for `messageAdded` events in `INBOX`.
- Verifies the parsed `From` address against a case-insensitive exact allowlist in trusted config.
- Fetches message bodies only after a sender matches.
- Extracts `text/plain`, or text from HTML as a fallback, capped at 20,000 characters.
- Records attachment filenames, Gmail part/attachment IDs, media types, and byte sizes. An explicit
  Open action fetches only that attachment into a mode-0600 process-owned temporary file.
- Stores message metadata, attachment metadata, and model summaries in SQLite for 180 days. Bodies
  are never stored.
- Deduplicates by Gmail message ID.
- Recovers an expired History cursor with an exact-sender search beginning five minutes before the
  last successful check, then saves a fresh cursor.
- If LM Studio is unavailable or times out, sends one metadata-only fallback notification and
  retries the summary later with backoff.

## Requirements

- Python 3.13 and [`uv`](https://docs.astral.sh/uv/)
- LM Studio `llmster` with its API bound to `127.0.0.1`
- `notify-send` (normally provided by `libnotify-bin`)
- A Google Workspace or Gmail account and a Google Cloud Desktop OAuth client
- Optional: an [ntfy](https://ntfy.sh) topic for phone push notifications alongside the desktop
  one -- set `ntfy_topic`/`ntfy_url` in `config.toml` (see `config.example.toml`)

## Install

```bash
uv sync --locked --all-groups
mkdir -p ~/.config/eom-email-watcher ~/.local/state/eom-email-watcher
cp config.example.toml ~/.config/eom-email-watcher/config.toml
chmod 700 ~/.config/eom-email-watcher ~/.local/state/eom-email-watcher
chmod 600 ~/.config/eom-email-watcher/config.toml
```

Edit the private config with the real exact sender list and the LM Studio model identifier. Never
commit that config; the repository example intentionally contains placeholders.

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
install -m 600 /dev/null ~/.local/state/eom-email-watcher/lmstudio-api-token
# Edit the file and paste the token on one line; do not put it in config.toml.
```

The watcher refuses to send email text to LM Studio when authentication is required but this token
file is missing. LM Studio's CLI can start the server but token creation is currently a GUI action.

## Gmail read-only OAuth setup

1. In Google Cloud Console, create or select a project.
2. Enable the Gmail API.
3. Configure the OAuth consent screen as Internal for the Workspace organization when available.
4. Create an OAuth Client ID with application type **Desktop app**.
5. Download the client JSON to:
   `~/.local/state/eom-email-watcher/credentials.json`
6. Lock it down and authorize:

```bash
chmod 600 ~/.local/state/eom-email-watcher/credentials.json
uv run eom-mail-watch setup
```

The browser consent request asks only for
`https://www.googleapis.com/auth/gmail.readonly`. The token is stored mode `0600`. Setup saves the
current Gmail cursor, so only future arrivals are processed.

## Commands

```bash
uv run eom-mail-watch doctor
uv run eom-mail-watch check --dry-run
uv run eom-mail-watch check
uv run eom-mail-watch recent --limit 20
```

`--dry-run` does not advance the Gmail cursor, add database rows, or send real notifications.

Desktop hosts use the versioned one-shot JSON contract documented in
[`docs/ENGINE_API.md`](docs/ENGINE_API.md). The existing human CLI remains the Linux/systemd entry
point.

The Linux Tauri proof provides a read-only Inbox, GUI watchlist management, live health status, and
a safe one-shot `Check now` action. It still requires this repository's Python/uv environment and
an existing private config; see [`desktop/README.md`](desktop/README.md). It does not replace the
systemd scheduler or native notification delivery yet.

## Two-hour user timer

After Gmail setup succeeds:

```bash
./scripts/install-user-services.sh
systemctl --user start eom-email-watcher.timer
systemctl --user status eom-email-watcher.timer
journalctl --user -u eom-email-watcher.service --since today
```

The unit is a hardened one-shot service. Logs contain message IDs and sanitized failure classes,
not bodies, OAuth tokens, or model prompts.
Its local LM Studio dependency starts llmster and the API on `127.0.0.1:1234` when needed.

## Model output and safety

The model must return a validated JSON object with category, priority, summary, action flag,
suggested action, deadline text/date, and confidence. The prompt treats the entire email as
untrusted data and denies instructions embedded in it. A model-proposed deadline before the
message date is removed while its original deadline text remains available for human review.

The local model has no Gmail tools and cannot send, delete, label, archive, or reply to mail. This
project requests no Gmail write scope.

## Monthly Firefly hours request

The optional outbound job uses a separate OAuth token with only `gmail.send`. It does not widen the
email watcher's read-only token. Configure `monthly_hours_recipient` privately, then authorize and
install the timers:

```bash
uv run eom-mail-watch setup-send
uv run eom-mail-watch send-hours --dry-run
./scripts/install-user-services.sh
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

Desktop checks are documented with the desktop proof because they require the Rust/Node/WebKit
toolchain in addition to Python.
