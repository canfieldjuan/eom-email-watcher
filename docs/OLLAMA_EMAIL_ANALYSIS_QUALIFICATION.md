# Ollama Email Analysis Qualification

## Status

Deterministic evidence captured on 2026-09-10. Slice 0B passes its frozen safety acceptance after
remediating prompt/input framing, but the selected profile is **not promoted** because the blinded
semantic review is still unscored. This document is the contract and evidence record for issue #72,
Slices 0A and 0B. It does not implement the inference gateway, qualify LM Studio fallback, or change
an application runtime default.

## Contract

### Root cause

Email Watcher's production `LocalModel` path can exercise Ollama through its loopback
OpenAI-compatible endpoint, but the benchmark candidate schema currently requires every run to be
CPU-only and assigns the corresponding runtime-specific CPU isolation method. A GPU-backed Ollama
run would therefore be mislabeled instead of producing trustworthy shared-profile evidence.

### Required change surface

- Generalize benchmark candidate metadata to distinguish CPU-only and GPU-backed execution while
  preserving the existing production prompt, schema, validator, privacy boundary, and CLI default.
  Record the execution device and fixed runtime-specific offload method explicitly.
- Add boundary tests for accepted runtime/device combinations and rejected mismatches.
- Run the selected immutable Ollama model artifact through the synthetic email-analysis and
  obligation-direction corpora.
- Commit only sanitized public metrics and reproducible runtime/model/hardware provenance. Keep
  source prompts and free-form model output in the ignored, mode-0600 local artifacts.
- Produce a blinded local comparison packet when a compatible baseline artifact exists. Human
  scoring remains pending until a reviewer actually records it.

### Explicit non-scope

- No inference gateway server, admission control, acknowledgement protocol, or application cutover.
- No LM Studio fallback implementation or qualification.
- No watcher configuration/default changes.
- No model download, prompt relaxation, validator relaxation, or corpus rewriting after results.
- No Document Summarizer, Invoice Processor, Connect, attachment, CPU-only, or small-model work.

### Assumptions and blockers

- The already-local Ollama artifact is the only candidate exercised by this slice; its mutable
  alias is not accepted as provenance without the content digest.
- Deterministic metrics can be completed autonomously. A human semantic verdict cannot be claimed
  until a blinded reviewer has scored the generated packet.
- The running Ollama service must remain loopback-only with cloud access disabled during evidence
  capture.
- A currently shared/resident model may make cold-load time unavailable. Record `null` rather than
  reuse an older measurement or infer load time from end-to-end latency.

### Contract revision

Process inspection showed no `CUDA_VISIBLE_DEVICES` override on the running Ollama server while
`ollama ps` reported 100% GPU residency. The GPU method is therefore
`ollama-runtime-managed-gpu`, not the stale `ollama-cuda-visible-devices` wording found in parallel
benchmark work. The required surface is unchanged; this revision prevents false provenance.

### Verification plan

- Focused unit tests for benchmark candidate device/method validation and output privacy.
- Ruff on changed Python files.
- Validation of both committed synthetic corpora.
- Live benchmark runs through `LocalModel` against Ollama's loopback endpoint.
- Inspection of the sanitized artifacts, model digest, runtime configuration, and GPU residency.
- Full Email Watcher unit gate: GitHub only.

## Slice 0B remediation contract

### Root cause

The qualified model is receiving current sender text and quoted history inside one undifferentiated
`body` field. The system prompt tells the model to distinguish them, but the input framing does not
identify a quoted block even when the source contains a strong reply delimiter. That leaves a quoted
invoice date available for adoption as if it were part of the current request.

The prompt also names the four priority labels without defining their decision boundaries. In the
captured failures, the model consistently chose `normal` for no-action mail that the corpus defines
as `low`, and for explicit deadline/access requests that the corpus defines as `high`. Finally,
`automated_notice` is described as a kind of no-action message without stating that a human-authored
payment confirmation remains `informational`.

These are prompt/input-contract defects. They are not evidence that the frozen labels should change,
and they cannot be repaired safely by rewriting model labels after inference without the source
semantics.

### Required change surface

- Split only strongly delimited quoted history from the current message before constructing the
  model prompt. Send the two portions as separately named untrusted fields while preserving the
  original text and order within each portion. If no recognized delimiter exists, keep the entire
  body as current text and use no quoted-history value.
- Define a mailbox-owner priority ladder: `urgent` for explicit immediate material risk, `high` for
  explicit near-term deadlines or operational/access changes requiring prompt response, `normal`
  for non-immediate human action, and `low` for messages requiring no mailbox-owner action.
