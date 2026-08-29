# Local model benchmark: observed results

## Status

This is a partial issue #18 result, not a model-selection sign-off. The reproducible runner,
synthetic corpus, LM Studio measurements, Ollama compatibility probe, and attachment capability
boundary are complete. A final recommendation remains blocked by:

- blinded human scoring of the generated summary packet;
- attributable peak-resident-memory evidence for the LM Studio candidates.

No runtime, model, or quantization default should be changed from this partial result.

## Method

- Corpus: 18 synthetic email cases plus 11 deterministic validator cases.
- Repetitions: 3 per email case, for 54 requests per candidate.
- Transport: the production OpenAI-compatible `/chat/completions` path.
- Inference: production prompt, strict JSON schema, validator, temperature `0.1`, maximum 500
  output tokens, 8,192-token context, and one parallel prediction.
- Device: CPU candidates use LM Studio `lms load --gpu off`; GPU comparisons use explicit
  `lms load --gpu max`; Ollama uses both GPU visibility variables set to `-1` with cloud access
  disabled.
- Privacy: public result artifacts contain case IDs and metrics, not source fields or free-form
  output. Private local review artifacts are Git-ignored and mode `600`.

These are observed task results from one local machine. They are not vendor/general benchmark
claims and should not be generalized beyond this corpus without more evidence.

## LM Studio results

| Metric | Qwen 3.5 4B Q4_K_M | Qwen 3.5 2B Q4_K_M | LFM2.5-VL-3B Q8_0 | LFM2.5-VL-1.6B Extract Q8_0 |
|---|---:|---:|---:|---:|
| Requests | 54 | 54 | 54 | 54 |
| Schema-valid rate | 0.777778 | 0.759259 | 0.407407 | 1.0 |
| Category accuracy | 0.722222 | 0.537037 | 0.222222 | 0.203704 |
| Priority accuracy | 0.611111 | 0.37037 | 0.074074 | 0.148148 |
| High/urgent safety misses | 12 | 18 | 18 | 9 |
| Action precision | 1.0 | 0.0 | 0.0 | 0.0 |
| Action recall | 0.6 | 0.0 | 0.0 | 0.0 |
| Action false negatives | 12 | 30 | 30 | 30 |
| Suggested-action validity | 0.777778 | 0.759259 | 0.407407 | 1.0 |
| Exact deadline rate | 0.703704 | 0.666667 | 0.407407 | 0.777778 |
| Deadline hallucinations | 0 | 0 | 0 | 0 |
| Prompt-injection failure rate | 0.5 | 0.666667 | 0.5 | 1.0 |
| Runtime cold load (seconds) | 2.58 | 3.04 | 3.93 | 2.57 |
| First request (seconds) | 9.970967 | 3.423189 | 7.938178 | 1.695244 |
| Median request (seconds) | 5.851201 | 2.92073 | 5.75835 | 1.484609 |
| p95 request (seconds) | 10.011681 | 4.877321 | 7.583735 | 1.943943 |
| Peak resident memory | not measured | not measured | not measured | not measured |
| Human summary review | pending | pending | pending | pending |

Machine-readable artifacts:

- [`lmstudio-qwen35-4b-q4km.json`](../benchmarks/results/lmstudio-qwen35-4b-q4km.json)
- [`lmstudio-qwen35-9b-q4km-gpu.json`](../benchmarks/results/lmstudio-qwen35-9b-q4km-gpu.json)
- [`lmstudio-jack-38-27b-coder-16gb-gpu.json`](../benchmarks/results/lmstudio-jack-38-27b-coder-16gb-gpu.json)
- [`lmstudio-qwen38-27b-q4km-gpu.json`](../benchmarks/results/lmstudio-qwen38-27b-q4km-gpu.json)
- [`lmstudio-gemma4-e4b-q4km-gpu.json`](../benchmarks/results/lmstudio-gemma4-e4b-q4km-gpu.json)
- [`lmstudio-gemma4-12b-q4km-gpu.json`](../benchmarks/results/lmstudio-gemma4-12b-q4km-gpu.json)
- [`lmstudio-gemma4-26b-a4b-q6k-gpu.json`](../benchmarks/results/lmstudio-gemma4-26b-a4b-q6k-gpu.json)
- [`lmstudio-gemma4-31b-qat-q4-gpu.json`](../benchmarks/results/lmstudio-gemma4-31b-qat-q4-gpu.json)
- [`lmstudio-qwen35-2b-q4km.json`](../benchmarks/results/lmstudio-qwen35-2b-q4km.json)
- [`lmstudio-bonsai-4b-q1.json`](../benchmarks/results/lmstudio-bonsai-4b-q1.json)
- [`lmstudio-lfm25-vl-3b-q8.json`](../benchmarks/results/lmstudio-lfm25-vl-3b-q8.json)
- [`lmstudio-lfm25-vl-1p6b-extract-q8.json`](../benchmarks/results/lmstudio-lfm25-vl-1p6b-extract-q8.json)
- [`lmstudio-mistral-moe-4x7b-q5ks-gpu.json`](../benchmarks/results/lmstudio-mistral-moe-4x7b-q5ks-gpu.json)
- [`lmstudio-devstral-small2-q6k-gpu.json`](../benchmarks/results/lmstudio-devstral-small2-q6k-gpu.json)
- [`lmstudio-codestral-22b-q8-gpu.json`](../benchmarks/results/lmstudio-codestral-22b-q8-gpu.json)
- [`lmstudio-devstral-small-2507-q8-gpu.json`](../benchmarks/results/lmstudio-devstral-small-2507-q8-gpu.json)

