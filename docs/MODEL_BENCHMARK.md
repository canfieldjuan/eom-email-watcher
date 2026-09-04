# Local model benchmark

The synthetic benchmark path measures the production email-analysis prompt and schema against a
finite corpus. Synthetic `run` commands never contact Gmail, download attachments, or send
benchmark content to a non-loopback endpoint. The explicitly separate `prepare-inbox` workflow
does read selected messages from the active Gmail account to create an owner-private corpus. This is
an operator/developer tool, not part of normal watcher execution.

The current observed comparison and its unresolved acceptance items are recorded in
[`MODEL_BENCHMARK_RESULTS.md`](MODEL_BENCHMARK_RESULTS.md).

The benchmark answers issue #18 in two stages:

1. deterministic model/schema/latency measurements produced by the runner; and
2. a blinded human review of summary and suggested-action faithfulness and usefulness.

Do not recommend a model until both stages are complete. String similarity is not a substitute for
the human analysis review.

New candidate testing uses the isolated Ollama procedure below. The LM Studio procedures remain
only to reproduce the historical artifacts already committed by issue #18; they are not the backend
for new benchmark runs.

## Privacy boundary

`benchmarks/email-analysis-v1.json` is synthetic and uses only IANA-reserved example email
domains. Corpus loading rejects any email-like value outside `example.com`, `example.net`, and
`example.org`.

Every run has two outputs:

- a public result containing model/runtime metadata, aggregate metrics, case IDs, pass/fail counts,
  latency, and no source email fields or free-form model output; and
- a mode-0600 local review file containing source text and model analyses. Put these files under
  `benchmarks/local/`, which Git ignores.

The runner rejects collisions between every input and output path. It also requires every
content-bearing output filename to end in `.local.json`; repository-local private outputs must
resolve under the Git-ignored `benchmarks/local/` directory.

The public result deliberately records only exception class names. Model response/error text is not
copied into it. A private corpus may be supplied by path, but neither that corpus nor its local
review artifacts belong in Git, CI output, issue comments, or PR comments.

## Build a private real-inbox corpus

The committed synthetic corpus remains the stable regression control. For task-fidelity testing,
create a second corpus from recent Inbox messages sent by the addresses in the configured watcher
allowlist. This command resolves the currently active account through the normal mailbox registry,
requires that account to be Gmail, and uses its account-specific read-only token. It fetches body
text and attachment filenames through the production MIME path and asks an explicitly selected
loopback model for draft labels. It does not download attachment bytes or print source content.

```bash
uv run eom-model-benchmark prepare-inbox \
  --base-url http://127.0.0.1:11434/v1 \
  --model <local-ollama-model> \
  --limit 20 \
  --output benchmarks/local/inbox-label-draft.local.json
```

The output is mode `0600`, Git-ignored, and contains real private email content. Draft labels are a
review aid, not ground truth. Open the file locally, review every `expected` object against its
source email, correct category, priority, action requirement, and deadline fields, then change the
top-level `labels_reviewed` value to `true`. Do not paste the file or its contents into chat, issues,
pull requests, CI logs, or cloud tools.

Finalize the reviewed draft into the corpus consumed by the normal runner:

```bash
uv run eom-model-benchmark finalize-inbox \
  --input benchmarks/local/inbox-label-draft.local.json \
  --output benchmarks/local/inbox-gold.reviewed.local.json

uv run eom-model-benchmark validate \
  --corpus benchmarks/local/inbox-gold.reviewed.local.json
```

Both preparation and finalization refuse to overwrite existing files. Real addresses are accepted
only from a private `*.local.json` corpus; the reserved-example-domain guard remains mandatory for
committed/public corpora. Run each candidate against both corpora and report the synthetic and real
results separately rather than combining their scores.

## Run the short- and long-document summary benchmark

`benchmarks/document-summary-v1.json` defines four controlled synthetic documents. The runner
expands compact, deterministic filler around fact-bearing segments to exactly 650, 3,000, 9,000,
and 18,000 words. This keeps the committed corpus reviewable while testing retrieval from prompts
that cross the normal 8,192-token email context.

Start the local Ollama process with a context large enough for the suite and cloud access disabled.
The command below assumes the selected model is already installed; it does not download a model.

