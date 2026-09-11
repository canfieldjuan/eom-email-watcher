# Email analysis gateway cutover

## Why this slice exists

Current `main` already has an opt-in `GatewayModel`, but its inference envelope is not compatible
with the implemented gateway: it omits the required immutable `request_expires_at` field and never
acknowledges a returned result. Its tests use permissive mock handlers, so they do not prove that the
real gateway accepts the request or releases retained output.

The root cause is that the client was implemented against the earlier architectural description
before the gateway lifecycle contract became executable. The correct fix must make ordinary email
analysis use the exact request/acknowledgement lifecycle while keeping the existing durable analysis
identity, application-side validation, and loopback model path unchanged.

## Scope

- Add a whole-second UTC expiry derived from the already-durable analysis or scheduling-extraction
  reservation time. Reuse that same expiry with the same request identity.
- Validate gateway output in the application, persist the accepted analysis or permanent rejection
  first, and only then acknowledge `persisted` or `application_rejected`.
- Treat acknowledgement transport failure as cleanup failure: retain the already-durable Email
  Watcher outcome, log the bounded failure, and never rerun inference because acknowledgement was
  lost.
- Prove the client request and acknowledgement against the executable gateway contract using
  synthetic email content.
- Preserve explicit analysis requeue: a permanently rejected result remains paused until the user
  requests a new durable analysis identity.

### Acceptance criteria

1. The same durable analysis or extraction reservation produces the same canonical UUIDv4 request
   ID and whole-second UTC expiry on retry; the expiry is no more than the gateway's 900-second
   admission window when first submitted.
2. A valid gateway result is committed to the message ledger before `persisted` acknowledgement.
3. Invalid application output is committed as a permanent analysis failure before
   `application_rejected` acknowledgement.
4. A failed or lost acknowledgement cannot undo the durable application outcome or cause another
   inference submission.
5. The exact gateway request validator accepts Email Watcher's ordinary analysis schema and
   envelope, and its acknowledgement removes the retained output.
6. Loopback inference, mailbox polling, notification delivery, Connect, and calendar write behavior
   are unchanged.

## Intentional

- The request expiry is derived from `analysis_context_at`, which is written atomically with the
  request ID before body fetch or inference. A duplicate expiry column would create two durable
  sources for one immutable value.
- An acknowledgement is best-effort cleanup after the application's terminal transaction. Gateway
  expiry remains the bounded cleanup fallback if the acknowledgement cannot be delivered.
- The client continues to submit app-owned prompts and schemas; it does not select a model or worker.

## Deferred

- Scheduling extraction currently emits a nested array/reference schema outside the gateway's
  deliberately narrow `email.analyze@1` schema subset. A separate contract slice must either admit
  that bounded schema or version the task; this PR does not silently weaken gateway admission.
- Document Summarizer and Invoice Processor cutover, LM Studio fallback, appliance provisioning UI,
  credential rotation, and Windows packaging remain separate slices.

## Verification

- Focused Email Watcher gateway-model and service tests.
- Ruff format/lint over changed Python.
- Exact-current cross-repository contract proof against the merged gateway package with synthetic
  input only.
- Full Email Watcher unit gate remains GitHub-owned.

Performed locally:

- `uv run ruff check .`
- `uv run pytest -q tests/test_model.py tests/test_gateway_model.py tests/test_service.py`
- Exact-current ASGI gateway proof: health available, one synthetic worker call, encrypted output
  retained before acknowledgement, and no output retained afterward.
