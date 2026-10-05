# Automations Entitlement on Rule Changes

## Why this slice exists

Issue #143, narrowed on 2026-10-05: automation rules can be created, edited, and enabled without the Automations entitlement. Only dispatch checks it. Since #193, this is reachable from the desktop:

- The Expiry Ledger's COI setup can create and enable rules (`desktop/src/coiSetup.ts:193-205`).
- Its provider list comes from `connect.catalog`, which requires the Connect entitlement (`discover_capabilities(require_entitlement=True)`, `src/eom_email_watcher/engine_api.py:2901-2903`).
- So a user with Connect but without Automations can author COI rules. Through the raw engine API, any user can.
- Those rules' fires then park in `entitlement_paused`. The paid boundary holds at run time but not at authoring, and the user is never told the rule will not run.

The rule-engine contract left this open: "Entitlement timing and whether an unlicensed match may later dispatch" (`docs/AUTOMATE_RULE_ENGINE_CONTRACT.md:866`). On 2026-10-05 the operator chose option 1, "Require the Automations license to create, edit or enable a rule; deleting and disabling stay allowed", answering "1".

### Problem-derived contract

- Root cause: `_with_automation_rule_mutation` (`engine_api.py:2291-2306`) checks only operation-lock support. `automation.rules.put` (`:2374`, which `put_watched` reuses), `delete` (`:2434`), and `set_enabled` (`:2455`) never consult `_automation_entitlement_active` (`:1484`, which requires both the Connect and Automations features).
- Correct fix must touch:
  - an authoritative engine check on create, edit, and enable;
  - a way for the desktop to know the entitlement state before a write fails;
  - the COI setup controls;
  - the contract and API docs.
- Must not change:
  - dispatch gating and `entitlement_paused` behavior;
  - rule grammar, validation, versioning (CAS), and lock behavior;
  - `list`, `get`, `prepare`, and `delete` availability;
  - existing rules when an entitlement lapses (they are not modified or deleted).

## Scope (this PR)

Ownership lane: automation-rule-entitlement
Slice phase: paid-boundary enforcement (operator decision, 2026-10-05)

### Observable behavior

Engine:

1. `automation.rules.put`, for create and edit, and `automation.rules.put_watched` return `automation_entitlement_required` ("Automation rules require an active Automations entitlement") unless `_automation_entitlement_active()` is true. The check runs before the mutation lock is taken and before runtime access, so a refused call writes nothing.
2. `automation.rules.set_enabled` with `enabled: true` has the same requirement. `enabled: false` is always allowed.
3. `automation.rules.delete`, `list`, `get`, and `prepare` require no entitlement. Disabling and deleting stay possible after a license lapses, matching calendar revocation (`engine_api.py:1583`).
4. `connect.entitlement.status` adds `automations_active: bool`, the current value of `_automation_entitlement_active()`. `_connect_entitlement_status` (`engine_api.py:1440-1442`) adds it to the package's `public_dict()`; the `connect-automate` package is unchanged. It is advisory for display; the engine check in items 1-2 is authoritative. This is the only place the desktop learns the Automations state.

Desktop:

5. Rust `ConnectEntitlementStatus` (`desktop/src-tauri/src/engine.rs:927`) gains `automations_active: bool` with `#[serde(default)]`, so an absent field reads as `false` (locked) rather than being silently dropped. The TypeScript `ConnectEntitlementStatus` (`desktop/src/main.ts:271`) gains the same field.
6. The Health tab's Connect card (`renderConnectStatus`, `desktop/src/main.ts:3623-3648`) states whether Automations is included whenever the Connect license is active. The detail line keeps today's text and adds "Automations are included." or "Automations are not included in this license, so rules can't be created or turned on." When Connect is not active, the card is unchanged. So "View Connect" from a locked COI setup lands on a screen that explains the lock.
6a. On each refresh the COI setup also fetches `connect_entitlement_status` (an existing Tauri command), alongside its current calls (`desktop/src/coiSetup.ts:133-136`). When it reports `automations_active: false`, the COI setup:
   - disables "Save enabled rule" (`data-action="save"`);
   - disables the toggle when it would enable a paused rule;
   - keeps the toggle enabled when it would pause an enabled rule;
   - shows "Automations locked" with a "View Connect" button that opens the Health view, the same pattern as the Inbox's "Connect actions locked" (`desktop/src/main.ts:1979-1993`).

   When the flag is `true`, the setup behaves exactly as today.
