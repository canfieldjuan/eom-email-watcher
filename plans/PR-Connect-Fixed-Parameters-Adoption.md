# Adopt the shared fixed-parameter runtime in Email Watcher

Status: PROPOSED AMENDMENT. Operator acceptance of the earlier plan does not
accept this consolidation. Implementation is paused pending fresh acceptance.
Plan-only PR. Implementation requires operator acceptance
of the reviewed plan. This does not authorize inspection, classification,
claims, provider changes, deployment, or merging a subsequent implementation PR.

## Problem-derived contract

Root cause: Watcher pins an older shared client and owns an independent durable
queue boundary. Updating the dependency alone neither carries the feature
requirement through that boundary nor updates the interactive parameter input.

Source baseline: Watcher main `742bc95fa922dca11ee8e9f6fe884c29b45b1ba6`.
`pyproject.toml` and `uv.lock` pin Connect Automate
`a3e2667741d9b06608e8a0b9689ea5f4fb75fc48`.
`engine_api.py` owns catalog discovery, `_discover_selected_generic_capability`,
`_prepare_or_create_generic_connect_job`, `_tracked_generic_job`,
`_discover_persisted_generic_capability`, and the lane pump.
`db.py` owns dispatch metadata and active invocation joining.
The queue currently restores durable `request_json`, but rediscovery does not
pass a recorded `required_manifest_features` requirement.
`desktop/src/connectTypes.ts` lacks fixed-value metadata, and
`collectCapabilityParameters` in `desktop/src/main.ts` prompts for every
parameter, including values a provider declares immutable.

Required surface: dependency pin/lockfile, queue metadata and its migration,
those discovery/preparation/restoration/join seams, immutable rule-selection
metadata and save/fire owners, inbox projection and desktop identity readers,
interactive parameter collection/types, Rust catalog transport, and their
behavioral regressions. Shared parsing and wire policy remain shared owners;
Watcher must not build its own header parser,
fixed-value comparator, or manifest negotiation policy.

## Validated upstream inventory and parity boundary

| Owner | Published source | Reuse in this slice |
| --- | --- | --- |
| Shared fixed-parameter policy/oracle | connect-contracts PR67, merge `0f77f8a77e6fb014f0d563645ebdbb83ce6dbc3b` | ADR-0013 and the fixed-parameters-v1 schemas, producer/consumer vectors and oracle |
| Shared client/runtime | connect-automate PR3, merge `a6bbb347ae6a4cb30aeb531153ef2c33ee828fed` | `connect.py`, `automate_connect.py`, `_manifest_policy.py`, and the runtime conformance fixtures/tests |
| Shared provenance | `manifest_policy/manifest_policy_sources.json` in that runtime | Pinned source revision and source hashes; never regenerate from an unpinned working tree |
| Watcher callers | Baseline above; Connect API/catalog and queue tests | Existing selection, confirmation, entitlement, cleanup and reconciliation paths |

The shared runtime's reviewed source is available locally at
`4f1173697a23ee9af578a3867e1b86edb87b2a59`; implementation must compare the
published merge tree with that source before adopting it. Prior shared PR
results are upstream evidence, not proof of Watcher adoption. This plan does
not claim a fresh runtime test or provider qualification.

No model is involved: there is no model pin, prompt, sampling setting, document
corpus, or inference budget to port. Reuse the public shared fixtures and
HTTP-boundary tests. Add Watcher tests for the actual caller and persistence
paths rather than another policy oracle.

Intentional integration differences from the upstream invoker:

- Watcher owns persisted jobs and lane reconciliation instead of using the
  shared invoker's action envelope. Persist the negotiated requirement as
  host metadata, outside the canonical provider job request.
- Watcher has an interactive parameter collector. Consume fixed declarations
  without prompting the user to edit immutable values; keep editable legacy
  parameters and confirmation behavior intact.
- Watcher owns active-job joins. A caller with a frozen feature requirement
  must not join a job that lacks it, even if provider request bytes match.

These differences require the parity probes below. There are no intentional
changes to the shared policy, provider wire schema, job identity or replay.

## Implementation sequence after acceptance

1. Add fail-first Watcher regressions that exercise real shared discovery and
   the application's request/queue owners. Use a deterministic local HTTP
   fixture or MockTransport at the transport boundary, not patched success
   results from discovery/parameter validation. First record which failures
   arise from the old dependency and which from missing Watcher metadata.
