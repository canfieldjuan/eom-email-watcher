# ADR-001: On-prem inference gateway v0 boundary

**Status:** Proposed
**Date:** 2026-08-29
**Decider:** Juan Canfield
**Scope:** Contract only; no runtime or application behavior changes

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

Current direction notes disagree about the selected worker: Email Watcher Issue #13 names
llama.cpp, while current Document Summarizer notes name Ollama. This is evidence that applications
must not select a worker or model. Worker promotion remains a measured administrator deployment
decision behind the gateway.

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

- `protocol_version`, `request_id`, task ID/version, requirements, and generation payload are
  mandatory and bounded.
- The request contains no `model`, worker URL, runtime command, lane name, user role, or routing
  override.
- The gateway authorizes the task before queueing and rejects requirements unsupported by its
  current policy.
- The gateway treats prompts and content as opaque untrusted data. Task IDs control policy; prompt
  text cannot select workers or elevate limits.
- A repeated active `request_id` for the same credential and canonical request joins or reuses that
  work. Reuse with different content is rejected.

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

## Failure and scheduling contract

The gateway returns a stable bounded error envelope with `code`, `retryable`, and optional
`retry_after_seconds`; it never returns worker stack traces or prompt fragments.

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
filenames, sender/subject, attachment bytes, or application database identifiers. Temporary worker
payloads are memory-only unless an administrator explicitly enables a separately designed and
audited diagnostic mode.

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

### B. Expose llama.cpp/Ollama directly on the LAN — rejected

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
- selecting llama.cpp, Ollama, a model, quantization, or lane count in the application contract.

## First implementation proof after acceptance

Build one gateway process with one text worker and two administrator-allowed task IDs:
`email.analyze@1` and `document.chunk.summarize@1`. Prove, with synthetic content only:

1. paired Email Watcher and Document Summarizer clients can authenticate over verified HTTPS;
2. neither request contains a model ID;
3. task policy selects the worker deployment;
4. concurrent mixed requests complete without starvation at the admitted limit;
5. revoked, wrong-scope, oversized, redirected, proxied, and plaintext requests fail closed;
6. stopping the gateway degrades only model-dependent actions in both applications;
7. Connect discovery and each application's private persistence remain unchanged.

Do not implement administrator UI, auto-discovery, multiple workers, vision, or production cutover
in that proof.
