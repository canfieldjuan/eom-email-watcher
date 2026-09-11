# ADR-001: On-prem inference gateway v0 boundary

**Status:** Accepted direction; runtime proof pending
**Date:** 2026-08-29
**Updated:** 2026-09-03
**Decider:** Juan Canfield
**Scope:** Contract only; no runtime or application behavior changes
**Implementation tracking:** GitHub issue #72

## Context

The product direction is one inference appliance serving several users and focused desktop apps
inside one small-business network. Customer content must stay on premises. The deployment has an
administrator and users, but it is not multi-tenant SaaS.

Current code cannot use that shape directly:

- Email Watcher admits only explicit-port loopback HTTP and says email bodies may not leave the
  machine (`src/eom_email_watcher/config.py:123-151`). Its client sends an OpenAI-compatible
  request containing a concrete model name (`src/eom_email_watcher/model.py:95-180`).
- Document Summarizer likewise admits only exact-loopback HTTP, disables proxies and redirects,
  and sends a concrete model name (`doc_sum/src-tauri/src/pipeline/model.rs:11-12,50-102,124-190`
  at revision `fb09f20a0e560638ffec2c1cf6ad2560dd2ad887`).
- Connect discovers application capabilities such as `document.summarize`; it does not discover or
  schedule models (`docs/CONNECT_V1.md:1-17`).
- The pinned worker proof established concurrent request compatibility and bearer-key rejection,
  but did not establish users, scoped authorization, fair scheduling, TLS, or appliance lifecycle
  (`docs/LLAMA_CPP_COMPATIBILITY.md:87-104`).

Earlier direction notes disagreed about the selected worker: Email Watcher Issue #13 named
llama.cpp, Document Summarizer selected Ollama, and the previous revision of this ADR selected vLLM
with an Ollama fallback. Applications must still remain independent of that choice. On 2026-09-03,
the administrator deployment policy superseded that worker order: Ollama is the primary worker and
LM Studio's headless `llmster` service is the planned unloaded-model fallback.

Email Watcher, Document Summarizer, and the in-flight Invoice Processor must use one shared
Qwen3-30B-A3B deployment profile through the gateway. The logical profile is
`qwen3-30b-a3b`; current development aliases such as `qwen3-30b-a3b:latest` and Ollama's published
`qwen3:30b-a3b` tag are runtime-specific inputs to qualification, not fields applications send and
not immutable production identities. Promotion pins the exact upstream checkpoint, quantization,
model-content digest, runtime configuration, and runtime-specific identifier for both workers.

## Decision

Introduce a separate, local-network **Inference Gateway** between desktop applications and one or
more model workers.

```text
Email Watcher ---------\
Document Summarizer ----+-- HTTPS + app credential --> Inference Gateway --> worker(s)
Invoice Processor ------/                                  |
Future local apps ------/                                  |
                                                           +-- task policy
                                                           +-- bounded fair queue
                                                           +-- model/runtime health
                                                           +-- aggregate metrics
```

The gateway is infrastructure, not Connect. Connect continues to answer “which application
provides this domain capability?” The gateway answers “which administrator-approved worker should
execute this application's model request?”

The first implementation will use direct application-to-gateway HTTPS. It will not add a
per-desktop loopback proxy unless real client-platform evidence later proves one necessary.

## Worker deployment policy

- Ollama is the primary inference worker. Promotion pins the exact Ollama package or container,
  dependency set, immutable model manifest and content digests, complete serving configuration,
  and cloud-disabled environment (`OLLAMA_NO_CLOUD=1`). Each task must pass its task-specific
  deterministic metrics and validation plus blinded human review for semantic outputs; structural
  validity alone is not evidence of useful model behavior.
- LM Studio is the planned fallback worker. Its headless `llmster` daemon and HTTP server may remain
  running without a loaded model. Just-In-Time loading and eviction may load the approved
  Qwen3-30B-A3B profile only after fallback admission. Qualification pins the exact LM Studio and
  inference-runtime versions, dependency set, model-content digest, model identifier, context/GPU/
  structured-output settings, JIT/eviction configuration, and authentication/network policy.
  Before policy marks LM Studio eligible for a task, that task must independently pass the same
  task-specific deterministic metrics and validation plus blinded human review required of the
  primary. Protocol similarity or a matching display name is not evidence of artifact, semantic,
  or privacy compatibility.
