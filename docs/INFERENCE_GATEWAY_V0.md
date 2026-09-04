# ADR-001: On-prem inference gateway v0 boundary

**Status:** Accepted direction; runtime proof pending
**Date:** 2026-08-29
**Updated:** 2026-09-01
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
llama.cpp, while Document Summarizer selected Ollama. Applications must still remain independent of
that choice. The administrator deployment policy now selects vLLM as the primary worker and Ollama
as the planned fallback, subject to task-specific quality, compatibility, privacy, and capacity
proof.

## Decision

Introduce a separate, local-network **Inference Gateway** between desktop applications and one or
more model workers.

```text
Email Watcher ---------\
Document Summarizer ----+-- HTTPS + app credential --> Inference Gateway --> worker(s)
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

- vLLM is the primary inference worker. Its continuous serving, OpenAI-compatible chat endpoint,
  and structured-output support fit the shared-appliance workload. Promotion requires each task to
  pass its task-specific deterministic metrics and validation plus blinded human review for
  semantic outputs; structural validity alone is not evidence of useful model behavior.
- Ollama is the planned fallback worker. Before it is eligible for a task, it must pass the same
  task-specific acceptance independently. Qualification pins the exact Ollama package or container,
  dependency set, model artifact content digest rather than a mutable tag, and complete serving
  configuration. It uses an already-local approved model and runs with cloud access disabled
  (`OLLAMA_NO_CLOUD=1`). Protocol similarity alone is not evidence of semantic or privacy
  compatibility.
- LM Studio and llama.cpp are no longer supported production-worker targets. Existing deployment
  files and compatibility evidence remain until an accepted cutover removes their operational use,
  but no new application client should bind to either runtime.
- The gateway owns worker selection, health, and fallback. Email Watcher, Document Summarizer, and
  later applications submit task requirements and never select vLLM, Ollama, a model artifact, or
  a fallback order.
- There is no cloud fallback. If neither approved local worker can serve a task, the gateway returns
  the existing bounded availability error and the application preserves its standalone behavior.
- Worker listeners are gateway-private: bind them to gateway-host loopback or a local socket, or
  enforce equivalent host/network isolation. Client computers must reach only the gateway and must
  not be able to connect directly to either worker endpoint.

Fallback is fail-closed and identity-preserving:

1. A new request may use Ollama only when policy marks the primary unavailable before that work is
   admitted and Ollama is healthy and qualified for the same task requirements.
2. An ambiguous or in-flight vLLM failure remains unresolved on its original gateway request and
   worker-attempt identity. The gateway must recover the primary's authoritative result, prove that
   the primary never accepted the request, or confirm cancellation before Ollama may execute it.
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

Warm versus cold standby is deliberately not fixed here. The primary and fallback may require
different model artifacts, and keeping both resident may exceed the appliance's usable VRAM. The
capacity proof decides whether Ollama stays warm on separate hardware, uses a smaller approved
lane, or starts only after vLLM is stopped.

On 2026-09-01, the development machine's existing environment reported vLLM 0.16.0 and its Ollama
deployment was available. That is installation evidence only, not workload or failover proof. The
selected Ollama Qwen model is stored as GGUF, while current vLLM documentation describes GGUF
support as experimental and under-optimized. The vLLM proof must select and pin an appropriate
supported model artifact plus the exact vLLM package/container provenance and serving
configuration rather than assuming the installed package or Ollama blob is the production
deployment.

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
identity and receive the same retained output forever. Email Watcher's current generic `ModelError`
path retries such failures, so gateway cutover is blocked until the client maps this case to the
terminal rejection plus explicit requeue behavior.

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
- both current apps need additive gateway adapters and health states;
- the gateway becomes shared infrastructure that requires backup/update/recovery procedures;
- sustained-load sizing and model-lane promotion need evidence rather than guesses.

## Explicit non-goals for v0

- Connect changes or cross-machine app discovery;
- workflow engine, arbitrary agents, automatic email replies, or irreversible model actions;
- cloud accounts, remote execution over the public internet, multi-organization tenancy, or RBAC;
- chat UI, streaming tokens, async callbacks, or durable distributed job queues;
- model download UI, GPU installers, marketplace, billing, licensing, or analytics dashboard;
- attachment/vision transport before the text proof is accepted;
- selecting vLLM, Ollama, a model, quantization, fallback order, or lane count in the application
  contract.

## First implementation proof after acceptance

Build one gateway process with vLLM primary, planned Ollama fallback, and two
administrator-allowed task IDs: `email.analyze@1` and `document.chunk.summarize@1`. Prove, with
synthetic content only:

1. paired Email Watcher and Document Summarizer clients can authenticate over verified HTTPS;
2. neither request contains a model ID;
3. the exact vLLM package or container, model artifact, runtime dependencies, and serving
   configuration are pinned and reproducible;
4. the exact Ollama package or container, dependency set, model artifact content digest, cloud-off
   environment, and serving configuration are pinned and reproducible;
5. both worker endpoints are unreachable from a client-network machine while the authenticated
   gateway remains reachable;
6. task policy selects vLLM without exposing that choice to either application;
7. both tasks pass their deterministic acceptance metrics and validators plus blinded human review
   of semantic output before promotion;
8. concurrent mixed requests complete without starvation at the admitted limit;
9. revoked, wrong-scope, oversized, redirected, proxied, and plaintext requests fail closed;
10. Ollama runs with `OLLAMA_NO_CLOUD=1`, uses the pinned already-local model, and passes independent
   task acceptance before becoming eligible;
11. making vLLM unavailable before admission moves an Ollama-qualified task to
   degraded-but-available service, while an unqualified fallback task remains unavailable;
12. an in-flight or ambiguous primary failure remains unresolved until the primary result is
    recovered, non-acceptance is proven, or cancellation is confirmed; only then may the same
    request proceed without duplicate worker execution;
13. request IDs accept only canonical URL-segment-safe UUIDv4 text, and mismatched or encoded
    acknowledgement identifiers fail closed;
14. a different valid credential cannot acknowledge or delete another credential's result, and the
    owning credential can still retrieve the protected result afterward;
15. a lost response returns the protected result to the same credential/request after gateway
    restart, and the authenticated acknowledgement operation atomically deletes that result content
    while retaining its metadata tombstone; exact repeats are idempotent, an acknowledged tombstone
    wins over later expiry, and conflicting dispositions fail closed;
16. an expired request terminalizes any in-flight attempt, never dispatches again, discards late
    output without recreating retained content, returns permanent `request_expired`, and permits
    only an explicit new-identity requeue;
17. an application-validation rejection becomes terminal for its original identity, is
    acknowledged to release the protected result, and permits only explicit new-identity requeue;
18. authentication, authorization, request-contract, and output-validation failures do not trigger
    fallback;
19. stopping the gateway degrades only model-dependent actions in both applications;
20. Connect discovery and each application's private persistence remain unchanged.

Do not implement administrator UI, auto-discovery, additional runtime families, vision, or
production cutover in that proof.

## Runtime references

- vLLM OpenAI-compatible server and security boundary:
  https://docs.vllm.ai/en/latest/serving/online_serving/openai_compatible_server/
- vLLM structured outputs:
  https://docs.vllm.ai/en/latest/features/structured_outputs/
- vLLM GGUF support status:
  https://docs.vllm.ai/en/latest/features/quantization/gguf/
- Ollama OpenAI compatibility and structured outputs:
  https://docs.ollama.com/api/openai-compatibility and
  https://docs.ollama.com/capabilities/structured-outputs