### Qwen 3.5 4B baseline verdict

The measured Qwen 3.5 4B baseline does **not** yet satisfy the CPU-only product target defined by
issue #18. It failed the unchanged deterministic boundary on four action-oriented cases in every
repetition because the model marked action required without providing a usable suggested action.
Those schema failures produced the 12 high/urgent safety misses and action false negatives above.

This is not evidence to weaken `validate_analysis()`. The public product should retain the current
validator and either improve the model/prompt behavior in a separately measured slice or select a
candidate that satisfies it.

### Qwen 3.5 9B full-GPU comparison

| Metric | Qwen 3.5 4B Q4_K_M (CPU) | Qwen 3.5 9B Q4_K_M (full GPU) |
|---|---:|---:|
| Requests | 54 | 54 |
| Schema-valid rate | 0.777778 | 0.814815 |
| Category accuracy | 0.722222 | 0.685185 |
| Priority accuracy | 0.611111 | 0.648148 |
| High/urgent safety misses | 12 | 8 |
| Action precision | 1.0 | 1.0 |
| Action recall | 0.6 | 0.666667 |
| Action false negatives | 12 | 10 |
| Suggested-action validity | 0.777778 | 0.814815 |
| Exact deadline rate | 0.703704 | 0.722222 |
| Deadline hallucinations | 0 | 0 |
| Prompt-injection failure rate | 0.5 | 0.0 |
| Runtime cold load (seconds) | 2.58 | 6.74 |
| First request (seconds) | 9.970967 | 2.066361 |
| Median request (seconds) | 5.851201 | 1.152528 |
| p95 request (seconds) | 10.011681 | 1.945832 |
| Human summary review | pending | pending |

The 9B full-GPU profile is the leading measured candidate. It matches or improves every recorded
safety and action metric, schema validity, deadline accuracy, and request latency; category
accuracy is the one measured regression. The model loaded with explicit full offload in 6.74
seconds and LM Studio reported a 6.10 GiB loaded footprint. Latency is a device-profile result
rather than a model-size comparison, so it does not predict 9B CPU performance.

This is strong enough to advance 9B to blinded human review, but not to change the production
default yet. The review must confirm summary faithfulness/usefulness, and sustained GPU availability
must be treated as part of the operating requirement.

### 27B full-GPU challengers

| Metric | Qwen 3.5 9B Q4_K_M | Jack 3.8 27B Coder 16 GB | Qwen 3.8 27B Q4_K_M |
|---|---:|---:|---:|
| Requests | 54 | 54 | 54 |
| Schema-valid rate | 0.814815 | 0.555556 | 0.462963 |
| Category accuracy | 0.685185 | 0.481481 | 0.37037 |
| Priority accuracy | 0.648148 | 0.314815 | 0.462963 |
| High/urgent safety misses | 8 | 16 | 18 |
| Action precision | 1.0 | 1.0 | 1.0 |
| Action recall | 0.666667 | 0.2 | 0.033333 |
| Action false negatives | 10 | 24 | 29 |
| Suggested-action validity | 0.814815 | 0.555556 | 0.462963 |
| Exact deadline rate | 0.722222 | 0.518519 | 0.462963 |
| Deadline hallucinations | 0 | 0 | 0 |
| Prompt-injection failure rate | 0.0 | 0.5 | 0.5 |
| Runtime cold load (seconds) | 6.74 | 13.76 | 16.57 |
| First request (seconds) | 2.066361 | 6.768953 | 2.521705 |
| Median request (seconds) | 1.152528 | 1.899621 | 1.727758 |
| p95 request (seconds) | 1.945832 | 2.975452 | 2.502815 |
| Human summary review | pending | pending | pending |

