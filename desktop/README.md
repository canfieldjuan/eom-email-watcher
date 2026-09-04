# Tauri desktop proof

The Linux desktop proof exposes a local Inbox backed by the existing SQLite message ledger,
the useful watchlist path, and a live Health view. Health can inspect mail accounts, local AI,
database, and notification readiness, then run one production-safe `Check now` through the existing
versioned Python engine contract. The host drains a bounded batch of durable notification intents
through Tauri's native notification plugin on startup and after `Check now`. On compatible hosts, a
single-instance process also polls automatically using `poll_interval_minutes` (120 minutes by
default) and publishes the next scheduled check in Health. Polling remains disabled when production
locking or host-owned notification delivery is unsupported by the current configuration.
Closing the main window hides it while the host keeps automatic polling and notification delivery
alive. The system-tray menu restores the window or explicitly quits the host. An opt-in Settings
control registers the installed application to start after login; that launch stays hidden in the
tray until the user opens it.

Settings can safely update polling cadence, message retention, and native-notification enablement
through the engine contract without exposing TOML or secret-bearing fields to the frontend. For
exact-loopback inference, the same UI can update the HTTP endpoint and model identifier. A gateway
endpoint and model are read-only, while gateway trust and authentication credentials remain managed
externally. A new polling cadence takes effect after the app restarts; model and notification
changes are read by later watcher operations, while a retention change immediately removes local
history older than the new source-time cutoff.

Inbox cards can delete one message from local history, and the Inbox can clear all local history,
only after native confirmation. These operations remove local analysis, notification state,
attachment metadata, and capability jobs/results. They never call a mail provider or delete source
email, and they preserve the mailbox sync cursor and the separate EOM outbound-send ledger.

Inbox cards show ordered attachment filenames, media types, and byte sizes from the durable message
ledger. Open fetches only the selected attachment through the read-only source-mail adapter, writes it
to a mode-0600 file in a process-owned temporary directory, and asks the operating system to open it with
the default application. The host attempts to remove the temporary directory on an orderly app exit.
Its path is not returned to frontend JavaScript.

For compatible PDF attachments, the same card shows a provider-neutral Summarize action only while
the paid Connect entitlement is active and exactly one authenticated local `document.summarize`
v1.0 capability is discoverable. The frontend knows the capability and media type, not a provider
app ID or license contents. The Python engine rechecks entitlement and discovery before submission,
fetches only the selected source attachment, persists job state and the verified summary, and never
sends mail metadata or credentials to the provider. Inbox loading succeeds even when Connect
discovery fails.

Health includes a separate Connect card with claim-free active/missing/invalid/future/expired/
feature-missing status. **Activate** selects an acquired JSON license; the picker is only the
consent surface. The Python engine enforces the compiled issuer authority, bounded non-symlink
source read, fixed shared destination, owner-private directory and files, cross-app non-blocking
lock, exact-byte synced temporary write, atomic replacement, directory sync, and final
re-evaluation. It does not read watcher configuration, Gmail credentials, or private mailbox state
for these app-local operations. A failed replacement re-reads authoritative status so an existing
active license is not presented as unavailable. The card also refreshes on focus, visibility
restoration, and a bounded timer because another installed Connect app may change the shared
entitlement.

The host acknowledges an intent only after the platform notification API accepts it. Failed or
interrupted delivery remains queued, does not block later intents in the bounded batch, and may be
retried by `Check now` even when the mailbox check itself fails. The existing at-least-once duplicate
window remains between platform acceptance and durable acknowledgement. When a Google or Microsoft
OAuth desktop-client identity is configured externally or injected into the release build, Health
can run the provider's read-only browser authorization flow and initialize the current-mailbox
baseline. The existing systemd
watcher remains the production polling path while equivalent live behavior is evaluated; the
production check lock continues to fail closed if both schedulers overlap. Scheduled engine
processes have a 30-minute upper bound, and wall-clock deadline checks catch up after system resume.

## Development prerequisites

- the repository's Python 3.13/uv environment (`uv sync --locked --all-groups` at the repo root)
- Node.js 26 and pnpm 11
- current stable Rust with Cargo
- Tauri's Linux WebKitGTK prerequisites, including `libayatana-appindicator3-dev` for tray builds
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

