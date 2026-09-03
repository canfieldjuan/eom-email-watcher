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
   effective-DACL, fixed `.<destination-filename>.tmp` atomic replacement with
   safe stale-temp reclamation and bounded sharing-violation retry, and
   non-blocking Windows lock primitives under the existing same-user trust
   model. Validate every installed-file ancestor and the file ACL itself while
   treating OWNER RIGHTS as the already-validated concrete owner. The shared
   lock range is byte offset `0`, length `1`.
3. Use those primitives for registration reads and entitlement status/install,
   including the contract-wide per-directory traversal limit of 256 direct
   children before case-insensitive `.json` filtering, commit-time revalidation,
   rollback, and propagation of an explicitly selected discovery root through
   each registration-file admission check.
4. Remove the packaging veto, use the platform data-file separator, and embed
   the production public keyring in the native Windows sidecar only after the
   bounded build-input reader rejects symlink/reparse substitution and binds
   the read to one stable file identity. Stage the returned validated bytes
   directly so a later source-path replacement cannot change package authority,
   refuse fixture/test key IDs in release packages, and make standalone smoke
   invocations declare the already-built binary's expected authority state.
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

- Both native Windows jobs are pinned to accepted `connect-contracts` commit
  `3005d82a7be885fba36f8688b5967a5b56a0abea`.
- Existing Linux Connect, entitlement, engine, and packaging tests remain green.
- Native Windows tests prove default paths, bounded reads, registration
  discovery and enumeration, replacement after a short-lived reader releases
  its handle, source-vs-installed entitlement ACL boundaries, activation,
  contention, rollback, OWNER RIGHTS admission, explicit v1/v2 runtime-root
  discovery, reparse refusal, safe fixed-temp crash recovery, unsafe stale-temp
  refusal, and the exact 256/257 direct-child boundary including non-candidate
  temp/lock names, case-insensitive names, and non-file `.json` entries.
- Desktop packaging tests prove an ordinary public keyring is accepted while
  Windows reparse metadata fails closed before that authority can be bundled;
  replacing the source after validation still stages the exact validated bytes,
  and a non-production key ID is rejected.
- The Windows NSIS job builds with the production public keyring and the
  packaged sidecar reports `missing`, not `authority_unavailable`, before a
  license is installed.
- The local Windows VM installs the production-signed entitlement, discovers the
  packaged Website Redesign capability, and completes one authenticated
  deterministic job.

## Verification results

- GitHub Actions run `33791640819` at consumer commit
  `0dfb8139296600532e8c7256aee02e08e33591eb` completed successfully across the
  Linux test, desktop, Windows operation-lock, and Windows package jobs.
- Windows package job `100769381640` ran all 37 desktop-packaging tests
  successfully, built the NSIS installer, passed two packaged engine
  process-tree lifecycle tests, and passed its repeated packaged-engine smoke.
- The consumer installer used for local acceptance had SHA-256
  `e0f4ad534cdefe50f5a9552b81ef982908ccc1265827b43b6bd13f346e36cf43`.
  It was built from commit `0dfb8139296600532e8c7256aee02e08e33591eb`;
  the subsequent fixed-temp recovery correction is covered by the native
  Windows test rather than this earlier cross-app artifact.
- A local Windows 11 VM installed that package and ran its installed
  `eom-mail-engine.exe`. The engine discovered authenticated provider instance
  `dab2c8a9-5375-490d-9b5f-fc6e906ad288`, observed an active
  production-signed entitlement, and reconciled completed job
  `70056a8f-a173-4ef0-a3bd-67165f12ca30`.
- The provider result and consumer-exported `test-business-homepage.html` were
  both 63,616 bytes with SHA-256
  `97920ed3503ded39ab70e4809af4ba60a3dc3bd6af2eb622302f6cdb1ec500e3`, proving
  discovery, bearer-authenticated job access, and integrity-bound output
  reconciliation across the two packaged applications.
- The acceptance harness initially assumed a wildcard sidecar name, while the
  installer correctly uses the fixed contract name `eom-mail-engine.exe`.
  Optical-media copying also preserved a read-only attribute on the seed
  SQLite file; that media attribute was cleared before the successful engine
  run. The corrected final media contains both harness fixes, but the proof did
  not destructively reset the already-installed VM proof root for a clean
  replay.

## Deferred

Named pipes, same-user hostile-process attestation, stale-file scavenging,
general hardening, code signing, updater work, installer redesign, remote model
access, and unrelated product polish remain deferred.