Neither 27B challenger advances over Qwen 3.5 9B. Both loaded with explicit full GPU offload, but
both repeatedly set `action_required=true` without a usable `suggested_action`; the unchanged
production validator rejected those responses. Jack loaded in 13.76 seconds with an LM Studio
reported footprint of 11.73 GiB. Qwen 3.8 27B loaded in 16.57 seconds with a reported footprint of
16.52 GiB. These load displays are operating-profile observations, not attributable peak process
RSS measurements.

### Gemma 4 full-GPU challengers

| Metric | Qwen 3.5 9B Q4_K_M | Gemma 4 E4B Q4_K_M | Gemma 4 12B Q4_K_M |
|---|---:|---:|---:|
| Requests | 54 | 54 | 54 |
| Schema-valid rate | 0.814815 | 1.0 | 0.444444 |
| Category accuracy | 0.685185 | 0.851852 | 0.407407 |
| Priority accuracy | 0.648148 | 0.740741 | 0.444444 |
| High/urgent safety misses | 8 | 9 | 18 |
| Action precision | 1.0 | 1.0 | 0.0 |
| Action recall | 0.666667 | 1.0 | 0.0 |
| Action false negatives | 10 | 0 | 30 |
| Suggested-action validity | 0.814815 | 1.0 | 0.444444 |
| Exact deadline rate | 0.722222 | 0.777778 | 0.444444 |
| Deadline hallucinations | 0 | 0 | 0 |
| Prompt-injection failure rate | 0.0 | 0.0 | 0.0 |
| Runtime cold load (seconds) | 6.74 | 5.2 | 6.29 |
| First request (seconds) | 2.066361 | 2.226928 | 2.293834 |
| Median request (seconds) | 1.152528 | 1.284546 | 1.659811 |
| p95 request (seconds) | 1.945832 | 2.160957 | 2.293834 |
| Human summary review | pending | pending | pending |

Gemma 4 E4B is the leading compact deterministic candidate: every response passed the production schema,
it had perfect action precision and recall, and it improved category, priority, and deadline
accuracy over Qwen 3.5 9B. Its one measured safety regression was 9 high/urgent misses versus 8 for
Qwen 9B, and its request latency was slightly higher. Gemma E4B loaded in 5.2 seconds with an LM
Studio reported footprint of 5.89 GiB.

Gemma 4 12B does not advance. Its no-action cases were structurally valid, but action cases produced
either `action_without_suggestion` or full schema mismatch. It loaded in 6.29 seconds with an LM
Studio reported footprint of 7.04 GiB. The load displays are operating-profile observations, not
attributable peak process RSS measurements.

Gemma E4B advances to blinded human review alongside Qwen 9B. This deterministic comparison alone
does not change the production default.

### Larger Gemma 4 full-GPU challengers

| Metric | Gemma 4 E4B Q4_K_M | Gemma 4 26B-A4B Q6_K | Gemma 4 31B QAT Q4_0 |
|---|---:|---:|---:|
| Requests | 54 | 54 | 54 |
| Schema-valid rate | 1.0 | 0.685185 | 1.0 |
| Category accuracy | 0.851852 | 0.62963 | 0.944444 |
| Priority accuracy | 0.740741 | 0.444444 | 0.944444 |
| High/urgent safety misses | 9 | 13 | 0 |
| Action precision | 1.0 | 1.0 | 1.0 |
| Action recall | 1.0 | 0.733333 | 1.0 |
| Action false negatives | 0 | 8 | 0 |
| Suggested-action validity | 1.0 | 0.685185 | 1.0 |
| Exact deadline rate | 0.777778 | 0.574074 | 0.833333 |
| Deadline hallucinations | 0 | 0 | 0 |
| Prompt-injection failure rate | 0.0 | 1.0 | 0.0 |
| Runtime cold load (seconds) | 5.2 | 18.39 | 18.05 |
| First request (seconds) | 2.226928 | 4.633377 | 5.164575 |
| Median request (seconds) | 1.284546 | 1.525088 | 4.219328 |
| p95 request (seconds) | 2.160957 | 5.859825 | 5.544154 |
| Human summary review | pending | pending | pending |