- vLLM and standalone llama.cpp are not selected production-worker targets for this v0 deployment.
  Existing deployment files and compatibility evidence may remain for historical comparison, but
  no application client should bind directly to them or to either selected worker.
- The gateway owns worker selection, health, model-profile resolution, and fallback. Email Watcher,
  Document Summarizer, Invoice Processor, and later applications submit task requirements and never
  select Ollama, LM Studio, a model artifact, or a fallback order.
- There is no cloud fallback. If neither approved local worker can serve a task, the gateway returns
  the existing bounded availability error and the application preserves its standalone behavior.
- Worker listeners are gateway-private: bind them to gateway-host loopback or a local socket, or
  enforce equivalent host/network isolation. Client computers must reach only the gateway and must
  not be able to connect directly to either worker endpoint.

Fallback is fail-closed and identity-preserving:

1. A new request may use LM Studio only when policy marks Ollama unavailable before that work is
   admitted and LM Studio is healthy and qualified for the same task requirements and model
   profile.
2. An ambiguous or in-flight Ollama failure remains unresolved on its original gateway request and
   worker-attempt identity. The gateway must recover the primary's authoritative result, prove that
   the primary never accepted the request, or confirm cancellation before LM Studio may execute it.
   Reusing the request ID alone is not evidence that duplicate work cannot occur.
3. Authentication, authorization, malformed input, unsupported-task, and application-validation
   failures do not trigger fallback.
4. Client health reports task availability or degradation, never the chosen worker name.

Before worker dispatch, the gateway durably reserves the authenticated request ID, immutable
request expiry, canonical request digest, and selected worker attempt. Exact repeats join active
work or return the protected result until the application acknowledges durable receipt. A reused ID
with different content is rejected. After acknowledgement or expiry, the gateway deletes the
content-bearing result but retains a metadata-only terminal tombstone for a bounded replay-protection
period beyond the request expiry. The client must never reuse an expired request ID, and the gateway
rejects any request whose immutable expiry has passed rather than dispatching it again.

The intended single-GPU fallback posture is a running LM Studio daemon/server with no resident
model. JIT may load the approved model after known pre-admission Ollama unavailability. The gateway
must not assume that an unhealthy primary released VRAM: it must prove the primary has no admitted
or ambiguous work and that sufficient capacity is available, explicitly unload/stop the primary
model under the appliance lifecycle contract, or keep the fallback unavailable. Concurrent primary
and fallback residency requires separate capacity evidence or separate hardware.

On 2026-09-03, the development machine reported Ollama client 0.24.0 with no running server and no
installed manifest for the selected profile. LM Studio's `llmster` server was listening on loopback
with no model loaded, and matching Qwen3-30B-A3B GGUF candidates existed on disk. This is
installation and cold-standby-shape evidence only, not workload, artifact-equivalence, JIT-load, or
failover proof. The first proof must create or acquire the selected Ollama mapping and pin both
workers' exact artifacts and serving configurations before either is called production-ready.

## Ownership boundaries

### Applications own

- the domain task identifier and version;
- prompt construction and untrusted-input delimiters;
- structured-output schema or plain-text output requirement;
- input-size admission before transmission;
- deterministic output validation;
- durable application state, retry decisions, and idempotency;
- authorization and every irreversible business effect.

Applications do not send a model ID and do not receive authority to configure workers.

### Gateway owns

- HTTPS termination and client credential authentication;
- task-scope authorization;
- bounded request admission, per-client limits, and fair scheduling;
- mapping versioned tasks/requirements to administrator-approved model lanes;
- worker lifecycle, health, capacity, timeouts, and model promotion/rollback;
- content-free operational metrics and auditable request metadata;
- translating stable gateway errors from worker-specific failures.

The gateway does not own application prompts, domain validation, business state, Connect
capabilities, attachments, workflows, or application databases.

### Workers own

- model loading and inference;
- runtime-specific batching, context, reasoning, and accelerator settings;
- no user identity, application discovery, or business authorization.

Workers are not exposed directly to client computers.

## Transport and trust contract

Remote inference is additive. Existing exact-loopback adapters remain valid and their validators
must not be weakened to admit arbitrary network URLs.

A gateway adapter must enforce all of the following:

1. HTTPS only for non-loopback endpoints. Plaintext private-LAN HTTP is rejected.
2. Normal certificate verification against the explicitly paired appliance trust root. There is no
   `verify=false` or “accept any certificate” mode.
3. No environment proxy, HTTP redirect, URL user-info, query, or fragment.
4. The configured authority is fixed after pairing; redirects and response-supplied endpoints
   cannot move a request elsewhere.