- Reserve `automated_notice` for machine-generated notices or receipts. Human-authored status and
  payment confirmations that require no action are `informational`.
- Tell the model to summarize the legitimate message purpose without reproducing embedded attempts
  to control the analysis.
- Add focused tests for quote partitioning, prompt field separation, priority boundaries, human
  confirmation categorization, and the unchanged strict schema/action validator.
- Re-run both frozen corpora through the exact pinned Ollama profile and replace the public sanitized
  result artifacts only with evidence from that run. Regenerate the ignored blinded review packet.

The frozen corpus files and expected labels are identified by these SHA-256 values:

- `benchmarks/email-analysis-v1.json`:
  `b64c74f44478a776f328b3e471cf66e57e27dfacf23ec6018b0ecb52dc44ccb6`
- `benchmarks/email-obligation-v1.json`:
  `f6abe7c55e405f03ffc9afbe54b9b191e74d6e1af4d4a0a833b503811d738e56`

### Acceptance criteria

- Both corpora remain byte-identical to the hashes above.
- Schema validity, action-required precision, and action-required recall remain `1.0` in both public
  artifacts.
- Both public artifacts report zero high/urgent false negatives and zero deadline hallucinations.
- The adversarial bulletin does not reproduce its forbidden marker and retains its expected
  category, priority, action, and deadline behavior in every repetition.
- The customer invoice-copy request retains the mailbox-owner action while ignoring the unadopted
  quoted due date in every repetition.
- The human payment confirmation is `informational`, `low`, and no-action in every repetition.

### Explicit non-scope

- No corpus or expected-label edits.
- No heuristic post-inference rewriting of category, priority, action, or deadline fields.
- No public analysis schema change or evidence-field addition.
- No inference gateway, LM Studio fallback, runtime-default, attachment, Connect, scheduling, or
  notification change.
- No claim that the model profile is promoted before deterministic acceptance and human blinded
  review are both complete.

### Verification plan

- Focused model and benchmark tests only; the full Email Watcher unit gate remains GitHub-owned.
- Ruff on changed Python and test files.
- `git diff --check` and corpus SHA-256 verification.
- Exact live Ollama reruns using the reproduction commands below.
- Public artifact inspection plus regeneration of the ignored mode-0600 private/blinded evidence.

## Evidence

### Exact subject under test

- Email Watcher base: `8a969c8` (`origin/main` before Slice 0B).
- Production path: `LocalModel.analyze`, including the remediated production system/user prompt,
  strict `Analysis` JSON schema, temperature `0.1`, 500-token maximum, and `validate_analysis`
  boundary.
- Ollama version: `0.24.0`.
- Ollama binary SHA-256:
  `b2e45ade9cb754a079f74645e1183d613f582d98f7354b05f4f9a5bd81f8e0c9`.
- Model alias: `qwen3-30b-a3b:latest` (recorded only as the runtime lookup key).
- Ollama manifest digest:
  `1eda56426671cdf365913097543c2253a73c57e35b12741306689968d7f70292`.
- GGUF content SHA-256:
  `c6cf00aa338f4eaffd3503339e180bd381ced9bedb55862fd04358b97fc85c1b`.
- Upstream metadata: Qwen3 30B-A3B Instruct 2507, 30.5B parameters, Apache-2.0, Q4_K_S GGUF.
- GGUF byte size: `17,984,491,520`.
- Requested context: 8,192 tokens. The artifact advertises a 262,144-token maximum; that larger
  maximum was not exercised.

The manifest alias is mutable. Reproduction must match both full digests above, not merely the
display name.

### Serving configuration and machine

Captured from the running server process:

```text
OLLAMA_HOST=127.0.0.1:11434
OLLAMA_NO_CLOUD=1
OLLAMA_CONTEXT_LENGTH=8192
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
```

The listener was observed only on `127.0.0.1:11434`. Hardware was an NVIDIA GeForce RTX 3090 with
24,576 MiB VRAM and driver `580.173.02`, an AMD Ryzen 9 7900X host exposed as six CPUs, and 63,394
MiB system memory. Ollama reported the model as 100% GPU at context 8,192. A post-run snapshot
showed 19,031 MiB GPU memory in use; this is not a peak or an attributable model-only measurement,
so `peak_resident_memory_mib` remains `null` in the result artifacts.

The server was already shared and the model could not be proven cold before the evidence run.
`cold_start_seconds` is therefore `null`; first-request latency is reported separately and must not
be relabeled as model load time.