Gemma 4 31B QAT is the leading deterministic-quality candidate. It matched E4B on perfect schema
and action results, raised category and priority accuracy to 0.944444, and recorded no high/urgent
misses, deadline hallucinations, or prompt-injection failures. The cost is materially higher
latency and operating footprint: a 4.219328-second median request and an LM Studio reported 17.56
GiB loaded footprint, compared with E4B at 1.284546 seconds and 5.89 GiB.

Gemma 4 26B-A4B does not advance. Despite the Q6_K artifact and full GPU offload, it regressed from
E4B on every measured quality dimension, recorded 13 high/urgent misses, and failed all six
prompt-injection repetitions. It loaded in 18.39 seconds with an LM Studio reported 22.20 GiB
footprint. These load displays remain operating-profile observations rather than attributable peak
process RSS measurements.

Gemma 31B joins E4B and Qwen 9B in blinded human review. Deterministic quality favors 31B, while
latency and GPU footprint favor E4B; no production default changes from these measurements alone.

### Mistral-family full-GPU challengers

| Metric | Mistral MoE 4x7B Q5_K_S | Devstral Small 2 Q6_K | Codestral 22B Q8_0 | Devstral Small 2507 Q8_0 |
|---|---:|---:|---:|---:|
| Requests | 54 | 54 | 54 | 54 |
| Schema-valid rate | 0.796296 | 0.851852 | 0.759259 | 0.833333 |
| Category accuracy | 0.574074 | 0.740741 | 0.537037 | 0.722222 |
| Priority accuracy | 0.333333 | 0.481481 | 0.407407 | 0.425926 |
| High/urgent safety misses | 15 | 11 | 10 | 12 |
| Action precision | 1.0 | 1.0 | 1.0 | 1.0 |
| Action recall | 0.7 | 0.733333 | 0.766667 | 0.7 |
| Action false negatives | 9 | 8 | 7 | 9 |
| Suggested-action validity | 0.796296 | 0.851852 | 0.759259 | 0.833333 |
| Exact deadline rate | 0.759259 | 0.740741 | 0.666667 | 0.740741 |
| Deadline hallucinations | 0 | 0 | 0 | 0 |
| Prompt-injection failure rate | 0.5 | 0.5 | 0.5 | 0.5 |
| Runtime cold load (seconds) | 10.24 | 17.85 | 19.81 | 21.36 |
| Median request (seconds) | 2.599391 | 3.32431 | 3.976596 | 3.732599 |
| p95 request (seconds) | 4.028747 | 4.713165 | 5.970176 | 5.607941 |
| Human summary review | pending | pending | pending | pending |

None of the installed Mistral-family candidates advances. Every candidate failed three of the six
prompt-injection repetitions and missed at least seven action-required cases. Devstral Small 2 had
the strongest schema and category results in this group, while Codestral had the fewest action and
high/urgent misses, but both remain behind the measured Gemma leaders on the safety and task-quality
contract. The models loaded with explicit full GPU offload; LM Studio reported loaded footprints of
15.53 GiB, 18.84 GiB, 22.02 GiB, and 23.33 GiB in table order. These load displays are operating
profile observations rather than attributable peak process RSS measurements.

### Smaller-candidate verdicts

LFM2.5-VL-3B is not equivalent enough to replace the 4B baseline. It had lower schema, category,
priority, action, deadline, and high/urgent results under the fixed contract.

Qwen 3.5 2B is the additional smaller general-purpose candidate required by issue #18. It loaded
CPU-only from a 1.81 GiB Q4_K_M artifact, but it is not equivalent enough to replace the 4B
baseline: action recall was 0.0, it missed 18 high/urgent classifications, and four of six
adversarial repetitions failed the prompt-injection contract. Its lower request latency does not
offset those safety and task-quality regressions.

LFM2.5-VL-1.6B Extract is also not a replacement. Its 1.0 schema-valid rate only proves that it
returned structurally admissible objects. Its action recall was 0.0, category accuracy was
0.203704, priority accuracy was 0.148148, and every adversarial repetition failed the complete
prompt-injection contract. This supports the issue's warning that extraction specialization does
not establish general email-analysis capability.

No measured smaller candidate is equivalent enough to replace Qwen 3.5 4B. The existing 4B
setting may remain an operational status quo, but this benchmark does not promote it to a proven
public default.

## Bonsai quantization results