```bash
CUDA_VISIBLE_DEVICES=GPU-YOUR-STABLE-UUID \
OLLAMA_HOST=127.0.0.1:11434 \
OLLAMA_NO_CLOUD=1 \
OLLAMA_CONTEXT_LENGTH=32768 \
OLLAMA_MAX_LOADED_MODELS=1 \
OLLAMA_NUM_PARALLEL=1 \
OLLAMA_MODELS=<local-model-store> \
ollama serve

# In a second terminal, verify the model is local and fully placed on the selected GPU.
ollama list
ollama run qwen3-30b-a3b:latest ""
ollama ps

uv run eom-model-benchmark run-documents \
  --corpus benchmarks/document-summary-v1.json \
  --runtime ollama \
  --execution-device gpu \
  --base-url http://127.0.0.1:11434/v1 \
  --model qwen3-30b-a3b:latest \
  --quantization Q4_K_S \
  --context-length 32768 \
  --repetitions 3 \
  --output benchmarks/results/ollama-qwen3-30b-a3b-document-summary-q4ks-gpu.json \
  --private-review-output \
    benchmarks/local/ollama-qwen3-30b-a3b-document-summary-q4ks-gpu.local.json
```

Do not publish the run as `ollama-cuda-visible-devices` unless `ollama ps` reports the selected
model fully on GPU. The stable UUID, not a mutable device index, is the selection mechanism.

The public result records schema validity, exact-term fact recall, word-limit compliance, latency,
and the API-reported prompt-token counts without source or output text. Cold-start time remains
`null` unless it is separately measured; zero is not a substitute for an unavailable measurement.
The committed corpus has no controlled unsafe-output canary, so its public result omits the
forbidden-output metric rather than reporting an unevaluated zero. The mode-`0600` local file
contains the generated documents and summaries for human review. Exact-term recall is a controlled
retrieval check, not a substitute for judging whether a summary is faithful, safe, and useful.

## Validate the corpus

```bash
uv run eom-model-benchmark validate \
  --corpus benchmarks/email-analysis-v1.json
```

The corpus covers action/no-action, invoices versus receipts, explicit/absent/ambiguous/past
deadlines, urgent operational risk, scheduling, every stable category, body and attachment-name
prompt injection, HTML-derived text, empty and long bodies, attachment filenames, and malformed or
contradictory raw analysis at the deterministic validation boundary.

## Obligation-direction regression corpus

`benchmarks/email-obligation-v1.json` is a separate synthetic regression corpus for assigning who
must act and who owes whom. It covers a customer requesting copies of invoices overdue on the
customer's side, a vendor asking the mailbox owner to pay, a sender adopting a deadline from a
forwarded invoice, quoted invoice history, and the boundary between building-access cards and
financial-card paraphrases.

The corpus is separate so the hash and meaning of `email-analysis-v1.json` and its historical
results remain unchanged. Validate it with:

```bash
uv run eom-model-benchmark validate \
  --corpus benchmarks/email-obligation-v1.json
```

When comparing models for this failure class, run every candidate against this exact file with the
same settings and repetitions. Keep content-bearing review output under `benchmarks/local/` as
described above; do not commit real email text or local review artifacts.

## LM Studio CPU-only procedure

Use one model at a time. `--gpu off` is mandatory; LM Studio otherwise chooses its own offload.
Keep context, parallelism, temperature, prompt, schema, corpus, and repetitions constant.

The models observed locally when this procedure was written were:

| Model key | Artifact quantization | Role |
|---|---|---|
| `qwen3.5-4b` | `Q4_K_M` | required baseline |
| `qwen3.5-9b` | `Q4_K_M` | full-GPU quality challenger |
| `jack-3.8-27b-coder-16gb-vram` | custom 16 GB VRAM artifact | full-GPU coder challenger |
| `qwen/qwen3.8-27b` | `Q4_K_M` | full-GPU general-purpose challenger |
| `google/gemma-4-e4b` | `Q4_K_M` | full-GPU efficient Gemma challenger |
| `google/gemma-4-12b` | `Q4_K_M` | full-GPU Gemma size challenger |
| `google/gemma-4-26b-a4b` | `Q6_K` | full-GPU mixture-of-experts challenger |
| `google/gemma-4-31b-qat` | `Q4_0` | full-GPU Gemma quality challenger |
| `qwen3.5-2b` | `Q4_K_M` | smaller general-purpose challenger |
| `bonsai-4b` | `Q1_0` | end-to-end 1-bit challenger |
| `ternary-bonsai-8b` | legacy `Q2_0` | ternary challenger requiring Prism llama.cpp |
| `lfm2.5-vl-3b` | `Q8_0` | installed general/multimodal challenger |
| `lfm2.5-vl-1.6b-extract` | `Q8_0` | optional extraction experiment only |
| `mistral-moe-4x7b-dark-multiverse-uncensored-enhanced32-24b` | `Q5_K_S` | third-party Mistral-family mixture-of-experts challenger |
| `mistralai/devstral-small-2-2512` | `Q6_K` | full-GPU official Devstral challenger |
| `mistralai/codestral-22b-v0.1` | `Q8_0` | full-GPU official Codestral challenger |
| `mistralai/devstral-small-2507` | `Q8_0` | full-GPU official Devstral challenger |