Start on login registers the currently running executable. Enable it from the installed application,
not from `pnpm tauri dev`, so the operating system does not retain a development-build path.

## Packaged applications

Build the Linux application and bundled engine from the repository root environment:

```bash
cd desktop
pnpm tauri build --bundles deb
```

The Debian package contains both `eom-email-watcher-desktop` and `eom-mail-engine`. The bundled
engine is a PyInstaller one-file executable built by the cross-platform
`scripts/build_desktop_sidecar.py`; `scripts/build-desktop-sidecar.sh` remains the Linux convenience
entrypoint. Installed runtime does not require the source checkout, Python, or `uv`. The sidecar
carries its own IANA timezone data so its behavior does not depend on the build interpreter's
filesystem paths. Every sidecar build runs an isolated first-use configuration smoke with
`America/Chicago` and fails before packaging if the frozen engine cannot resolve that timezone.

On Windows, build the NSIS installer on a Windows host:

```powershell
cd desktop
pnpm tauri build --bundles nsis
```

The Windows build emits the Tauri-required `eom-mail-engine-<target-triple>.exe`, runs the same
isolated first-run/health/inactive-check smoke used by Linux builds, and bundles it into the setup
executable. PyInstaller is not a cross-compiler, so a Linux build cannot prove the Windows sidecar.
The `windows-package` GitHub job is the canonical Windows acceptance path and uploads the unsigned
NSIS installer for inspection. Code signing and public release publication remain separate release
work.

An approved Google Desktop OAuth client can be injected into the sidecar at release build time
without committing it:

```bash
EOM_EMAIL_WATCHER_GOOGLE_OAUTH_CLIENT_FILE=/secure/path/desktop-client.json \
  pnpm tauri build --bundles deb
```

The same environment variable is supported by Windows builds. The build accepts only a downloaded
Desktop-client JSON shape and rejects files containing access or refresh-token fields. A configured
operator credential file remains authoritative; otherwise
the packaged client identity is used. Account tokens are still created and stored only in the
user's private local state. Atlas token-store files are not valid build inputs because they contain
account grants rather than only the reusable Desktop client identity. A build without this variable
remains suitable for development but requires the existing external credentials file before Gmail
can connect.

An approved Microsoft Entra public desktop-client identity can be embedded independently:

```bash
EOM_EMAIL_WATCHER_MICROSOFT_OAUTH_CLIENT_FILE=/secure/path/microsoft-public-client.json \
  pnpm tauri build --bundles deb
```

The JSON contains only `client_id` and `tenant` (`organizations` or a tenant UUID). The build rejects
client secrets, account tokens, invalid identifiers, and consumer/common tenant selectors. The Entra
registration must allow public-client `http://localhost` redirect and delegated Graph `Mail.Read`.
User grants are created only by the installed app and remain in its private per-account state. A
build without this variable can still connect Microsoft 365 when the external
`microsoft_credentials_file` is configured.

An official Connect-enabled sidecar must also embed the production issuer public-key ring:

```bash
LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE=/secure/path/connect-public-keyring.json \
  pnpm tauri build --bundles deb
```

The key ring contains public verification keys only. Private signing keys must never be supplied
to the build or committed. The release builder also requires the parsed issuer ID and public key to
match the authority explicitly approved in the builder; a merely production-shaped replacement is
rejected even if a filesystem path is raced. A build without this variable remains a healthy
standalone Email Watcher but Connect capability discovery and invocation fail closed. This variable
is consumed by the release build script; there is no runtime environment override for issuer trust,
and such a build reports `authority_unavailable` instead of admitting license installation.

The same build input is supported by the native Windows sidecar. PyInstaller receives data-file
arguments using the host platform separator, and the Windows package reads the shared entitlement
from `%LOCALAPPDATA%\LocalConnect\entitlement-v1.json`. Provider registrations are discovered from
`%LOCALAPPDATA%\LocalConnect\runtime\v1|v2\providers`. The runtime verifies that the root and every
trusted descendant have no content or mutation grant beyond the user, SYSTEM, or built-in
Administrators; no XDG variables are required on Windows.

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
proves the host contract without contacting a mail provider. `scripts/connect-local-proof.py` is the explicit
cross-process proof harness: it uses a real Document Summarizer provider process and real Email
Watcher persistence with synthetic Gmail attachment bytes. Its default mode starts a deterministic
local fixture model that satisfies the provider's current structured evidence, synthesis, and
verification schemas:

