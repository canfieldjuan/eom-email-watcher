# Tauri desktop proof

The Linux desktop proof exposes a read-only Inbox backed by the existing SQLite message ledger,
the useful watchlist path, and a live Health view. Health can inspect Gmail, local AI, database,
and notification readiness, then run one production-safe `Check now` through the existing
versioned Python engine contract. The host drains a bounded batch of durable notification intents
through Tauri's native notification plugin on startup and after `Check now`. On compatible hosts, a
single-instance process also polls automatically using `poll_interval_minutes` (120 minutes by
default) and publishes the next scheduled check in Health. Polling remains disabled when production
locking or host-owned notification delivery is unsupported by the current configuration.

Settings can safely update polling cadence, message retention, and native-notification enablement
through the engine contract without exposing TOML or secret-bearing fields to the frontend. A new
polling cadence takes effect after the app restarts; retention and notification changes are read by
later watcher operations. Model/gateway settings and credentials remain outside this UI.

Inbox cards show ordered attachment filenames, media types, and byte sizes from the durable message
ledger. Open fetches only the selected attachment through the read-only Gmail engine, writes it to a
mode-0600 file in a process-owned temporary directory, and asks the operating system to open it with
the default application. The host attempts to remove the temporary directory on an orderly app exit.
Its path is not returned to frontend JavaScript.

For compatible PDF attachments, the same card shows a provider-neutral Summarize action only while
exactly one authenticated local `document.summarize` v1.0 capability is discoverable. The frontend
knows the capability and media type, not a provider app ID. The Python engine re-discovers before
submission, fetches only the selected Gmail attachment, persists job state and the verified summary,
and never sends mail metadata or credentials to the provider. Inbox loading succeeds even when
Connect discovery fails.

The host acknowledges an intent only after the platform notification API accepts it. Failed or
interrupted delivery remains queued, does not block later intents in the bounded batch, and may be
retried by `Check now` even when the Gmail check itself fails. The existing at-least-once duplicate
window remains between platform acceptance and durable acknowledgement. It does **not** yet own
tray/autostart behavior or public Google OAuth verification. When an OAuth desktop-client identity
is configured externally or injected into the release build, Health can run the read-only Gmail
browser authorization flow and initialize the current-mailbox baseline. The existing systemd
watcher remains the production polling path while equivalent live behavior is evaluated; the
production check lock continues to fail closed if both schedulers overlap. Scheduled engine
processes have a 30-minute upper bound, and wall-clock deadline checks catch up after system resume.

## Development prerequisites

- the repository's Python 3.13/uv environment (`uv sync --locked --all-groups` at the repo root)
- Node.js 26 and pnpm 11
- current stable Rust with Cargo
- Tauri's Linux WebKitGTK prerequisites
- a compatible local OpenAI-style model endpoint

Install frontend dependencies and run the app:

```bash
cd desktop
pnpm install --frozen-lockfile
pnpm tauri dev
```

In development, the host builds and prefers the target-triple-named Tauri sidecar, sends one JSON
request over stdin, and reads one JSON response. It never invokes a shell or exposes the config path
to the frontend. If no packaged sidecar is available, development may fall back to
`uv run --project <repository> eom-mail-engine`.

For an isolated development config, set `EOM_EMAIL_WATCHER_CONFIG` to its path before starting the
app. `EOM_EMAIL_ENGINE_BIN` may point to an already-installed `eom-mail-engine` executable; when it
is absent, the packaged sidecar and then the repository/uv development runner are tried in that
order. These are trusted process-environment settings, not frontend inputs.

## Debian package

Build the Linux application and bundled engine from the repository root environment:

```bash
cd desktop
pnpm tauri build --bundles deb
```

The package contains both `eom-email-watcher-desktop` and `eom-mail-engine`. The bundled engine is a
PyInstaller one-file executable built by `scripts/build-desktop-sidecar.sh`; installed runtime does
not require the source checkout, Python, or `uv`.

An approved Google Desktop OAuth client can be injected into the sidecar at release build time
without committing it:

```bash
EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE=/secure/path/desktop-client.json \
  pnpm tauri build --bundles deb
```

The build accepts only a downloaded Desktop-client JSON shape and rejects files containing access
or refresh-token fields. A configured operator credential file remains authoritative; otherwise
the packaged client identity is used. Account tokens are still created and stored only in the
user's private local state. Atlas token-store files are not valid build inputs because they contain
account grants rather than only the reusable Desktop client identity. A build without this variable
remains suitable for development but requires the existing external credentials file before Gmail
can connect.

## Verification

```bash
pnpm build
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml --all-targets
pnpm tauri build --bundles deb
```

The Rust tests perform real health, inactive-check, add/list/remove, capability-discovery, and
summary-command contract calls through `eom-mail-engine` using isolated config. The inactive check
proves the host contract without contacting Gmail. `scripts/connect-local-proof.py` is the explicit
cross-process proof harness: it uses a real Document Summarizer provider process and real Email
Watcher persistence with synthetic Gmail attachment bytes. Its default mode starts a deterministic
local fixture model that satisfies the provider's current structured evidence, synthesis, and
verification schemas:

```bash
.venv/bin/python scripts/connect-local-proof.py \
  --provider-binary /path/to/document-summarizer \
  --pdf /path/to/structured-report.pdf
```

The proof uses attachment-scoped v2 discovery and explicit provider/capability selection, persists a
stable request identity and generic output, stops the provider, replays the completed job while it is
offline, and verifies the Inbox remains healthy. A deterministic reference provider then advertises
the same summary capability plus `document.translate` and `document.inspect`. The proof observes two
summary-provider choices, invokes both non-summary capabilities through the same generic contract,
persists their provenance and parameters into the Inbox, presents bounded text natively, keeps an
unknown output opaque until private export, and removes the reference provider without disrupting
Document Summarizer or the Inbox. It then restarts Document Summarizer
under the same durable v2 identity, interrupts a job after provider acceptance, restarts again, and
reconciles the same request to the provider's authoritative `PROVIDER_RESTARTED` failure without
reopening Gmail or resubmitting. Any false lifecycle, privacy, persistence, or provenance predicate
exits nonzero. Its single JSON result contains booleans/counts and a summary digest rather than
document text or credentials. Supplying `--model-base-url` and `--model-name` exercises that
exact-loopback model for the completed-output path; the deterministic interruption path still uses
the bounded fixture model so it can pause safely. An authenticated endpoint may additionally use
`--model-api-token-file`.
Both configured-model fields are required together, and the provider retains final endpoint
validation. Neither mode claims a live Gmail OAuth or human UI-click test.

The icon is a temporary text-free engineering asset required by Tauri's Unix build. It is not a
final product-brand decision.