5. Bounded request and response bodies, connect/read deadlines, and cancellation on application
   shutdown.
6. No cloud fallback. An unavailable appliance degrades model-dependent actions only.

Initial setup may require the administrator to provide the appliance URL, trust root, and a
single-use pairing code. Zero-touch discovery and certificate provisioning are later UX work, not
permission to weaken this boundary.

## Identity and authorization

The organization has one administrator domain, not tenants.

- Administrator credentials manage users, client credentials, task policy, workers, and aggregate
  health. They are never installed in ordinary applications.
- Each user/device/application installation receives its own revocable opaque credential.
- Credentials are stored hashed at the gateway and in the platform credential store or a
  mode-restricted local file at the client. They are never placed in app config, logs, URLs, or
  Connect registrations.
- A credential is scoped to explicit task IDs and maximum request limits. Authorization defaults
  to deny for unknown tasks and versions.
- Pairing codes are single-use, short-lived, and exchanged only over the authenticated TLS
  channel. Pairing UX and recovery remain a later implementation slice.

v0 does not require an external identity provider, cloud account, JWT, organization selector,
enterprise RBAC, or multi-tenant database.

## Client-facing request contract

The stable gateway operation is synchronous `POST /v1/inference`. Synchronous operation preserves
both current application call shapes and avoids inventing a distributed job system before one is
needed.

Illustrative request:

```json
{
  "protocol_version": 1,
  "request_id": "018f...uuid",
  "request_expires_at": "2026-09-08T18:00:00Z",
  "task": {
    "id": "email.analyze",
    "version": 1
  },
  "requirements": {
    "input_modalities": ["text"],
    "output_media_type": "application/json",
    "structured_output": true,
    "max_output_tokens": 500
  },
  "generation": {
    "messages": [
      {"role": "system", "content": "application-owned prompt"},
      {"role": "user", "content": "application-owned delimited input"}
    ],
    "temperature": 0.1,
    "response_schema": {}
  }
}
```

Required rules:

- `protocol_version`, `request_id`, `request_expires_at`, task ID/version, requirements, and
  generation payload are mandatory and bounded. The client chooses and durably records the immutable
  expiry before first submission; gateway task policy rejects expiries outside its allowed window.
- `request_id` is the canonical lowercase UUIDv4 text form matching
  `^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`. Alternate or
  percent-encoded forms are rejected, so the same value is unambiguous in JSON and a URL segment.
- The request contains no `model`, worker URL, runtime command, lane name, user role, or routing
  override.
- The gateway authorizes the task before queueing and rejects requirements unsupported by its
  current policy.
- The gateway treats prompts and content as opaque untrusted data. Task IDs control policy; prompt
  text cannot select workers or elevate limits.
- Before dispatch, the gateway durably reserves `request_id`, `request_expires_at`, credential
  identity, a canonical request digest, and worker-attempt identity. An exact repeat joins active
  work or returns the protected unacknowledged result across gateway restarts. Reuse with different
  content is rejected.
- The application acknowledges a result only after it has durably persisted either the validated
  domain result or a terminal application-validation rejection. Acknowledgement authorizes deletion
  of the gateway's content-bearing result, not deletion of its metadata tombstone.
- An expired request is never dispatched. The gateway returns permanent `request_expired`; the
  client stops automatic retries for that identity and exposes an explicit requeue that creates a
  new request ID and expiry.
- Expiry terminalizes the request and any in-flight worker attempt. The gateway requests best-effort
  cancellation but does not depend on cancellation succeeding: output arriving after expiry is
  discarded and cannot recreate the result buffer or change the terminal tombstone.

Illustrative success:

```json
{
  "protocol_version": 1,
  "request_id": "018f...uuid",
  "status": "completed",
  "output": {
    "media_type": "application/json",
    "content": "application-validates-this"
  },
  "provenance": {
    "task_policy_version": 3,
    "deployment_id": "opaque-admin-deployment-id"
  }
}
```

The gateway may return opaque deployment provenance for audit and comparison. Applications do not
branch business behavior on a worker or model name.

### Result acknowledgement operation

After durable application handling, the client calls
`POST /v1/inference/{request_id}/ack` with the same application credential that owns the reserved
request:

```json
{
  "protocol_version": 1,
  "request_id": "018f...uuid",
  "disposition": "persisted"
}
```

