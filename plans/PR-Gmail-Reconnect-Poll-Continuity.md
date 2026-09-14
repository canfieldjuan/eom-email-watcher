# Bind Gmail credential identity during authorization

## Why this slice exists

At `381f8f051a7936c595ed53ea46589906f4e95e5a`, installed Gmail reconnect
succeeded but the next dry check returned `mailbox_identity_changed`, blocking
the Linux installed proof for issue #139 and the first public release.

### Problem-derived contract

- **Root cause:** Gmail authorization can initialize a cursor without reconciling
  its credential identity into `mail_accounts`, so the next dry poll rejects a
  null or stale account identity.
- **Correct fix must touch/change:** bind successful Gmail authorization; retain
  the cursor for first binding of a valid token; reset it for an explicit
  credential replacement; prove the next `watcher.check` succeeds.
- **Must not change:** OAuth scopes, staged token installation, address matching,
  Microsoft/IMAP identity, notifications, model, Connect, schemas, or responses.

### Contract revision

The live account key was null and `mailbox_state` has no identity column. The
initial preservation idea contradicted the credential-epoch contract: a replaced
refresh token requires a new identity and baseline, while runtime stays unchanged.

## Scope (this PR)

Ownership lane: Email Watcher first-release installed Linux proof
Slice phase: first-release blocker found by installed primary-flow demonstration

1. Reconcile Gmail's identity before a successful authorization response.
2. Preserve a cursor only for first binding of an already-valid credential.
3. Reset the baseline when browser reauthorization replaces the credential.
4. Add fail-first coverage for reconnect followed by polling admission.

### Review Contract

Acceptance criteria:

1. Same-address reauthorization advances identity and baseline, then public
   `watcher.check` succeeds.
2. Existing-token authorization binds without changing its retained cursor.
3. Fresh Gmail connection stores a non-null identity before success.
4. Different-address reconnect installs neither replacement token nor identity.
5. Microsoft 365 and IMAP completion and runtime identity paths remain unchanged.
6. The focused engine tests, full repository suite, Ruff, and diff check pass.
7. A rebuilt unsigned Debian package smokes, installs, and reaches Gmail polling.

Reachability proof: the regression calls the public
`mail.accounts.reconnect` operation and then the public `watcher.check`
operation against the same persisted store; the installed proof repeats the
check through `/usr/bin/eom-mail-engine`.

Affected surfaces: Gmail authorization completion, Gmail credential-epoch
binding, baseline selection, and engine API regression tests.

Risk areas: accepting a different mailbox, retaining an old credential
namespace, resetting a cursor when the credential did not change, masking
Microsoft/IMAP identity changes, or exposing credential-derived identifiers.

Reviewer rules triggered: R1, R2, R3, R5, R6, R8, R10, R13, and R14.

### Boundary-change enumeration

- Boundary path/seam: authenticated Gmail gateway identity ->
  `_finish_gmail_authorization` -> `mail_accounts.mailbox_identity_key` ->
  `load_mailbox_account` -> `reconcile_mailbox_session_identity`.
- Replaced-path behavior: successful Gmail authorization no longer returns with
  a null or stale account identity.
- Guard-relevant fields: valid existing vs replaced credential, bound vs unbound
  account identity, same vs different authenticated address, retained vs new
  baseline, and dry vs non-dry poll.
- Caller x input shape: compatibility authorization with a valid token, fresh
  connect, same-address reconnect with a rotated token, different-address
  reconnect, and the immediately following dry poll.


### Files touched

- `plans/PR-Gmail-Reconnect-Poll-Continuity.md`
- `src/eom_email_watcher/engine_api.py`
- `tests/test_engine_api.py`

## Mechanism

Both successful Gmail authorization paths pass the gateway's credential identity
to `_finish_gmail_authorization`. The helper reconciles a changed identity before
baseline completion. Reusing a valid installed credential preserves the cursor
while binding that credential for the first time. Browser authorization is an
explicit credential replacement, so a changed identity is recorded as
`replacement`; the store removes the previous baseline and the completion path
sets the authenticated Gmail profile's current history ID.

The existing normalized profile-address comparison still rejects a different
mailbox before the staged token is installed. Provider-neutral runtime loading,
Microsoft 365, and IMAP continue using their existing identity mechanisms.

## Intentional

- Gmail's mailbox key remains a one-way credential epoch derived from the
  refresh token; the refresh token itself is never persisted in the database or
  returned by the engine.
- A rotated Gmail refresh token creates a new message/rule identity namespace
  even when the normalized address is unchanged, matching the existing
  `AUTOMATE_RULE_ENGINE_CONTRACT.md` boundary.
- A valid installed token first observed after schema upgrade may bind without
  discarding its cursor because no credential replacement occurred.

## Deferred

Parking predicate: unrelated account migration, provider identity redesign,
OAuth UX, and release packaging changes remain parked unless they prevent this
same-account reconnect from reaching the installed primary flow.

Parked hardening: none.

## Verification

- Live fail-first installed proof at `381f8f051a7936c595ed53ea46589906f4e95e5a`:
  reconnect succeeded, then `watcher.check` returned
  `mailbox_identity_changed`.
- Automated fail-first regression:
  `uv run pytest -o addopts='' --tb=short tests/test_engine_api.py::test_gmail_reconnect_preserves_bound_identity_for_the_next_check`
  — `1 failed in 1.06s` before the production change.
- Replacement/rejection boundary tests:
  `uv run pytest -o addopts='' --tb=short tests/test_engine_api.py::test_gmail_reconnect_rebinds_rotated_identity_for_the_next_check tests/test_engine_api.py::test_mail_account_reconnect_rejects_different_identity_before_replacing_token`
  — `2 passed in 0.72s`.
- Existing-token/replacement crash boundary:
  `uv run pytest -o addopts='' --tb=short tests/test_engine_api.py::test_gmail_authorize_preserves_existing_baseline tests/test_engine_api.py::test_gmail_authorize_resets_baseline_for_an_already_installed_replacement tests/test_engine_api.py::test_gmail_reconnect_rebinds_rotated_identity_for_the_next_check tests/test_engine_api.py::test_mail_account_reconnect_rejects_different_identity_before_replacing_token`
  — `4 passed in 1.56s`.
- Full repository suite: `uv run pytest -o addopts='' --tb=short` —
  `1311 passed, 16 skipped in 86.27s`.
- Ruff: `uv run ruff check .` — `All checks passed!`.
- Diff check: `git diff --check` — passed with no output.
- Unsigned package build: `pnpm tauri build --bundles deb` — completed one
  `Email Watcher_0.1.0_amd64.deb` bundle; the embedded sidecar build reported
  `packaged-engine-smoke: ok`.
- Artifact SHA-256: `6f0abbd58f96f679c41113a21e8d590af843acd4b166f7152abd5ab47f18b978`;
  installed status: `install ok installed 0.1.0 amd64`.
- Installed engine smoke: `scripts/smoke_packaged_engine.py /usr/bin/eom-mail-engine`
  — `packaged-engine-smoke: ok`.
- Installed Gmail reachability: `gmail.authorize` reused the valid token with
  `baseline_initialized: false`; the following installed-engine
  `watcher.check` dry run exited `0`, returned `ok: true`, and reported an
  active watcher with zero discovered messages.

## Estimated diff size

| File | LOC |
|---|---:|
| `plans/PR-Gmail-Reconnect-Poll-Continuity.md` | 154 |
| `src/eom_email_watcher/engine_api.py` | 36 |
| `tests/test_engine_api.py` | 200 |
| **Total** | **390** |
