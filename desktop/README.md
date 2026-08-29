# Tauri desktop proof

The Linux desktop proof exposes a read-only Inbox backed by the existing SQLite message ledger,
the useful watchlist path, and a live Health view. Health can inspect Gmail, local AI, database,
and notification readiness, then run one production-safe `Check now` through the existing
versioned Python engine contract. The host drains a bounded batch of durable notification intents
through Tauri's native notification plugin on startup and after `Check now`. On compatible hosts, a
single-instance process also polls automatically using `poll_interval_minutes` (120 minutes by
default) and publishes the next scheduled check in Health. Polling remains disabled when production
locking or host-owned notification delivery is unsupported by the current configuration.

The host acknowledges an intent only after the platform notification API accepts it. Failed or
interrupted delivery remains queued, does not block later intents in the bounded batch, and may be
retried by `Check now` even when the Gmail check itself fails. The existing at-least-once duplicate
window remains between platform acceptance and durable acknowledgement. It does **not** yet own
tray/autostart behavior, Gmail OAuth, settings mutation, or packaging. The existing systemd watcher
remains the production path while equivalent live behavior is evaluated; the production check lock
continues to fail closed if both schedulers overlap. Scheduled engine processes have a 30-minute
upper bound, and wall-clock deadline checks catch up after system resume.

## Development prerequisites

- the repository's Python 3.13/uv environment (`uv sync --locked --all-groups` at the repo root)
- Node.js 26 and pnpm 11
- current stable Rust with Cargo
- Tauri's Linux WebKitGTK prerequisites
- an existing watcher config at `~/.config/eom-email-watcher/config.toml`

Install frontend dependencies and run the app:

```bash
cd desktop
pnpm install --frozen-lockfile
pnpm tauri dev
```

The host runs `uv run --project <repository> eom-mail-engine` directly with structured arguments
and sends one JSON request over stdin. It never invokes a shell or exposes the config path to the
frontend.

For an isolated development config, set `EOM_EMAIL_WATCHER_CONFIG` to its path before starting the
app. `EOM_EMAIL_ENGINE_BIN` may point to an already-installed `eom-mail-engine` executable; when it
is absent, the repository/uv runner above is used. These are trusted process-environment settings,
not frontend inputs.

## Verification

```bash
pnpm build
cargo fmt --manifest-path src-tauri/Cargo.toml --check
cargo clippy --manifest-path src-tauri/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path src-tauri/Cargo.toml
pnpm tauri build --no-bundle
```

The Rust test performs real health, inactive-check, and add/list/remove calls through
`eom-mail-engine` using a temporary zero-sender config. The inactive check proves the host contract
without contacting Gmail. The production build intentionally disables installer bundling; runtime
and installer packaging belong to the distribution slice.

The icon is a temporary text-free engineering asset required by Tauri's Unix build. It is not a
final product-brand decision.