2. Update `pyproject.toml` to the published runtime merge above and regenerate
   `uv.lock` with uv. Audit all transitive changes; unrelated dependency
   updates are out of scope. Verify the installed revision and packaged
   policy/fixture provenance match the published runtime.
3. For new-intent catalog browsing and capability selection, always request
   `fixed-parameters-v1` through shared discovery's
   `required_manifest_features=("fixed-parameters-v1",)` option. This covers
   `_connect_catalog`, `_connect_attachment_capabilities` and
   `_discover_selected_generic_capability`, including rule selection.
   The shared consumer owner still admits an unacknowledged legacy response
   on its legacy decoder path; requesting support is not evidence of support.
   Bind fixed parameters once for each newly selected invocation using the
   shared parameter owner. Missing caller input receives the declaration's
   fixed value; a conflicting caller value follows the shared rejection.
   Preserve presence and primitive type, including false, zero and empty
   string. Freeze the acknowledged feature requirement and shared-bound fixed
   parameters when a rule revision is saved. Later fires consume that frozen
   selection; they never rebind omitted fields from a newer catalog. A
   conflicting declaration moves the fire to manual review. Legacy revisions
   without frozen selection metadata require manual review when their
   capability first declares fixed parameters. Re-confirming means saving an
   explicit new revision; do not mutate an existing revision or reinterpret
   its definition hash. Preserve legacy rules whose capabilities remain legacy.
   Store the immutable selection snapshot with the revision in the same
   transaction, after save-time discovery/shared validation. Enable/disable
   revisions retain their underlying selection; enabling is not reselection.
   If save-time discovery fails, do not save an unverified new selection.
4. Add immutable, validated feature-requirement metadata to generic dispatch
   admission in the same transaction as the job. Use the shared feature
   validator and record exactly the selected capability's verified
   `acknowledged_manifest_features`: non-empty only on acknowledgment, empty
   on the legacy path, never inferred from the request header. This
   plan explicitly permits additive dispatch and immutable rule-selection
   metadata migrations (baseline schema version 31; allocate the next free
   version at implementation time).
   Legacy rows receive an empty requirement, not an invented acknowledgment.
   Keep provider `request_json` unchanged. Extend Watcher's host-side
   invocation/join identity to distinguish requirements; preserve existing
   legacy fingerprints and never rehash provider-owned requests. The host
   fingerprint owner includes non-empty feature requirements, so inbox ranking
   using that fingerprint keeps those jobs separate; the empty legacy input
   retains its old fingerprint. Project the frozen requirement into result
   DTOs and verified acknowledgment into catalog DTOs, including Rust and
   TypeScript. Desktop result matching and request-ID caching consume those
   fields, never infer acknowledgment from a fixed-value field.
5. Pass the persisted requirement to shared discovery before every proven-new
   submission, including authoritative resubmission. A missing acknowledgment
   cannot downgrade an admitted feature request. Restoration/resubmission
   consumes the exact frozen request and values, without rebinding from a
   rediscovered declaration. Both interactive and automation crash recovery
   normalize an omitted fixed input through shared parameter binding against
   the persisted selection snapshot before comparing invocation identity.
   Explicit conflicts remain rejected. Preserve the minimal fixed declaration
   provenance needed by that shared owner at admission; never use a fresh
   catalog or fill omitted editable inputs during recovery. GET-only
   reconciliation remains available under existing ownership/entitlement rules
   and never needs a new manifest merely to observe a possibly accepted job. Reuse existing queue error/ownership
   transitions; shared fixed refusals must not cause retry loops or release a
   possibly owned lane on malformed/ambiguous evidence.
6. Update `desktop/src/connectTypes.ts` and parameter collection to consume
   fixed metadata. Factor collection into a directly testable helper if
   needed, without redesigning the UI or adding classifier controls. Prompt
   only for editable fields. The backend remains authoritative against forged
   caller values. Extend `ConnectParameter` in the Rust transport to preserve
   presence and primitive type of `fixed_value` when deserializing the engine
   response and serializing to TypeScript. Extend catalog/result identity DTOs
   at that actual serialization boundary as specified in the reader table.
