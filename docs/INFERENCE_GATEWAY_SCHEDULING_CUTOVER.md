# Scheduling extraction gateway cutover

### Contract

Root cause:

- Email Watcher's `GatewayModel.extract_scheduling` currently submits the scheduling prompt and
  schema as `email.analyze@1`, so the gateway cannot authorize, advertise, or constrain the distinct
  operation independently.
- Pydantic emits the trusted, non-recursive scheduling schema with local `$defs`/`$ref`. The merged
  gateway deliberately rejects references and accepts bounded homogeneous arrays, so the exact
  schema still fails admission even though every domain collection already has a code-owned cap.
- Exact-current validation also proves the nullable `referenced_event` object contains two optional
  nullable identifier fields. That nested union is outside the gateway's fail-closed schema subset;
  reference inlining alone is therefore insufficient.

Required change surface:

- Submit scheduling extraction as `email.schedule.extract@1` while ordinary message analysis remains
  `email.analyze@1`.
- Derive the wire schema from `SchedulingExtraction.model_json_schema()` and inline only exact local
  `#/$defs/<name>` references from that same generated document. Reject unknown, malformed, mixed,
  or cyclic references locally; do not loosen the gateway.
- Narrow only `provider_event_id` and `human_reference` inside the nullable event-reference branch
  from “string or explicit null” to “optional string omitted when absent.” Pydantic already maps
  omission to `None`; keep the outer `referenced_event` null branch and all application validation.
  Fail locally if either identifier becomes required or the generated shape no longer matches.
- Keep the existing durable extraction request ID/time, expiry guard, output validation, persistence,
  and post-persistence acknowledgement lifecycle unchanged.
- Prove the emitted request with the exact merged gateway contract and a synthetic scheduling result.

Explicit non-scope:

- No prompt, Pydantic domain model, validator, automation ledger, retry policy, calendar proposal or
  write, entitlement, mailbox, notification, Connect, worker/model, credential-file, or deployment
  change.
- No general remote-schema resolver, external reference, recursive schema support, generic nullable
  rewriting, task fallback, or direct Ollama/LM Studio call.
- No claim that the appliance credential is already deployed with the new task grant or that live
  semantic acceptance has been performed.

Assumptions/blockers:

- Local Inference Gateway main includes the merged bounded `email.schedule.extract@1` task policy.
- Deployment must add the task grant to Email Watcher's existing gateway credential before live
  scheduling extraction is enabled; missing authorization must remain a visible permanent failure.

Verification plan:

- Unit tests prove ordinary analysis keeps its task identity, scheduling selects the new identity,
  the emitted schema contains neither `$defs` nor `$ref`, and accepted scheduling output still enters
  the current deterministic validator.
- Reference-inlining tests prove unknown, mixed, and cyclic local references fail before transport,
  while repeated references produce independent schema objects rather than shared mutable aliases.
  Event-reference tests prove the two absent identifiers use omission semantics and an unexpected or
  required generated shape fails closed.
- An exact-current cross-repository ASGI test passes Email Watcher's emitted scheduling request
  through the merged gateway parser, task policy, worker-output validator, result persistence, and
  acknowledgement deletion using synthetic content only.
- Focused Email Watcher gateway/scheduling/service tests and Ruff pass; the full unit/cov gate remains
  GitHub-owned by standing operator direction.

### Acceptance criteria

1. `analyze` still emits `email.analyze@1`; `extract_scheduling` emits
   `email.schedule.extract@1` through the same `_inference` transport.
2. The scheduling wire schema is derived from the authoritative Pydantic model, contains no reference
   or definition keywords, retains every array cap and the outer nullable event reference, represents
   its two absent identifiers by omission, and is accepted by the exact merged gateway
   `InferenceRequest` validator.
3. Any generated reference outside the exact local definitions map, a `$ref` with siblings, or a
   reference cycle fails locally before a credential read or transport call.
4. A valid synthetic result is accepted by the existing scheduling validator, persisted before the
   existing acknowledgement, and removed from the gateway result buffer after acknowledgement.
5. Unsupported-task/forbidden gateway responses remain permanent and do not fall back to a direct
   runtime or silently reuse another task identity.
6. Mailbox analysis, scheduling semantics, durable automation state, calendar behavior, Connect,
   notifications, retention, and `gmail.readonly` behavior remain unchanged.

### Implementation summary

- Scheduling extraction now selects `email.schedule.extract@1`; ordinary email analysis continues to
  select `email.analyze@1` through the same request transport.
- The wire schema is regenerated from `SchedulingExtraction`, safely expands exact local references,
  preserves bounded collections and the outer nullable event reference, and narrows only the two
  absent event identifiers to omission instead of explicit null.
- Malformed, external, mixed, unknown, cyclic, or unexpectedly required reference shapes become a
  permanent local `invalid_request` before credential access or transport.
- Existing deterministic scheduling validation and persistence-before-acknowledgement service
  behavior are unchanged.

### Cold diff audit

- `scheduling.py` owns the task-specific wire projection. It copies every referenced definition,
  fails on unsafe reference shapes, and checks the exact event-reference structure before narrowing
  the two optional identifiers; it does not alter the Pydantic domain model.
- `model.py` makes task identity an explicit internal `_inference` argument. Only the scheduling call
  selects the new task and schema; ordinary analysis retains its existing constants and behavior.
- Tests cover independent reference copies, retained array caps, the outer null branch, omission
  semantics accepted by Pydantic, unknown/mixed/external/null/cyclic reference failures, an
  unexpectedly required identifier, task selection, and permanent pre-transport schema failure.
- README/ADR changes replace the stale scheduling blocker with the implemented task grant and
  fail-closed behavior. Service, database, automation, calendar, mailbox, Connect, and deployment
  files are untouched.
- Untraced or forbidden changes: none.

### Gap audit

DONE for the client compatibility implementation; GitHub review remains pending.

- Selected schema/task guard tests: 10 passed.
- The focused scheduling/gateway/service suite completed at 100%, and Ruff passed.
- Exact-current cross-repository ASGI proof against merged gateway task policy: application
  `accepted=True`, one worker call, retained encrypted output before acknowledgement, and an
  acknowledged row with no retained output afterward.
- The first exact-current attempt disproved the original “inlining alone” assumption because the
  nested nullable event identifiers remained outside the gateway subset; the contract and wire
  projection were corrected before completion.
- Appliance credential deployment and live semantic model acceptance remain separate operational
  evidence and are not represented as complete by this PR.