7. If a save or enable fails with `automation_entitlement_required` (the license lapsed after the last refresh), the setup shows the engine's message and refreshes, which shows the locked state.

### Invariants

- With an inactive entitlement, no request creates a rule, changes a definition, or turns a rule on, whatever its source (desktop, CLI, or script).
- Disable and delete always succeed under the existing CAS and lock rules, whatever the entitlement.
- The desktop's copy of `automations_active` can be stale; the engine decides. A stale `true` produces a refused write and a refresh, never a write.
- `_automation_entitlement_active` is the single source for the engine check and for `automations_active`. `connect.entitlement.status` is the single place the desktop reads it, for both Health and the COI setup.

### Concurrency

- The check runs before the lock, so a license that lapses between the check and the write is allowed through for that one call. This is accepted: dispatch remains gated, so such a rule cannot run, and the next mutation is refused.
- The desktop uses the existing mutation reservation (`coiRules.ts` "mutation reserves before awaiting…" test). Locked-state rendering reads only the last committed list.

### Failure cases

- `_automation_entitlement_active` returning false for any reason, including a missing or invalid license, fails closed: the call is refused and the UI is locked.
- The scripted COI proof (`scripts/coi_local_proof.py`, which uses the installed license) needs a license that includes Automations to create its rule. That is intended; the script is unchanged.

### Files touched

- `src/eom_email_watcher/engine_api.py`
- `docs/ENGINE_API.md`
- `docs/AUTOMATE_RULE_ENGINE_CONTRACT.md`: the §10 deferred item is replaced by this decision
- `docs/CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md`: the M5 desktop setup section (`:277-331`) states that save and resume require the Automations entitlement, that pause does not, and that the setup shows the locked state
- `desktop/src-tauri/src/engine.rs`
- `desktop/src/coiRules.ts`
- `desktop/src/coiSetup.ts`
- `desktop/src/main.ts`: passes a "View Connect" callback to `mountCoiSetup`
- `desktop/test/coiRules.test.ts`
- `tests/test_engine_api.py`
- `tests/test_coi_local_proof.py`: only if its in-process run reaches `put`, in which case it enables the entitlement explicitly

## Mechanism

- Engine: add `_require_automation_entitlement()`, modelled on `_require_calendar_entitlement` (`engine_api.py:1491`). Call it at the top of `_automation_rules_put` and in `_automation_rules_set_enabled` when `enabled` is true, before `_with_automation_rule_mutation`. `_connect_entitlement_status` returns the package's `public_dict()` plus `automations_active`.
- Desktop: a pure function in `coiRules.ts`, `coiControlState(automationsActive, selectedRule)`, returns whether Save and the toggle are enabled and whether the locked line shows. `coiSetup.ts` fetches `connect_entitlement_status` on refresh and applies it. `mountCoiSetup` takes an optional `onViewConnect` callback, which `main.ts` supplies as `() => showView("health")`. `renderConnectStatus` adds the Automations sentence when `status.active`.

## Intentional

- Authoring requires the license, and disable/delete never do (operator decision).
- The Automations state is exposed once, on the existing `connect.entitlement.status`, rather than on the rule list or a new operation.

## Deferred

- A general (non-COI) rule-management UI: still #143's other open item.

## Verification