7. Run focused proof, source-owner mutations and required gates, then open a
   separate implementation PR. Record exact-head artifacts outside worktrees.

## Consolidated owners and reader audit

The earlier plan covered queue admission but missed rule-selection lifetime
and readers of the same identity. This table is an implementation checklist,
not a second definition of shared wire or fixed-parameter policy.

| Owner / reader | Adopted form and regression |
| --- | --- |
| Shared discovery and parameter binder | Own negotiation, acknowledgment and typed fixed binding; Watcher consumes verified results and reuses the binder with persisted declarations on recovery |
| `_automation_rules_prepare`, `_automation_rules_put`, `Store.put_automation_rule`, rule parser/canonicalizer | Save one immutable revision selection snapshot from verified discovery; preserve definition hashing and carry snapshot through enable/disable |
| `Store._automation_rule_detail`, `_automation_rule_detail_data`, Rust `AutomationRuleDetail` across get/put/set-enabled, TS `RuleDetail` (`coiRules.ts`), saved-rule review (`coiSetup.ts`) | Project that revision snapshot read-only: verified feature requirement and fixed declarations/values, preserving presence and primitive types, or explicit legacy/none. Render with `textContent` even when the rule is not editable; never derive review values from a fresh catalog or change definition bytes/hash |
| `_dispatch_automation_fire`, including exact-job crash recovery | New fires use the revision snapshot; recovered jobs use their admission snapshot; rotation/legacy missing snapshot produces manual review, not implicit reselection |
| `_prepare_or_create_generic_connect_job`, `Store.create_connect_job`, dispatch migration | Atomically admit frozen request, verified requirement and fixed declaration provenance; legacy rows stay empty and do not gain invented acknowledgment |
| `_validate_existing_invocation_identity`, interactive retry and automation recovery callers | Normalize only omitted fixed fields with the shared binder and persisted declarations, then compare durable intent; explicit conflicts and wrong types reject |
| Host fingerprint owner, active/completed lookup, admission joins, effectful join guard | Include verified requirement in host identity; legacy empty requirement preserves its existing fingerprint; same-request-ID conflicts cannot bypass it |
| `Store.inbox_page`, result projection, `_generic_connect_result`, `_generic_connect_active_result` and result serializers | Rank by the requirement-aware host fingerprint and expose the frozen requirement; prove both otherwise identical jobs remain visible |
| `_tracked_generic_job`, `_discover_persisted_generic_capability`, submit/resume paths | Restore saved bytes; require frozen acknowledgment before new POST; GET-only reconciliation remains independent of fresh manifest availability |
| Rust `ConnectParameter`, capability catalog and attachment/result DTOs, `Engine::attachment_capabilities` | Round-trip fixed fields and feature identity through the real typed engine transport, retaining false, zero and empty string, omitting absent legacy fields |
| Desktop collector, `matchingCapabilityResult`, `capabilityInvocationKey`, request-ID cache call sites and result types | Consume fixed metadata without prompting; matching/cache identities use verified catalog versus frozen result requirements and never reuse a different-requirement job ID |
| `isCoiProvider` (`desktop/src/coiRules.ts`), provider list and live pre-save recheck (`desktop/src/coiSetup.ts`) | Both callers consume the same fixed-aware provider eligibility owner; required declared values must not disappear at the rule-provider filter before selection or save |

Audit every reader and producer of parameters, invocation fingerprints,
requirements, rule revisions and capability DTOs before pushing implementation.
No private manifest parser/comparator or downstream identity special case is
introduced. Where an existing policy must be factored to one owner, its callers
are included in the same class fix.

## Verification and fail-first acceptance

- End-to-end negotiation: start with Watcher's real catalog/selection call,
  assert the outgoing manifest feature header, return a correctly acknowledged
  fixed declaration, select it with caller values omitted, admit the job,
  restart the queue, and assert both the saved feature requirement and the
  eventual POST's bound fixed value. Do not inject a pre-acknowledged
  capability or mock discovery success. Repeat with a legacy provider that
  ignores the feature request: it remains selectable, records an empty
  requirement and submits its unchanged legacy parameters.
- Legacy discovery without negotiated headers remains usable, while malformed
  present headers and wrong/unsolicited acknowledgment reject through the
  shared consumer owner. Negotiated discovery takes the shared strict path.