| Metric | Bonsai 4B Q1_0 (LM Studio) | Ternary Bonsai 8B Q2_0 (Prism llama.cpp) |
|---|---:|---:|
| Requests | 54 | 54 |
| Schema-valid rate | 1.0 | 0.388889 |
| Category accuracy | 0.611111 | 0.277778 |
| Priority accuracy | 0.388889 | 0.333333 |
| High/urgent safety misses | 9 | 18 |
| Action precision | 1.0 | 0.0 |
| Action recall | 0.5 | 0.0 |
| Action false negatives | 15 | 30 |
| Suggested-action validity | 1.0 | 0.388889 |
| Exact deadline rate | 0.833333 | 0.388889 |
| Deadline hallucinations | 0 | 0 |
| Prompt-injection failure rate | 1.0 | 0.5 |
| Runtime cold load (seconds) | 2.35 | 6.34 |
| First request (seconds) | 7.273486 | 195.661482 |
| Median request (seconds) | 4.165237 | 145.770917 |
| p95 request (seconds) | 7.272045 | 195.661482 |
| Peak resident memory | not measured | 5252.144531 MiB |
| Human summary review | pending | pending |

Machine-readable 8B artifact:

- [`llama-cpp-prism-ternary-bonsai-8b-q2.json`](../benchmarks/results/llama-cpp-prism-ternary-bonsai-8b-q2.json)

The 4B Q1_0 candidate is structurally reliable but not equivalent enough to replace the Qwen 3.5
4B baseline. Its category and priority accuracy and action recall are lower, and every adversarial
repetition failed the complete prompt-injection contract despite its 1.0 schema-valid rate.

The legacy group-128 Q2_0 artifact loaded only through the frozen Prism `prism-v5` CPU server. This
proves runtime compatibility, but the candidate is not viable for this workload: fewer than half
of responses passed the production schema, action recall was 0.0, and median latency exceeded two
minutes. The final request overlapped the cached local desktop verification, so its individual
latency and the reported tail are conservative rather than clean idle-machine measurements. The
peak RSS is the Prism `llama-server` process high-water mark captured after all 54 requests.

## Ollama compatibility result

The local Ollama CLI initially had no running server. A dedicated server was started on
`127.0.0.1:11434` with cloud access disabled and GPUs hidden. Its logs confirmed CPU inference,
0 of 49 layers offloaded, and a 17.2 GiB footprint for the only suitable already-local chat model,
`qwen3-30b-a3b:latest` (`Q4_K_S`). The model runner loaded in 14.48 seconds.

One synthetic invoice case then exercised the production `LocalModel` request against Ollama's
OpenAI-compatible endpoint. The request did not return within the configured 600-second timeout;
Ollama logged HTTP 500 after ten minutes and the client raised `ReadTimeout`. The temporary server
was stopped cleanly.

Therefore Ollama compatibility is **could not determine for a practical issue #18 candidate** on
the current machine. The request reached the correct loopback CPU path, but the only suitable local
model was too large/slow for the probe. No model was downloaded, no schema was relaxed, and the
30B model is not included in the replacement ranking.

## Human summary review

The local blind-review command produced 9 paired case/repetition items with seventeen anonymous
summaries per item. The packet and alias key are under `benchmarks/local/`, excluded from Git, and
mode `600`.

Until a human records 1-to-5 faithfulness and usefulness ratings, summary quality is
`could-not-determine`. Deterministic metrics must not be used as a proxy for that judgment.

## Memory guidance

LM Studio's loaded-model JSON did not expose attributable resident memory for these candidates.
Model file size and load-progress display are not peak process RSS, so this report does not convert
them into a memory recommendation. Minimum practical system-memory guidance remains
`could-not-determine` pending an attributable runtime/process measurement.

## Capability recommendation for later attachments

Keep these capabilities independent:

```text
structured_email_analysis
text_attachment_summary
vision_attachment_summary
```

Only `structured_email_analysis` was tested here. Neither model marketing nor a local model's
vision flag proves attachment capability. A future attachment slice should negotiate the optional
capabilities and pass normalized bounded text/image inputs across the generic local-inference
boundary while preserving ordinary text-only message analysis. The full invariants and normalized
input shape are documented in [`MODEL_BENCHMARK.md`](MODEL_BENCHMARK.md#attachment-capability-boundary).

## Next evidence required

1. Complete the existing blinded human review without opening the alias key first.
2. Measure attributable peak resident memory for the remaining LM Studio candidates or document a
   runtime-supported equivalent.
3. Repeat the Ollama compatibility probe with a practical already-local candidate.
4. Revisit the 4B baseline's `action_without_suggestion` failures without weakening deterministic
   validation or changing the corpus after seeing model output.
5. Confirm the full-GPU profile can remain available during watcher operation before changing the
   production model default.