- Fail-first engine tests in `tests/test_engine_api.py`, with the entitlement monkeypatched as existing tests do (`tests/test_connect_v2_engine_api.py:766`):
  - with it inactive, `put` (create), `put` (edit), `put_watched`, and `set_enabled(true)` are each refused with `automation_entitlement_required`, and the rules snapshot revision is unchanged;
  - with it inactive, `set_enabled(false)` and `delete` succeed;
  - with it active, all succeed;
  - `connect.entitlement.status` reports `automations_active` both ways and keeps the package's `state` and `active`;
  - `_automation_entitlement_active` itself still requires both features (one test on `feature_entitlements_active`'s arguments).
- Existing rule-mutation tests (the 17 engine calls in `tests/test_engine_api.py`) enable the entitlement explicitly. None is weakened or deleted.
- Rust: a typed test for `ConnectEntitlementStatus` deserializes with `automations_active` true, false, and absent (absent reads as false).
- Desktop: `coiControlState` unit tests (locked plus new rule, locked plus enabled rule, locked plus paused rule, entitled), and source wiring for:
  - the locked line and the "View Connect" callback;
  - refresh on `automation_entitlement_required`;
  - the COI refresh fetching `connect_entitlement_status`;
  - `renderConnectStatus` adding the Automations sentence only when Connect is active.
- Commands, each a narrowing of a prefix declared in the Codex scope file:
  - `uv run --locked pytest -q tests/test_engine_api.py tests/test_coi_local_proof.py tests/test_certificate_expiry_ledger.py tests/test_connect_v2_engine_api.py`
  - `uv run --locked ruff check`
  - `node --test --experimental-strip-types --test-isolation=none desktop/test/*.test.ts`
  - `pnpm --dir desktop build`
  - `cargo test --manifest-path desktop/src-tauri/Cargo.toml --lib --offline`
- Reviewer-run: `cargo fmt --check` and `clippy -D warnings`, as the release-candidate workflow enforces.

## Estimated diff size

| File | LOC |
|---|---:|
| `src/eom_email_watcher/engine_api.py` | 20 |
| `docs/ENGINE_API.md` | 8 |
| `docs/AUTOMATE_RULE_ENGINE_CONTRACT.md` | 6 |
| `docs/CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md` | 8 |
| `desktop/src-tauri/src/engine.rs` | 20 |
| `desktop/src/coiRules.ts` | 20 |
| `desktop/src/coiSetup.ts` | 35 |
| `desktop/src/main.ts` | 12 |
| `desktop/test/coiRules.test.ts` | 45 |
| `tests/test_engine_api.py` | 100 |
| `tests/test_coi_local_proof.py` | 4 |
| **Total** | **283** |

## Codex scope file

```json
{
  "roots": ["<worktree>"],
  "allow": [
    "src/eom_email_watcher/engine_api.py", "docs/ENGINE_API.md", "docs/AUTOMATE_RULE_ENGINE_CONTRACT.md", "docs/CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md",
    "desktop/src-tauri/src/engine.rs", "desktop/src/coiRules.ts", "desktop/src/coiSetup.ts", "desktop/src/main.ts",
    "desktop/test/coiRules.test.ts", "tests/test_engine_api.py", "tests/test_coi_local_proof.py"
  ],
  "goal": "Automations entitlement on rule changes (plans/PR-Automation-Rule-Entitlement.md)",
  "plan": "plans/PR-Automation-Rule-Entitlement.md",
  "verify": {
    "commands": [
      "uv run --locked pytest -q",
      "uv run --locked ruff check",
      "node --test --experimental-strip-types --test-isolation=none",
      "pnpm --dir desktop build",
      "cargo test --manifest-path desktop/src-tauri/Cargo.toml --lib"
    ],
    "max_runs": 12
  },
  "churn": { "max_lines": 360, "max_new_tests": 18 }
}
```

## Review amendments (Codex review of `72c4763`)

1. "View Connect" led to a Health card that reports the Connect license as "Active" without mentioning Automations, which is misleading for exactly the Connect-only user. Health now states whether Automations is included. The Automations state moved from a rule-list flag to `connect.entitlement.status`, so Health and the COI setup read it from one place.
2. The COI-specific contract (`CERTIFICATE_EXPIRY_LEDGER_AUTOMATION_CONTRACT.md`, M5) now gets the save/resume restriction too, not only the general rule-engine contract.