The Extract model is not a valid general default unless it also passes the complete classification,
priority, action, deadline, adversarial, and human-summary contract.

Example baseline run:

```bash
lms load qwen3.5-4b \
  --gpu off \
  --context-length 8192 \
  --parallel 1 \
  --identifier bench-qwen35-4b \
  --yes

uv run eom-model-benchmark run \
  --corpus benchmarks/email-analysis-v1.json \
  --runtime lmstudio \
  --base-url http://127.0.0.1:1234/v1 \
  --model bench-qwen35-4b \
  --quantization Q4_K_M \
  --context-length 8192 \
  --cold-start-seconds 5.34 \
  --repetitions 3 \
  --require-auth \
  --api-token-file ~/.local/state/eom-email-watcher/lmstudio-api-token \
  --output benchmarks/results/lmstudio-qwen35-4b-q4km.json \
  --private-review-output benchmarks/local/lmstudio-qwen35-4b-q4km.local.json

lms unload bench-qwen35-4b
```

Repeat with the exact candidate model key, identifier, and quantization. The public result records
the standardized CPU-only method as `lms-load-gpu-off`. Preserve the `lms load` terminal output as
local run evidence and pass its reported load duration as `--cold-start-seconds`; do not copy
tokens or private prompts into a public artifact.

LM Studio's local CLI did not expose peak resident model memory in its loaded-model JSON on the
machine used to author this procedure. The result therefore leaves `peak_resident_memory_mib` null
rather than substituting model file size or guessing. Record a measured value only when the runtime
or an external process monitor can attribute it to the candidate process.

## LM Studio full-GPU comparison procedure

GPU measurements use the same prompt, schema, validator, corpus, context, parallelism,
temperature, and repetitions as CPU measurements. Load with explicit full offload; do not use LM
Studio's automatic ratio because that would leave the execution profile ambiguous:

```bash
lms load qwen3.5-9b \
  --gpu max \
  --context-length 8192 \
  --parallel 1 \
  --identifier bench-qwen35-9b-gpu \
  --yes

uv run eom-model-benchmark run \
  --corpus benchmarks/email-analysis-v1.json \
  --runtime lmstudio \
  --execution-device gpu \
  --base-url http://127.0.0.1:1234/v1 \
  --model bench-qwen35-9b-gpu \
  --quantization Q4_K_M \
  --context-length 8192 \
  --cold-start-seconds YOUR_MEASURED_LOAD_SECONDS \
  --repetitions 3 \
  --require-auth \
  --api-token-file ~/.local/state/eom-email-watcher/lmstudio-api-token \
  --output benchmarks/results/lmstudio-qwen35-9b-q4km-gpu.json \
  --private-review-output benchmarks/local/lmstudio-qwen35-9b-q4km-gpu.local.json

lms unload bench-qwen35-9b-gpu
```

The public artifact records `cpu_only=false` and `gpu_offload_method=lms-load-gpu-max`. The runner
accepts runtime-specific GPU profiles for LM Studio and Ollama, rejects llama.cpp GPU declarations,
and rejects mixed CPU/GPU method declarations. Quality scores remain comparable because the
evaluation contract is fixed; latency reflects the complete model-plus-device profile and must not
be attributed to model size alone.

## Prism llama.cpp CPU-only procedure

The locally observed `Ternary-Bonsai-8B-Q2_0.gguf` uses Prism's deprecated legacy group-128
`Q2_0` encoding. Current mainline llama.cpp and the moving Prism `prism` branch do not load that
artifact. Use the complete frozen `prism-v5` build pinned to the tested commit; do not mix its
libraries with another llama.cpp build. Newer `Q2_0_g64` or `PQ2_0` artifacts require a different
runtime line and are outside this result's provenance.

Build an isolated CPU-only server:

