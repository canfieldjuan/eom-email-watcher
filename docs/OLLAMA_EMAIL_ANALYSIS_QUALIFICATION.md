# Ollama Email Analysis Qualification

## Status

Deterministic evidence captured on 2026-09-10. The selected profile is **not promoted** because
priority/deadline failures remain and the blinded semantic review is still unscored. This document
is the contract and evidence record for issue #72, Slice 0A. It does not implement the inference
gateway, qualify LM Studio fallback, or change an application runtime default.

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

## Evidence

### Exact subject under test

- Email Watcher base: `c10a37c` (`origin/main` before this runner-only change).
- Production path: `LocalModel.analyze`, including the production system/user prompt, strict
  `Analysis` JSON schema, temperature `0.1`, 500-token maximum, and `validate_analysis` boundary.
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
| Category accuracy | 0.759259 | 0.75 |
| Priority accuracy | 0.444444 | 0.5 |
| High/urgent false negatives | 6 | 3 |
| Action precision | 1.0 | 1.0 |
| Action recall | 1.0 | 1.0 |
| Exact deadline rate | 0.777778 | 0.25 |
| Deadline hallucinations | 0 | 3 |
| Prompt-injection failures | 3 | 0 |
| Grounding failures | 0 | 0 |
| First request, seconds | 7.254158 | 1.334516 |
| Median request, seconds | 0.828843 | 1.032336 |
| p95 request, seconds | 1.240357 | 1.334516 |

Machine-readable public artifacts:

- [`ollama-qwen3-30b-a3b-q4ks-gpu.json`](../benchmarks/results/ollama-qwen3-30b-a3b-q4ks-gpu.json)
- [`ollama-qwen3-30b-a3b-q4ks-gpu-obligation.json`](../benchmarks/results/ollama-qwen3-30b-a3b-q4ks-gpu-obligation.json)

Artifact SHA-256 values are `00fb081793d96cd2a2ce1a7c202d00eabf71c172d1307f0317dc7e19197896b6`
and `2e942c0a2aba941f3699666796f6f7e5f1431c684e8963efde3a0826be3ca2dd`,
respectively.

The 30B candidate improves on the committed Qwen 3.5 4B CPU baseline's schema-valid rate (`1.0`
versus `0.777778`), action recall (`1.0` versus `0.6`), category accuracy (`0.759259` versus
`0.722222`), deadline exactness (`0.777778` versus `0.703704`), and high/urgent misses (`6` versus
`12`). Its priority accuracy is worse (`0.444444` versus `0.611111`), and the composite
prompt-injection failure rate is unchanged at `0.5`.

The obligation-direction case that motivated the regression corpus did not reverse who owed whom:
the generated action told the mailbox owner to provide the requested invoice copies and access-card
numbers. It nevertheless adopted a quoted date as a deadline in all three repetitions and assigned
normal rather than high priority. A customer payment confirmation preserved `action_required=false`
but was categorized as an automated notice with normal rather than low priority. These are real
contract failures, not reasons to rewrite the gold labels after observing the output.

### Blinded semantic review

The Git-ignored, mode-0600 packet `benchmarks/local/issue72-summary-review.local.json` compares this
run with the committed Qwen 3.5 4B baseline. It contains 42 shared schema-valid case/repetition
pairs and 84 unscored candidate outputs. The alias key remains separate. No human semantic score or
preference is claimed.

### Verdict

**Not promoted.** Ollama can execute the exact Email Watcher analysis contract on the selected,
fully pinned 30B artifact with strong warm latency and complete schema/action admission. The
remaining priority safety misses, deadline failures, unchanged adversarial failure rate, absent
attributable peak-memory/cold-load measurement, and pending blinded review block issue #72 profile
promotion. Gateway, fallback, and application-cutover work must not treat this slice as a model
sign-off.

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