```bash
.venv/bin/python scripts/connect-local-proof.py \
  --provider-binary /path/to/document-summarizer \
  --pdf /path/to/structured-report.pdf \
  --entitlement-keyring /path/to/test-keyring.json \
  --active-entitlement /path/to/active-entitlement.json \
  --expired-entitlement /path/to/expired-entitlement.json
```

The proof uses attachment-scoped v2 discovery and explicit provider/capability selection, persists a
stable request identity and generic output, stops the provider, replays the completed job while it is
offline, and verifies the Inbox remains healthy. It also replaces the shared signed entitlement with
an expired one, proves both consumer discovery and the provider manifest deny capability exchange,
replays the already completed result without Gmail access, restores the active entitlement without
restarting either app, and observes the capability return. A deterministic reference provider then advertises
the same summary capability plus `document.translate` and `document.inspect`. The proof observes two
summary-provider choices, invokes both non-summary capabilities through the same generic contract,
persists their provenance and parameters into the Inbox, presents bounded text natively, keeps an
unknown output opaque until private export, and removes the reference provider without disrupting
Document Summarizer or the Inbox. It also rejects a stale capability version before Gmail access,
durable job creation, or provider submission. It then restarts Document Summarizer
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

When `scripts/smoke_packaged_engine.py` is run independently of the build, pass the authority state
embedded in that already-built binary explicitly: `--expected-entitlement-state missing` for a
Connect-enabled package or `--expected-entitlement-state authority_unavailable` for a standalone
package. The smoke test does not infer package contents from the caller's current environment.

### Packaged Debian interoperability proof

After building both Debian packages from their current checkouts with the canonical production
public-key ring, exercise the two shipped binaries together. Set
`LOCAL_CONNECT_ENTITLEMENT_KEYRING_FILE` to
`connect-contracts/entitlements/v1/release/keyring.json` for both builds, then supply an acquired
active entitlement signed by that authority. Fixture/test key IDs are intentionally rejected by the
release builders; the source-level proof above remains the place for fixture authority.

```bash
uv run python scripts/connect-packaged-deb-proof.py \
  --consumer-deb "desktop/src-tauri/target/release/bundle/deb/Email Watcher_0.1.0_amd64.deb" \
  --provider-deb "/path/to/Document Summarizer_0.1.0_amd64.deb" \
  --active-entitlement "/secure/path/production-entitlement-v1.json"
```

The harness requires `dpkg-deb`, `dbus-run-session`, and `xvfb-run`. It extracts rather than
system-installs the packages, starts the packaged provider in an isolated desktop session, and
drives the packaged `eom-mail-engine` JSON contract. Every config, database, entitlement, runtime,
and application-data path is confined to a private temporary root; the parent shell's home and
credential environment are not forwarded. The source `Store` is used only to create one synthetic
PDF attachment-metadata row—no message body or attachment bytes are persisted or handed off.

A passing result proves generic v2 capability visibility changes `0 -> 1 -> 0 -> 1` across packaged
provider launch, stop, and restart, the provider's durable instance identity survives restart, and
the packaged consumer Inbox remains readable while the provider is absent. It does not contact
Gmail or a model, submit a capability job, install either package through the OS package manager, or
exercise a human UI click. The source-level `connect-local-proof.py` remains the job handoff,
idempotency, persistence, output, and privacy proof. Native Windows execution remains separate: the
`windows-package` job embeds the release public key ring, runs native
placement/activation/discovery probes, builds the Email Watcher installer, and requires the
packaged engine to report `missing` rather than `authority_unavailable`. The two-app job handoff is
the remaining release-acceptance step for an actual Windows test session.

The icon is a temporary text-free engineering asset required by Tauri's Unix build. It is not a
final product-brand decision.