```bash
git clone --branch prism-v5 --single-branch https://github.com/PrismML-Eng/llama.cpp.git
git -C llama.cpp checkout b06f74a9ea9fa5eaf751f7d575bf766486b4ae62
cmake -S llama.cpp -B llama.cpp/build \
  -DGGML_CUDA=OFF \
  -DGGML_HIP=OFF \
  -DGGML_VULKAN=OFF \
  -DLLAMA_BUILD_SERVER=ON \
  -DLLAMA_BUILD_EXAMPLES=OFF \
  -DLLAMA_BUILD_TESTS=OFF \
  -DCMAKE_BUILD_TYPE=Release
cmake --build llama.cpp/build --target llama-server --config Release -j 4
```

Start one CPU-only prediction slot on loopback. Replace `MODEL_PATH` with the already-local GGUF;
the benchmark process never downloads a model:

```bash
llama.cpp/build/bin/llama-server \
  --model MODEL_PATH/Ternary-Bonsai-8B-Q2_0.gguf \
  --alias bench-ternary-bonsai-8b \
  --host 127.0.0.1 \
  --port 11435 \
  --ctx-size 8192 \
  --parallel 1 \
  --n-gpu-layers 0
```

Preserve the server's model-loaded duration as `--cold-start-seconds`, then use the same production
client, schema, validator, corpus, and repetitions:

```bash
uv run eom-model-benchmark run \
  --corpus benchmarks/email-analysis-v1.json \
  --runtime llama_cpp \
  --base-url http://127.0.0.1:11435/v1 \
  --model bench-ternary-bonsai-8b \
  --quantization Q2_0 \
  --context-length 8192 \
  --cold-start-seconds YOUR_MEASURED_LOAD_SECONDS \
  --repetitions 3 \
  --output benchmarks/results/llama-cpp-prism-ternary-bonsai-8b-q2.json \
  --private-review-output benchmarks/local/llama-cpp-prism-ternary-bonsai-8b-q2.local.json
```

The public artifact records `prism-llama-cpp-cpu-only`; it must not be labeled as an LM Studio or
Ollama run. Stop the temporary server after the result and keep all content-bearing output local.

## Ollama isolated procedures

Do not enable or reuse a system service that listens beyond loopback. Ollama's local API does not
require authentication, so every benchmark server must bind explicitly to `127.0.0.1`, disable
cloud execution, admit only one model and one prediction slot, and use an already-local model.

### Full-GPU procedure for new candidate testing

Select the intended GPU by the stable UUID reported by `nvidia-smi -L`, then start a disposable
server. This records the hardware-selection mechanism without coupling the benchmark to a device
index whose ordering can change:

```bash
CUDA_VISIBLE_DEVICES=GPU-YOUR-STABLE-UUID \
OLLAMA_NO_CLOUD=1 \
OLLAMA_HOST=127.0.0.1:11434 \
OLLAMA_MAX_LOADED_MODELS=1 \
OLLAMA_NUM_PARALLEL=1 \
OLLAMA_CONTEXT_LENGTH=8192 \
ollama serve
```

In a second terminal, verify the model is already local, preload it while measuring the cold load,
and require `ollama ps` to report a GPU processor before running the complete corpus:

```bash
ollama list
time ollama run YOUR_LOCAL_MODEL_ID ""
ollama ps

uv run eom-model-benchmark run \
  --corpus benchmarks/email-analysis-v1.json \
  --runtime ollama \
  --execution-device gpu \
  --base-url http://127.0.0.1:11434/v1 \
  --model YOUR_LOCAL_MODEL_ID \
  --quantization YOUR_EXACT_QUANTIZATION \
  --context-length 8192 \
  --cold-start-seconds YOUR_MEASURED_LOAD_SECONDS \
  --repetitions 3 \
  --output benchmarks/results/ollama-candidate-gpu.json \
  --private-review-output benchmarks/local/ollama-candidate-gpu.local.json

ollama stop YOUR_LOCAL_MODEL_ID
```

The public artifact records `cpu_only=false` and
`gpu_offload_method=ollama-cuda-visible-devices`. Stop the disposable server after the run. If
`ollama ps` reports CPU or mixed placement, do not publish the artifact as a full-GPU result.

### CPU-only compatibility procedure

Run a dedicated loopback server with cloud access disabled and GPUs hidden from the process. Do not
reuse an Ollama process whose device configuration is unknown.

```bash
CUDA_VISIBLE_DEVICES=-1 \
ROCR_VISIBLE_DEVICES=-1 \
OLLAMA_NO_CLOUD=1 \
OLLAMA_HOST=127.0.0.1:11434 \
ollama serve
```

In a second terminal, confirm the desired model is already local and run the same corpus through
Ollama's OpenAI-compatible loopback endpoint:

