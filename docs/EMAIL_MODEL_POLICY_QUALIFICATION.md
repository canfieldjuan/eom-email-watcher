# Email model thinking policy and 9B qualification

## Decision and ownership

Operator decision (2026-10-07): Email Watcher targets **non-thinking mode** for
both the direct `LocalModel` client and the gateway worker. The selected runtime
profile owns its supported mode and chat-template projection. Do not copy Invoice
Processor's API flag into a different runtime. Shared-host ownership remains in
[ADR-0011](https://github.com/canfieldjuan/connect-contracts/blob/8e7661e2631f580f3e59f3ae9e3410c32f0bb94c/adr/0011-shared-local-model-runtime.md);
this qualification does not migrate the gateway to that host.

The deployed gateway serves the actual 9B artifact below, rather than the
application's 30B display label. Its immutable template closes an empty thinking
block before generation. Direct requests in this run select that same profile.
The template is already configured for the target; no serving configuration,
request flag, runtime default or prompt was changed during qualification.

**Answer-channel decision is separate:** preserve current content-first selection
and reasoning fallback in both clients for this comparison. Neither its
necessity nor safe removal is established by historical parsed-output metrics.
Future removal or restriction needs a separately recorded decision and channel
proof. Schema/task validators remain enforced. The installed worker's offline
mixed/empty/malformed/truncated probes demonstrate current selection, not live
absence of reasoning in gateway completions.

## Inventory before change

The recorded 30B comparator and its frozen oracle are documented in
[Ollama qualification](OLLAMA_EMAIL_ANALYSIS_QUALIFICATION.md). Its blinded
semantic review remains pending. The available older private output packet did
not match the remediated run and was not substituted for it.

Subject under test: Email source `7186dc988801995628ecff6b6a135278783f47c5`, with
production prompts, `run_benchmark`, strict Analysis schema and task validator.
Both production clients generated identical prompts/schema/output limits on all
22 frozen analysis/obligation cases before live calls. The parser repair below
comes after the recorded candidate run; do not relabel that run as testing the
repaired source.

| Dimension | Recorded 30B baseline | Exercised 9B candidate |
|---|---|---|
| Runtime | Ollama0.24.0 | Same binary/version |
| GGUF | `c6cf00aa338f4eaffd3503339e180bd381ced9bedb55862fd04358b97fc85c1b` | `cd76ec205963b3b33350093e6904d9de16c4e666fd104e1f632d25c7f15f2a13` |
| Quantization | Q4_K_S | Alias Q4_K_M; runtime metadata reports unknown |
| Framing | Established 30B template; historical raw channels not retained | Role/content messages plus empty closed thinking prefix |
| Context |8192 |32768, verified in actual loaded GPU instance |
| Model sampler defaults | top_k20/top_p0.8 | top_k1/top_p1/presence_penalty0 |
| Request settings | temperature0.1, analysis500, no seed | Identical |
| Scheduling | No retained model qualification | Existing public fixture smoke at1500, stopped on first rejection |
| Gateway expiry | Historical case time cannot serve as current request lifetime | Adapter uses current wall clock for expiry; keeps historical analysis time inside prompt |

Runtime binary SHA256:
`b2e45ade9cb754a079f74645e1183d613f582d98f7354b05f4f9a5bd81f8e0c9`.
9B template SHA256:
`790aae76aa0a2bbdb8bde833cf46a7169825353d3bc3ebafa174d7f6fb6a3980`.
Actual gateway release directory:
`3ca137aa71392e0d5b3b9dac4b4b03559e0018a4`; installed worker/source hashes are
recorded separately, so the release directory is not assumed to equal a source
checkout. Service environment context8192 does not override the observed32768
loaded context. No model/capacity hypothesis was used to explain a failure.

## Results and limits

Status: **direct safety acceptance only; not promoted**. One maximum144-call
reservation stopped at67 submissions:54 analysis,12 obligation and one scheduling
response. No gateway inference was reached, no retry or replacement run occurred.
Both corpora and canonical corpus fingerprints/repetitions match the recorded
baseline. Use the existing benchmark's labels and scorer; none were rewritten.

| Metric |30B analysis |9B direct analysis |30B obligation |9B direct obligation |
|---|---:|---:|---:|---:|
| Runs |54 |54 |12 |12 |
| Schema-valid rate |1.0 |1.0 |1.0 |1.0 |
| Action precision/recall |1.0/1.0 |1.0/1.0 |1.0/1.0 |1.0/1.0 |
| High/urgent misses |0 |0 |0 |0 |
| Deadline hallucinations |0 |0 |0 |0 |
| Adversarial/grounding failures |0/0 |0/0 |0/0 |0/0 |
| Category accuracy |0.851852 |0.833333 |1.0 |1.0 |
| Priority accuracy |0.944444 |0.944444 |1.0 |1.0 |
| Exact deadline rate |0.777778 |0.833333 |0.5 |0.5 |

All67 direct raw responses selected content, finished normally and had no
nonblank reasoning field. This is bounded evidence for this pinned profile,
not a guarantee about untested profiles. The deployed gateway's raw worker
channels are unobserved; its live task qualification remains outstanding.

Sanitized candidate metrics:

- `ollama-qwen35-9b-nonthinking-direct-analysis.json` (durable candidate artifact)
- `ollama-qwen35-9b-nonthinking-direct-obligation.json` (durable candidate artifact)

Candidate artifacts are retained outside the worktree; the frozen baseline
fixture inventory remains unchanged.

## Scheduling failure and origin repair

The retained scheduling response supplied the correct attendee and time but
quoted prose containing the mailbox. `scheduling.py` previously passed that
quote to RFC header parser `getaddresses`, which does not extract addresses from
ordinary prose. Blame traces this assumption to
`af8e00ecf7b3423593fcddd063f2676ddc4fd561` (strict scheduling extraction ledger).

Minimal public reproduction: an attendee supported by
`Please invite sender@example.com to our meeting.` was rejected with
`attendee_unsupported`. The source-bound matcher now recognizes a complete
normalized mailbox inside prose and checks the source occurrence's boundaries.
It also prevents a quote cropped out of a larger local part/domain from creating
false support. The header-parser call and its redundant validity helper are
removed; the mailbox adapter's actual header parser is unchanged.

The response also declared `new_meeting` while reporting an ambiguity reason.
That contradiction correctly remains rejected as `new_meeting_ambiguous`.
The original completion is retained unchanged. An offline variant removing only
the ambiguity list isolates the mailbox parser and passes after the repair;
it is not a rewritten production output or a passing scheduling-model run.

Regression evidence covers both directions, prose/address forms, source
association, cropped quotes and preserved ambiguity. Replay uses the raw saved
response with no additional inference.

## Durable evidence

Artifacts live outside worktrees in the private
`nonthinking-9b-qualification-20261007` evidence directory. Cite aliases/digests,
not local credential or document names:

| Alias | SHA256 |
|---|---|
| reservation.json | `3ef14f20ad11983ba77b4831e611ba0ddde06e71dcb9de69c1069001755f2a26` |
| freeze.json | `50c7980243463cdeb13cf32f5ebb1acab41043ca4e623b412d6067d51be09e3b` |
| verified-gateway-profile.json | `7745fde01d3f5eb40879d518302880ab2fdc62da59156ebd598dc5b5c01ee93c` |
| direct-channel-observation.json | `3de6dd9a56b1838fef12047267942a19cab2fff3c5c5a5c32f6f78ed9fa2efd1` |
| worker-channel-probes.json | `297b190105507e359c47d2353e410203537db13cfc1f654336cac22b4d921b1f` |
| validator-replay.json | `ce8943f176101143580fdd54ed55a17e509eea64e41b09cf959ae15bb21939af` |
| cropped-mailbox-before-after.json | `53ca00b5ef93fee9b90112cf52d01dbac980eefa071e27581be13e82eb3deed5` |
| exit.json | `fdd61a1b481632c0cc3e7e8153477fdbcf1375d2a86009d6f16f6799fb4db74c` |
| ollama-qwen35-9b-nonthinking-direct-analysis.json | `bf6b46605af9ce4d67f8827c326444fe1a80407d27b9cbddcd8d55bbb4921d83` |
| ollama-qwen35-9b-nonthinking-direct-obligation.json | `8d09e84fc2912d93544edfedc5bca5a16430b3f115013fddcfb7ed90177705d9` |

## Remaining gates

- Independent semantic review of summary/action outputs, with a matching
  remediated comparator if recovered. No human verdict is fabricated.
- Gateway live qualification on the frozen Email inputs and actual worker.
- Scheduling qualification beyond parser replay and the failed fixture smoke;
  its ambiguity contradiction is not repaired by accepting it.
- A reconciled source freeze and revised finite reservation before further live
  generation; retain the failed67 submissions in cumulative accounting.
- Profile/default promotion and shared-host migration are separate later steps.
