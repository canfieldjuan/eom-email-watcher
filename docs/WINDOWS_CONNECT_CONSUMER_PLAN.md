# Windows Local Connect consumer slice

## Root cause

Email Watcher's Windows package deliberately excludes the Connect public
authority and its consumer code still assumes Unix ownership, mode bits,
`os.getuid()`, XDG discovery, and `fcntl` entitlement activation. A Windows
Website Redesign provider could therefore run correctly while Email Watcher
silently reports no capability.

The root cause is one incomplete platform boundary shared by discovery,
entitlement storage, activation locking, and package construction—not a missing
button or manifest field.

## Correct fix

1. Derive default Windows discovery and entitlement locations from the accepted
   `%LOCALAPPDATA%\LocalConnect` contract while preserving explicit test roots
   and all Unix behavior.
2. Add protected private-DACL creation plus bounded regular-file, reparse-point,
   effective-DACL, delete-sharing reads, atomic-replacement, and non-blocking
   Windows lock primitives under the existing same-user trust model. The shared
   lock range is byte offset `0`, length `1`.
3. Use those primitives for registration reads and entitlement status/install,
   including bounded Windows registration enumeration, commit-time revalidation,
   and rollback.
4. Remove the packaging veto, use the platform data-file separator, and embed
   the production public keyring in the native Windows sidecar.
5. Exercise Windows paths and packaged authority in the existing Windows CI,
   then prove packaged provider discovery and one authenticated job on the local
   Windows VM.

## Must not change

- Connect v1/v2 schemas, transport kinds, routes, bearer-token handling,
  capability selection, job persistence, output semantics, or UI labels.
- Gmail/Microsoft credentials, mail polling, notifications, model analysis,
  private databases, or standalone behavior.
- Website Generator implementation, Document Summarizer, billing, named pipes,
  a broker, auto-launch, code signing, installer UX, or macOS placement.

## Expected files

- `.github/workflows/ci.yml`
- `README.md`
- `desktop/README.md`
- `docs/CONNECT_V1.md`
- `scripts/build_desktop_sidecar.py`
- `scripts/smoke_packaged_engine.py`
- `src/eom_email_watcher/connect.py`
- `src/eom_email_watcher/connect_windows.py`
- `src/eom_email_watcher/entitlement.py`
- `tests/test_connect.py` (existing Linux regression coverage; no code change expected)
- `tests/test_desktop_packaging.py`
- `tests/test_entitlement.py` (existing Linux regression coverage; no code change expected)
- `tests/test_windows_connect.py`
- `docs/WINDOWS_CONNECT_CONSUMER_PLAN.md`

## Verification contract

- Existing Linux Connect, entitlement, engine, and packaging tests remain green.
- Native Windows tests prove default paths, bounded reads, registration
  discovery and enumeration, replacement during an active reader, activation,
  contention, rollback, and reparse refusal.
- The Windows NSIS job builds with the production public keyring and the
  packaged sidecar reports `missing`, not `authority_unavailable`, before a
  license is installed.
- The local Windows VM installs the production-signed entitlement, discovers the
  packaged Website Redesign capability, and completes one authenticated
  deterministic job.

## Deferred

Named pipes, same-user hostile-process attestation, stale-file scavenging,
general hardening, code signing, updater work, installer redesign, remote model
access, and unrelated product polish remain deferred.
