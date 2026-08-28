# Tauri desktop proof

The Linux desktop proof exposes a read-only Inbox backed by the existing SQLite message ledger and
the useful watchlist path: list, add, and remove exact watched senders through the existing
versioned Python engine contract.

It does **not** yet own polling, native notifications, tray/single-instance behavior, Gmail OAuth,
settings mutation, or packaging. The existing systemd watcher remains the production path while
those capabilities are proven in later slices.

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

The Rust test performs a real add/list/remove round trip through `eom-mail-engine` using a temporary
zero-sender config. The production build intentionally disables installer bundling; runtime and
installer packaging belong to the distribution slice.

The icon is a temporary text-free engineering asset required by Tauri's Unix build. It is not a
final product-brand decision.