- Invoke a supported attachment through Watcher's interactive API with fixed
  string/integer/boolean fields, both omitted and supplied identically. Verify
  actual submitted multipart request values. Conflicting and wrong-type
  values make no POST; probe false, zero, empty string and bool-as-integer.
- Save a rule through the real discovery/save path, fire it, then rotate the
  fixed declaration before a separate fire. Assert the revision's fixed
  values and requirement remain frozen and the later conflict needs manual
  review. Test legacy missing snapshots and enabling an old revision; neither
  operation may silently adopt a current fixed value. Include save-time
  discovery failure and explicit new-revision re-confirmation.
- Replay the original omitted fixed inputs through the interactive API and
  automation exact-job crash recovery after admission/restart. Assert the same
  request identity is recovered without a fresh catalog; conflicting supplied
  values still reject and omitted editable fields are not filled implicitly.
- Queue, restart and rediscover the same provider with the feature absent or
  fixed declaration rotated. Verify the durable feature requirement survives
  restart, no downgraded POST occurs, GET reconciliation still works, and an
  eligible authoritative resubmission sends the original request/job/artifact
  identity. No inference or automatic inspection is involved.
- Two otherwise identical invocations with different feature requirements do
  not join. Identical requirements still deduplicate. Migrated legacy rows
  remain empty-requirement and retain their existing request/fingerprint.
  Project both jobs through inbox ranking and the Rust result transport;
  desktop matching/cache tests must select/resume each without a cross-join.
- Exercise well-formed fixed refusal and malformed retryability/status
  variants through the queue; assert authoritative non-acceptance versus
  possible provider ownership without inventing a second error decoder.
- Desktop fixed values do not prompt and preserve falsy values; editable
  legacy fields and cancellation still work. Forged frontend values reject
  at the backend. Test the production collector, not a mirrored algorithm.
  Also round-trip declarations through `Engine::attachment_capabilities` with
  false, zero and empty string; a collector-only test cannot prove delivery.
- Exercise the rule-provider filter through both the production provider list
  and live pre-save recheck: required fixed parameters are accepted, including
  false, zero and empty string; required editable parameters are rejected.
  Mixed fixed/editable requirements remain rejected; a provider rotation
  between listing and save must be judged by the same `isCoiProvider` owner.
- Exercise the rule-detail projection through real engine transport for
  get/put/set-enabled and no-op responses, then render the saved-rule review.
  Assert the frozen requirement and fixed declarations/values survive with
  false, zero and empty string; legacy/none remains explicit. Cover a rule
  that is not editable, enable/disable preservation and unchanged definition
  bytes/hash. Test the production readers, not a mirrored projection.
- Existing Connect entitlement, Automations authority, confirmation, source
  deletion and effect-authority regressions remain passing. Issue238's
  pre-existing rule contract/source reconciliation stays a separate owner.
- Mutating away persisted feature requirements, restoring against current
  values, or weakening join isolation must independently break the relevant
  Watcher regression. Shared policy/oracle mutation checks remain upstream.

During implementation run the affected Connect/catalog/entitlement, queue/DB
migration and desktop helper tests plus Ruff and frontend checks. CI owns the
broad suites in `.github/workflows/ci.yml`, including Windows operation locks,
packaging and desktop jobs. Run any repository-required local gates; do not
claim Windows/package parity from a Linux unit test. The plan-only PR uses
`git diff --check` and a source-reference/diff audit, not duplicated code tests.

## Non-scope and blockers

No inspection engine origin, inspection selection switch/backfill, classifier
execution, cache/sweep, class checks, invoice record adoption, claims,
OCR/extraction lineage, mailbox capture/reconcile work, entitlement policy
change, shared-contract amendments, provider implementation, deployment or
release qualification. The accepted inspection contract and status-only PR237
are separate from this plan. No existing user-rule revision is migrated to a
new capability selection.

Block implementation until this plan receives operator acceptance after
review. If dependency adoption requires a shared semantic change, stop and
report the exact conflict rather than patching it locally. If the added
feature metadata needs a different migration or join strategy, update this
plan before expanding the implementation surface.

## Gap audit

NOT DONE: this PR changes only the proposed adoption plan. Partial
implementation exists separately and is paused; no adoption/runtime proof or
implementation acceptance is claimed here. Review and fresh operator
acceptance of this consolidation precede further implementation and merge.
