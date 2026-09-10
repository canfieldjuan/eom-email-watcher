# CPU-only local model benchmark: initial observed results

Shared GPU qualification for issue #72 is tracked separately in
[`OLLAMA_EMAIL_ANALYSIS_QUALIFICATION.md`](OLLAMA_EMAIL_ANALYSIS_QUALIFICATION.md). Nothing in that
evidence changes this report's CPU-only status or completes its pending human review.

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
- Device: LM Studio `lms load --gpu off`; Ollama server with both GPU visibility variables set to
  `-1` and cloud access disabled.
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
- [`lmstudio-qwen35-2b-q4km.json`](../benchmarks/results/lmstudio-qwen35-2b-q4km.json)
- [`lmstudio-lfm25-vl-3b-q8.json`](../benchmarks/results/lmstudio-lfm25-vl-3b-q8.json)
- [`lmstudio-lfm25-vl-1p6b-extract-q8.json`](../benchmarks/results/lmstudio-lfm25-vl-1p6b-extract-q8.json)

### Qwen 3.5 4B baseline verdict

The measured Qwen 3.5 4B baseline does **not** yet satisfy the CPU-only product target defined by
issue #18. It failed the unchanged deterministic boundary on four action-oriented cases in every
repetition because the model marked action required without providing a usable suggested action.
Those schema failures produced the 12 high/urgent safety misses and action false negatives above.

This is not evidence to weaken `validate_analysis()`. The public product should retain the current
validator and either improve the model/prompt behavior in a separately measured slice or select a
candidate that satisfies it.

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

The local blind-review command produced 19 paired case/repetition items with four anonymous
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
2. Measure attributable peak resident memory for each candidate or document a runtime-supported
   equivalent.
3. Repeat the Ollama compatibility probe with a practical already-local candidate.
4. Revisit the 4B baseline's `action_without_suggestion` failures without weakening deterministic
   validation or changing the corpus after seeing model output.