`disposition` is either `persisted` or `application_rejected`. The acknowledgement contains no
generated output, prompt, validation detail, or other customer content. The path and body request
IDs must match.

A successful first or exact-repeat acknowledgement returns HTTP 200:

```json
{
  "protocol_version": 1,
  "request_id": "018f...uuid",
  "status": "acknowledged",
  "disposition": "persisted"
}
```

The gateway accepts acknowledgement only from the credential identity that owns the reservation.
Another valid credential receives `forbidden`; the gateway makes no state change or deletion, and
the protected result remains available to its owner. The first valid acknowledgement of a terminal,
unexpired result atomically records its disposition in the metadata tombstone and deletes the
content-bearing result.

Acknowledgement and expiry use one serialized state transition. If acknowledgement commits first,
its tombstone disposition takes precedence over later wall-clock expiry: an exact repeat returns the
same HTTP 200 response while that tombstone exists, even after request expiry, while a conflicting
disposition fails permanently with `acknowledgement_conflict`. If expiry commits first, the request
returns `request_expired` and cannot be acknowledged. A nonterminal, unexpired request returns
`result_not_terminal`. After bounded tombstone cleanup, an unknown ID returns `unknown_request`.
None of these responses can revive work or retain late output.

## Failure and scheduling contract

The gateway returns a stable bounded error envelope with `code`, `retryable`, and optional
`retry_after_seconds`; it never returns worker stack traces or prompt fragments.

```json
{
  "protocol_version": 1,
  "request_id": "018f...uuid",
  "status": "failed",
  "error": {
    "code": "capacity_limited",
    "retryable": true,
    "retry_after_seconds": 90
  }
}
```

The client rejects malformed envelopes, mismatched request IDs, non-boolean `retryable` values,
retry delays outside 1 through 86400 seconds, and retry delays on permanent failures. For Email
Watcher, one analysis attempt keeps the same request ID, expiry, context timestamp, and body-size
limit across process restarts. The request is reconstructed from Gmail's immutable message payload
rather than persisting the raw body. Automatic retries stop at the immutable expiry. An explicit
requeue after expiry, configuration repair, contract repair, or terminal application-output
rejection creates a new request identity and expiry.

Minimum classes:

| Class | Meaning | Client behavior |
|---|---|---|
| invalid_request | Malformed, oversized, or inconsistent request | Do not retry unchanged |
| unauthenticated | Missing, expired, or revoked credential | Require repair/pairing |
| forbidden | Credential lacks the task/version scope | Do not retry unchanged |
| unsupported_task | No approved policy satisfies the requirements | Mark capability unavailable |
| capacity_limited | Bounded queue or client limit reached | Retry after the supplied delay |
| worker_unavailable | Approved worker is unhealthy | Preserve local work and retry later |
| inference_timeout | Admitted inference exceeded its deadline | Preserve local work and retry per app policy |
| invalid_worker_output | Worker response violated the gateway envelope | Do not treat as domain-valid output |
| request_expired | The immutable request lifetime ended | Stop automatic retries; offer explicit new-identity requeue |
| unknown_request | No live reservation or retained tombstone identifies the request | Stop acknowledgement/retry for that identity |
| result_not_terminal | Acknowledgement arrived before a terminal result | Do not delete content; reconcile the request first |
| acknowledgement_conflict | A different acknowledgement disposition already won | Preserve the first terminal disposition |

Application validation happens after the gateway returns a valid envelope. If the application
rejects that output, it durably marks the original request terminally rejected and acknowledges
receipt so the gateway can delete the buffered result. It must not automatically resubmit that
identity and receive the same retained output forever. Email Watcher's ordinary analysis path maps
this case to terminal rejection plus explicit requeue behavior. Its nested scheduling-extraction
schema remains outside the gateway's initial bounded schema subset and requires a separate contract
slice before that automation can use the gateway.

v0 scheduling is a bounded fair queue across client credentials, with per-credential in-flight and
queued limits. It must prevent one document workload from starving small email tasks. Exact queue
depths and lane mappings are administrator capacity policy, not client contract fields.

## Health contract

The gateway exposes two health views over the same verified HTTPS authority:

- `GET /health/live` is unauthenticated and returns only process liveness plus protocol version. It
  exposes no users, tasks, workers, models, queue state, or hardware details.
- `GET /v1/health` requires a client credential and returns only the task IDs/versions that
  credential may call, each as `available`, `degraded`, or `unavailable`, plus a bounded diagnostic
  code. It does not expose model identities or other clients.