### Deterministic results

| Metric | Email analysis | Obligation direction |
|---|---:|---:|
| Synthetic cases x repetitions | 18 x 3 | 4 x 3 |
| Requests | 54 | 12 |
| Schema-valid rate | 1.0 | 1.0 |
| Category accuracy | 0.740741 | 1.0 |
| Priority accuracy | 0.888889 | 1.0 |
| High/urgent false negatives | 0 | 0 |
| Action precision | 1.0 | 1.0 |
| Action recall | 1.0 | 1.0 |
| Exact deadline rate | 0.777778 | 0.5 |
| Deadline hallucinations | 0 | 0 |
| Prompt-injection failures | 0 | 0 |
| Grounding failures | 0 | 0 |
| First request, seconds | 1.535999 | 1.176111 |
| Median request, seconds | 0.91311 | 1.160166 |
| p95 request, seconds | 1.511075 | 1.834465 |

Machine-readable public artifacts:

- [`ollama-qwen3-30b-a3b-q4ks-gpu.json`](../benchmarks/results/ollama-qwen3-30b-a3b-q4ks-gpu.json)
- [`ollama-qwen3-30b-a3b-q4ks-gpu-obligation.json`](../benchmarks/results/ollama-qwen3-30b-a3b-q4ks-gpu-obligation.json)

Artifact SHA-256 values are `fca7c5b3daa00270dc5ce4faacbf6243a971f0df6f86bbdc90e517914fa201bf`
and `ef2c8ce1ad2fc90af0655601699e2cf6c2f11881f53e56b0f32b0b074a90179f`,
respectively.

The 30B candidate improves on the committed Qwen 3.5 4B CPU baseline's schema-valid rate (`1.0`
versus `0.777778`), action recall (`1.0` versus `0.6`), category accuracy (`0.740741` versus
`0.722222`), deadline exactness (`0.777778` versus `0.703704`), priority accuracy (`0.888889`
versus `0.611111`), and high/urgent misses (`0` versus `12`). Its composite prompt-injection failure
rate is `0.0` versus the baseline's `0.5`.

The obligation-direction case that motivated the regression corpus preserves the correct
mailbox-owner action, ignores the unadopted quoted due date, and assigns high priority in every
repetition. The customer payment confirmation is informational, low priority, and no-action in every
repetition. The frozen corpora and expected labels were not changed.

### Blinded semantic review

The Git-ignored, mode-0600 packet `benchmarks/local/issue72-summary-review.local.json` compares this
run with the committed Qwen 3.5 4B baseline. It contains 42 shared schema-valid case/repetition
pairs and 84 unscored candidate outputs. The alias key remains separate. No human semantic score or
preference is claimed.

### Verdict

**Deterministic remediation accepted; not promoted.** Ollama can execute the exact Email Watcher
analysis contract on the selected, fully pinned 30B artifact with complete schema/action admission,
zero high/urgent misses, zero deadline hallucinations, and zero adversarial failures in both frozen
corpora. The absent attributable peak-memory/cold-load measurement remains an explicitly recorded
operational limitation. The pending blinded human review still blocks profile promotion. Gateway,
fallback, and application-cutover work must not treat this slice as final model sign-off.

### Reproduction commands

```bash
uv run eom-model-benchmark validate --corpus benchmarks/email-analysis-v1.json
uv run eom-model-benchmark validate --corpus benchmarks/email-obligation-v1.json

uv run eom-model-benchmark run \
  --corpus benchmarks/email-analysis-v1.json \
  --runtime ollama \
  --execution-device gpu \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen3-30b-a3b:latest \
  --quantization Q4_K_S \
  --context-length 8192 \
  --repetitions 3 \
  --timeout 300 \
  --output benchmarks/results/ollama-qwen3-30b-a3b-q4ks-gpu.json \
  --private-review-output benchmarks/local/ollama-qwen3-30b-a3b-q4ks-gpu-issue72.local.json

uv run eom-model-benchmark run \
  --corpus benchmarks/email-obligation-v1.json \
  --runtime ollama \
  --execution-device gpu \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen3-30b-a3b:latest \
  --quantization Q4_K_S \
  --context-length 8192 \
  --repetitions 3 \
  --timeout 300 \
  --output benchmarks/results/ollama-qwen3-30b-a3b-q4ks-gpu-obligation.json \
  --private-review-output \
    benchmarks/local/ollama-qwen3-30b-a3b-q4ks-gpu-obligation-issue72.local.json
```
