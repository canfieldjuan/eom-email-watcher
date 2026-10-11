# Adopt the shared fixed-parameter runtime in Email Watcher

Status: PROPOSED. Plan-only PR. Implementation requires operator acceptance
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
those discovery/preparation/restoration/join seams, interactive parameter
collection/types, and their behavioral regressions. Shared parsing and wire
policy remain shared owners; Watcher must not build its own header parser,
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
   string. Existing immutable rule definitions are not rewritten from later
   catalogs; conflicting stored intent must fail rather than silently change.
4. Add immutable, validated feature-requirement metadata to generic dispatch
   admission in the same transaction as the job. Use the shared feature
   validator and record exactly the selected capability's verified
   `acknowledged_manifest_features`: non-empty only on acknowledgment, empty
   on the legacy path, never inferred from the request header. This
   plan explicitly permits an additive dispatch migration (baseline schema
   version 31; allocate the next free version at implementation time).
   Legacy rows receive an empty requirement, not an invented acknowledgment.
   Keep provider `request_json` unchanged. Extend Watcher's host-side
   invocation/join identity to distinguish requirements; preserve existing
   legacy fingerprints and never rehash provider-owned requests.
5. Pass the persisted requirement to shared discovery before every proven-new
   submission, including authoritative resubmission. A missing acknowledgment
   cannot downgrade an admitted feature request. Restoration/resubmission
   consumes the exact frozen request and values, without rebinding from a
   rediscovered declaration. GET-only reconciliation remains available under
   existing ownership/entitlement rules and never needs a new manifest merely
   to observe a possibly accepted job. Reuse existing queue error/ownership
   transitions; shared fixed refusals must not cause retry loops or release a
   possibly owned lane on malformed/ambiguous evidence.
6. Update `desktop/src/connectTypes.ts` and parameter collection to consume
   fixed metadata. Factor collection into a directly testable helper if
   needed, without redesigning the UI or adding classifier controls. Prompt
   only for editable fields. The backend remains authoritative against forged
   caller values. Audit Rust transport types if they mirror the declaration;
   extend only an actual serialization boundary that would drop the metadata.
7. Run focused proof, source-owner mutations and required gates, then open a
   separate implementation PR. Record exact-head artifacts outside worktrees.

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
- Exercise a rule fire through the real preparation/queue path: its immutable
  stored parameters are preserved, and shared fixed binding is applied only
  to newly prepared intent, never as a rewrite of admitted request bytes.
- Queue, restart and rediscover the same provider with the feature absent or
  fixed declaration rotated. Verify the durable feature requirement survives
  restart, no downgraded POST occurs, GET reconciliation still works, and an
  eligible authoritative resubmission sends the original request/job/artifact
  identity. No inference or automatic inspection is involved.
- Two otherwise identical invocations with different feature requirements do
  not join. Identical requirements still deduplicate. Migrated legacy rows
  remain empty-requirement and retain their existing request/fingerprint.
- Exercise well-formed fixed refusal and malformed retryability/status
  variants through the queue; assert authoritative non-acceptance versus
  possible provider ownership without inventing a second error decoder.
- Desktop fixed values do not prompt and preserve falsy values; editable
  legacy fields and cancellation still work. Forged frontend values reject
  at the backend. Test the production collector, not a mirrored algorithm.
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

NOT DONE: this is a proposed adoption plan. No dependency, source, schema,
tests or product behavior has changed, and no adoption/runtime proof is
claimed. Review and operator acceptance precede implementation.