Administrator worker/GPU/queue diagnostics are a separate privileged surface and are not returned
by either application health endpoint. Applications use task availability—not worker presence—to
decide whether a model-dependent action can run.

## Privacy and observability

Default logs and metrics may contain:

- request ID, credential ID hash, task ID/version, timestamps, status, latency;
- admitted input/output byte and token counts;
- queue and worker health/capacity;
- opaque deployment/policy versions.

They must not contain prompts, email/document content, generated output, bearer credentials,
filenames, sender/subject, attachment bytes, or application database identifiers. Prompts and
worker input payloads are memory-only unless an administrator explicitly enables a separately
designed and audited diagnostic mode.

Generated output awaiting application acknowledgement is a narrow exception: it is encrypted at
rest in a gateway-owned, credential-scoped result buffer, accessible only to the same authenticated
credential and canonical request. It is excluded from logs, metrics, diagnostics, and backups and
is deleted on durable application acknowledgement or immutable request expiry. For a bounded
replay-protection period beyond expiry, a metadata-only tombstone retains request ID, credential
hash, canonical digest, terminal status, timestamps, and expiry, but no prompt or generated content.
Late worker output is discarded before durable storage and cannot resurrect expired content.

## Availability and standalone behavior

- Applications start and retain non-model workflows when the gateway is missing.
- Model-dependent actions report unavailable or retryable failure without corrupting local state.
- Email Watcher retains exact sender gating, body non-persistence, durable analysis/delivery, and
  metadata-only fallback behavior.
- Document Summarizer retains local ingestion, provenance, pipeline persistence, and deterministic
  verification.
- Connect availability and discovery do not depend on gateway availability.
- Removing or replacing a worker changes gateway health/policy, not application configuration.

## Options considered

### A. Direct HTTPS gateway with per-app credentials — selected

| Dimension | Assessment |
|---|---|
| Client machinery | Medium; one additive adapter per language |
| Security | Strong when paired trust and no-proxy/no-redirect rules are enforced |
| Packaging | No extra desktop process |
| Worker flexibility | High; worker and model stay behind policy |

This is the smallest mechanism that provides encrypted multi-user access without exposing workers
or duplicating a resident bridge on every computer.

### B. Expose worker runtimes directly on the LAN — rejected

It leaks worker/model selection into apps and the network surface. The proven API-key boundary does
not establish administrator/user roles, per-task scopes, fair scheduling, stable errors, or
runtime-independent policy.

### C. Per-desktop loopback bridge — deferred

It preserves existing loopback clients but adds another installed process, lifecycle, port, update,
and credential owner on every PC. Reconsider only if implementing secure HTTPS adapters in the
supported application languages proves materially worse.

### D. Use Connect as the inference broker — rejected

It conflates application capability discovery with model infrastructure and would make standalone
Connect behavior depend on appliance availability.

### E. Cloud relay/control plane — rejected

It violates the no-cloud ordinary inference boundary and adds an account/service dependency the
small-business on-prem product does not need.

## Consequences

What becomes easier:

- one GPU appliance can serve multiple users and applications;
- worker/model upgrades do not require application releases;
- one place owns capacity, fairness, credentials, and privacy-safe metrics;
- app #3 can use the same protocol without modifying apps #1 or #2.

What becomes harder:

- the appliance must provision TLS trust and application credentials;
- Email Watcher and Document Summarizer need additive gateway adapters and health states, while the
  in-flight Invoice Processor must start on that same client contract rather than add a direct
  worker binding;
- the gateway becomes shared infrastructure that requires backup/update/recovery procedures;
- sustained-load sizing, cold-fallback latency, and model-profile promotion need evidence rather
  than guesses.

## Explicit non-goals for v0

- Connect changes or cross-machine app discovery;
- workflow engine, arbitrary agents, automatic email replies, or irreversible model actions;
- cloud accounts, remote execution over the public internet, multi-organization tenancy, or RBAC;
- chat UI, streaming tokens, async callbacks, or durable distributed job queues;
- model download UI, GPU installers, marketplace, billing, licensing, or analytics dashboard;
- attachment/vision transport before the text proof is accepted;
- selecting Ollama, LM Studio, a model, quantization, fallback order, or lane count in the
  application contract;
- defining Invoice Processor's domain task, schema, or product behavior while that application is
  in flight, or requiring its client adoption in the current two-task proof.

## First implementation proof after acceptance