```bash
ollama list

uv run eom-model-benchmark run \
  --corpus benchmarks/email-analysis-v1.json \
  --runtime ollama \
  --base-url http://127.0.0.1:11434/v1 \
  --model YOUR_LOCAL_MODEL_ID \
  --quantization YOUR_EXACT_QUANTIZATION \
  --context-length 8192 \
  --cold-start-seconds YOUR_MEASURED_SERVER_MODEL_LOAD_SECONDS \
  --repetitions 3 \
  --output benchmarks/results/ollama-candidate.json \
  --private-review-output benchmarks/local/ollama-candidate.local.json
```

The runner uses the same `LocalModel`, prompt, strict JSON schema, and validator as production. A
runtime that cannot honor that request is recorded as schema/request failure; do not weaken the
schema for compatibility. The public result records the CPU-only method as
`ollama-gpus-hidden`. No benchmark command pulls or bundles a model.

## Blinded analysis review

After every candidate run, combine the private files into a blinded packet. Use a seed that is not
shared with the reviewer until scoring is complete.

```bash
uv run eom-model-benchmark blind \
  --input benchmarks/local/lmstudio-qwen35-4b-q4km.local.json \
  --input benchmarks/local/lmstudio-lfm25-vl-3b-q8.local.json \
  --seed LOCAL_SECRET_SEED \
  --output benchmarks/local/summary-review.local.json \
  --key-output benchmarks/local/summary-review-key.local.json
```

The review packet omits model identity and includes only case/repetition pairs for which every
candidate produced a schema-valid result. A human reviewer reads each synthetic source and scores
both the paired summaries and suggested actions from 1 to 5 for faithfulness and usefulness. The
suggested-action review is the semantic check for direction-sensitive wording such as who should
send payment; do not replace it with an ever-growing phrase denylist. Keep the alias key separate
until scoring is finished. Publish only aggregate scores and non-identifying observations in the
comparison report.

## Metrics and decision rule

The public artifact records:

- strict schema/validator success;
- category and priority accuracy, including urgent/high false negatives;
- action-required precision, recall, and false negatives;
- suggested-action structural validity;
- exact final deadline text/date and hallucinations;
- prompt-injection canary reproduction;
- unsupported-output grounding failures, reported separately from prompt injection;
- cold, median, and p95 request latency;
- peak resident memory when it is actually exposed or measured; and
- the status of blinded human summary and suggested-action review.

`forbidden_output_substrings` has one deterministic role boundary: a marker that appears in the
untrusted source fields is a prompt-injection canary; a marker absent from those fields is an
unsupported-output grounding marker. The public artifact reports those failure classes separately.
The grounding-failure aggregate uses all benchmark runs as its denominator so candidate rates stay
comparable even when individual corpora carry different numbers of grounding markers.
If a corpus contains no markers for one of those roles, the corresponding case and aggregate metric
is omitted. Absence means unevaluated; it must not be interpreted as a zero-failure result.

A smaller model may replace the 4B baseline only when it does not materially worsen schema success,
action recall, deadline exactness/hallucination, priority safety, prompt-injection resistance, or
human-rated summary and suggested-action faithfulness/usefulness. Observed corpus results must
remain separate from vendor or general benchmark claims.

## Attachment capability boundary

Model/runtime capabilities are independent flags:

```text
structured_email_analysis
text_document_summary
text_attachment_summary
vision_attachment_summary
```

`structured_email_analysis` is required for every benchmark candidate and for the current watcher.
`text_document_summary` means the model was exercised against bounded extracted document text. It
does not prove the separate normalized attachment handoff represented by `text_attachment_summary`.
The optional capabilities are not inferred merely because a model is marketed as multimodal.

A later attachment slice should pass a normalized, bounded local input across the inference
boundary, not a Gmail MIME object or runtime-specific projector field. That input should include a
stable source ID, supported media type, bounded extracted text and/or bounded local image pages,
truncation status, and page/size counts. The response should be a separate retryable attachment
analysis result; it must not overwrite or invalidate the existing message analysis.

The later slice must preserve these invariants:

- attachments remain untrusted data and cannot issue instructions;
- supported types and byte/page/text limits are explicit before extraction;
- extraction and inference remain local and use loopback-only model transport;
- raw attachment content is not added to the durable message ledger by default;
- failures are visible and retryable without corrupting message analysis; and
- a text-only installation still analyzes ordinary email when attachment understanding is absent.

Inbox priority grouping remains a separate UI slice and should consume the already stored priority;
it must not rerun inference.