Build one gateway process with Ollama primary, unloaded-model LM Studio fallback, the single
Qwen3-30B-A3B deployment profile, and two administrator-allowed task IDs:
`email.analyze@1` and `document.chunk.summarize@1`. Proving Invoice Processor adoption is a later
acceptance gate after its in-flight repository publishes a versioned task; that application must
then use this same client and deployment-profile boundary rather than introduce a direct worker
binding. Prove the current two-task slice, with synthetic content only:

1. paired Email Watcher and Document Summarizer clients can authenticate over verified HTTPS;
2. neither request contains a model ID;
3. the exact Ollama package or container, dependency set, immutable model manifest/content digests,
   cloud-off environment, and serving configuration are pinned and reproducible;
4. the exact LM Studio/`llmster` and inference-runtime versions, dependency set, model artifact,
   identifier, JIT/eviction behavior, authentication, and serving configuration are pinned and
   reproducible;
5. both workers resolve the single logical profile to the same approved upstream checkpoint and
   quantization, with runtime-specific immutable digests recorded as deployment provenance;
6. both worker endpoints are unreachable from a client-network machine while the authenticated
   gateway remains reachable;
7. task policy selects Ollama without exposing that choice to either application;
8. each task independently passes its deterministic acceptance metrics and validators plus blinded
   human review of semantic output on both Ollama and LM Studio before that worker is eligible;
9. concurrent mixed requests complete without starvation at the admitted limit;
10. revoked, wrong-scope, oversized, redirected, proxied, and plaintext requests fail closed;
11. Ollama runs with `OLLAMA_NO_CLOUD=1`, uses the pinned profile, and passes task acceptance before
    becoming primary;
12. LM Studio remains healthy with no model resident, then JIT-loads the pinned profile only after
    safe fallback admission and unloads it under the configured eviction/TTL policy;
13. making Ollama unavailable before admission moves an LM-Studio-qualified task to
   degraded-but-available service, while an unqualified fallback task remains unavailable;
14. an in-flight or ambiguous primary failure remains unresolved until the primary result is
    recovered, non-acceptance is proven, or cancellation is confirmed; only then may the same
    request proceed without duplicate worker execution;
15. on a single GPU, fallback does not start until every primary attempt has the authoritative
    disposition required by item 14 and either capacity evidence proves concurrent residency safe,
    or the primary model is explicitly unloaded and the reclaimed capacity is verified; ambiguous
    work or VRAM ownership keeps fallback unavailable;
16. when Ollama recovers, the gateway stops new LM Studio admissions, waits until every fallback
    attempt has an authoritative disposition, drains and unloads the fallback model, and verifies
    capacity before readmitting Ollama; only separate concurrent-residency capacity evidence may
    waive the drain and unload steps;
17. request IDs accept only canonical URL-segment-safe UUIDv4 text, and mismatched or encoded
    acknowledgement identifiers fail closed;
18. a different valid credential cannot acknowledge or delete another credential's result, and the
    owning credential can still retrieve the protected result afterward;
19. a lost response returns the protected result to the same credential/request after gateway
    restart, and the authenticated acknowledgement operation atomically deletes that result content
    while retaining its metadata tombstone; exact repeats are idempotent, an acknowledged tombstone
    wins over later expiry, and conflicting dispositions fail closed;
20. an expired request terminalizes any in-flight attempt, never dispatches again, discards late
    output without recreating retained content, returns permanent `request_expired`, and permits
    only an explicit new-identity requeue;
21. an application-validation rejection becomes terminal for its original identity, is
    acknowledged to release the protected result, and permits only explicit new-identity requeue;
22. authentication, authorization, request-contract, and output-validation failures do not trigger
    fallback;
23. stopping the gateway degrades only model-dependent actions in both participating applications;
24. Connect discovery and each application's private persistence remain unchanged.

Do not implement administrator UI, auto-discovery, additional runtime families, vision, or
production cutover in that proof.

## Runtime references

- Ollama OpenAI compatibility and structured outputs:
  https://docs.ollama.com/api/openai-compatibility and
  https://docs.ollama.com/capabilities/structured-outputs
- Ollama Qwen3-30B-A3B published tag and current artifact metadata:
  https://ollama.com/library/qwen3:30b-a3b
- LM Studio headless `llmster`, JIT loading, and eviction:
  https://lmstudio.ai/docs/developer/core/headless_llmster and
  https://lmstudio.ai/docs/developer/core/server/settings
- LM Studio CLI model identifiers and load/unload controls:
  https://lmstudio.ai/docs/cli
