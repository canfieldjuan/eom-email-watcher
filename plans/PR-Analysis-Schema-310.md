# Email-analysis output schema matches its contract — #310

Status: accepted contract only. No production code, test, corpus, prompt, setting or
installation change in this PR.

## Operator acceptance

Juan accepted the reviewed contract at `e9bea7ee662fef26b66909fd75aa1918b755aff6` in the
coding chat on 2026-10-30, verbatim: “I accept let’s continue”. The Codex review of that commit
completed with no findings.

Merge this PR as contract-only after CI, then implement R1–R4 in a separate PR on the merged
contract, starting with fail-first regressions. The GPU settling run needs a separate operator go.

Base: `3edcae19de0fff45ebc808aa5650e17a411a2e45`.

## Root cause

Three owners describe the same email-analysis output, and they disagree.

| Owner | What it says today |
|---|---|
| Prompt, `model.py:166-167` | All eight keys are required: `category, priority, summary, action_required, suggested_action, deadline_text, deadline_iso, confidence`. |
| Prompt, `model.py:152-157` | `deadline_iso` must be `YYYY-MM-DD`. |
| `Analysis` model, `model.py:90-92` | `suggested_action`, `deadline_text` and `deadline_iso` have `default=None`. Pydantic therefore leaves them out of `required` in `Analysis.model_json_schema()`, and `deadline_iso` is an unrestricted string. |
| Decoder schema, `model.py:411` (direct) and `:722` (gateway) | That generated schema is sent as a strict `json_schema` response format (`model.py:366-372`), so a runtime that compiles it to a grammar *permits* omitting those three keys and any string for the deadline. |
| Validator, `validate_analysis` `model.py:191-217` | `Analysis.model_validate` accepts an omitted key as `None`; `action_required=true` without a usable suggestion is rejected (`:215-216`); a deadline that does not parse as `%Y-%m-%d`, is in the past, or has no `deadline_text` is silently cleared (`:200-209`). |

Origin, from history (not from docs):

- `cd5a387` (2026-07-18, first commit) introduced the defaults, the "Required keys" prompt line,
  the strict schema and the date-only prompt rule together. Its validator was lenient: an
  action without a suggestion was accepted, so the omission permission was harmless.
- `691a599e` (#11, 2026-08-25) intentionally made an action without a suggestion fail closed.
  Its PR body lists "No schema … changes" as non-scope, and its boundary probes call the
  validator with hand-built dicts only; none exercises what the decoder schema allows.
- No test pins the `Analysis` schema's `required` list. By contrast, the scheduling schema is
  shaped by `scheduling_response_schema()` and its optional keys are asserted on purpose
  (`tests/test_scheduling.py:110-111`). Every `Analysis(...)` fixture in `tests/` and the
  production rebuild `service.py:_stored_analysis` already pass all eight keys.

Conclusion: the optional decoder keys are an unintended gap, not a design choice. A model that
writes every key (the 9B so far) hides it. A model that follows the grammar's permission to
stop after `confidence` turns every action-required email into a rejection.

## Validated inventory and reproduction

`reproduction.py` runs, with no model or GPU, the real `Analysis`/`validate_analysis` at the
base commit and the real gateway admission model and output validator copied from
`local-inference-gateway` `origin/main` `f105bfd` (`contracts.py`, unchanged). The proposed schema
in the reproduction is a probe; it is not the implementation.

| Check | Current | Proposed |
|---|---|---|
| Decoder `required` | 5 keys (`category, priority, summary, action_required, confidence`) | all 8, in declared order |
| Gateway admission (`Generation`) | admitted | admitted |
| Gateway admission with `pattern` on `deadline_iso` | — | rejected: "unsupported keyword 'pattern'" |
| Gateway admission with `format: date` | — | rejected: "unsupported keyword 'format'" |
| Gateway output check, observed 35B answer (no suggestion, timestamp deadline) | valid | invalid: missing `suggested_action`, `deadline_text`; deadline fails |
| Gateway output check, observed 9B answer (all keys, date-only) | valid | valid |
| `validate_analysis`, no-action answer with the 3 nullable keys omitted | accepted as nulls | (R2) rejected |
| `validate_analysis`, action answer with `deadline_iso` `2026-09-05T00:00:00-05:00Z` | accepted; `deadline_iso` silently cleared, `deadline_text` kept | (R3) cannot be decoded by an enforcing runtime; validator still clears it from a non-enforcing one |

Grammar evidence (llama.cpp schema-to-grammar Python port from the sandbox `ik_llama.cpp`
checkout `a61ad78`, an approximation of the converter Ollama and llama-server compile in):

- Current root rule: the five required keys in order, then
  `( "," ( suggested-action … | deadline-text … | deadline-iso ) )? "}"`, so the object may end
  right after `confidence`.
- Proposed root rule: all eight keys in declared order, none optional;
  `deadline-iso-0 ::= "\"" char{10,10} "\""`.
- Observed outputs agree with the current rule. The 9B wrote `confidence` before
  `suggested_action`. The 35B wrote `confidence`, then `deadline_iso`, then stopped without a
  suggestion.

The gateway does not reshape this schema: `decoder_schema` returns it unchanged unless it uses
passage definitions (`contracts.py:268-271`). The watcher encodes the request without sorting keys
(`model.py:575-577`). The gateway validates the generated object against the same schema
(`worker.py:414`) and records a mismatch as `invalid_worker_output`, non-retryable
(`app.py:212-218`).

## Required behavior

### R1 — the decoder schema requires every key the prompt requires

One owner, `analysis_response_schema()` in `model.py`, produces the email-analysis response
schema. `LocalModel.analyze` and `GatewayModel.analyze` both send it; nothing else sends
`Analysis.model_json_schema()` directly. Its `required` list equals its property names, all
eight, in declared order. The three nullable keys keep their `string | null` union, so "none" is
still expressible as an explicit `null`. The schema uses only keywords the gateway admits today.

### R2 — the validator enforces the same key set

The `Analysis` fields `suggested_action`, `deadline_text` and `deadline_iso` become required
but nullable: no default, `None` still allowed. An answer missing any of them fails
`Analysis.model_validate` and maps to the existing "did not match the required schema" error
(benchmark code `schema_mismatch`). Explicit `null` keeps today's meaning. Unchanged:
- NUL-character rejection;
- blank-suggestion normalization;
- suggestion- and deadline-driven promotion to `action_required=true`;
- the action-without-suggestion rejection;
- the past and malformed deadline clearing.

This protects the direct path when a runtime does not enforce the schema.

### R3 — the deadline shape is bounded at decode time, cleared at validation

In the decoder schema only, the string branch of `deadline_iso` carries `minLength: 10` and
`maxLength: 10`. That is the strongest date bound the gateway admits today. It removes timestamps,
offsets and repeated-suffix loops, and it bounds that field's output. The `Analysis` field
itself gets no length or pattern constraint, because the frozen corpus case
`validation-malformed-date-clears-iso` (`deadline_iso: "September 5"`) requires a malformed date
to be cleared, not rejected. The existing `%Y-%m-%d` parse and past-date clearing stay the final
authority. Do not add `pattern` or `format`. The gateway rejects both at admission, which would
turn every gateway email analysis into a request failure.

### R4 — no other observable behavior changes

- **Prompt text unchanged.** It already states the stricter contract.
- **Frozen corpora unchanged:** `benchmarks/email-analysis-v1.json` and
  `benchmarks/email-obligation-v1.json` stay byte-identical, and their validation cases keep
  their expected outcomes.
- **Failure handling unchanged.** A rejected answer still records a non-retryable analysis
  failure and sends the fallback notification on both paths. On the gateway path, a missing key
  or over-long deadline is now rejected by the gateway (`invalid_worker_output`) before the
  watcher sees it, instead of by the watcher (`application_output_rejected`). That is the only
  visible change in failure code.
- **Persisted rows, notifications and scheduling extraction unchanged.**
- **Known model-visible change:** the decoded key order moves `suggested_action`,
  `deadline_text` and `deadline_iso` ahead of `confidence`. This is why R4 settling requires a 9B
  re-qualification, not only unit tests.

## Required change surface and single owner

- `src/eom_email_watcher/model.py`:
  - make the `Analysis` nullable fields required-nullable;
  - add `analysis_response_schema()` (derive from `Analysis.model_json_schema()`, add the R3
    bound);
  - use it at `:411` and `:722`.
- `tests/test_model.py`: R1–R3 regressions.
- Any test that builds an `Analysis` or a raw answer without all eight keys is updated to pass
  explicit `None`; the inventory above found none in fixtures.

## Explicit non-scope

- #310 acceptance item 1 (the 35B comparison harness's copied template with an extra trailing
  newline). That harness lives in local evidence, not in this repo; fix it there before any 35B
  ranking.
- Gateway changes. Admitting `pattern` or `format` would allow an exact `YYYY-MM-DD` grammar.
  That is a `local-inference-gateway` admission-contract change, to be proposed there
  separately; R3 is the bound available today.
- Encoding the conditional "`action_required=true` ⇒ non-null suggestion" in the schema
  (a root `anyOf` of two closed objects is admitted). It would forbid outputs the validator
  deliberately repairs (a suggestion with `action_required=false` is promoted to true) and
  changes decoding more broadly. The validator stays the owner of that rule.
- Prompt, category/priority rules, scheduling schema, providers, settings, installation,
  model choice.

## Verification plan

Fail-first, before implementation, at the base:

1. `analysis_response_schema()["required"] == list(properties)` (all eight, declared order).
2. Its `deadline_iso` string branch has `minLength == maxLength == 10`, and no node in the schema
   uses a keyword outside the gateway's admitted set (`pattern`, `format`, `$ref`, `oneOf`,
   `allOf`, `const` absent).
3. Request-capture tests through the real `LocalModel` and `GatewayModel` request builders, with
   fake HTTP transport only, show both send that exact schema.
4. Validator boundaries:
   - each nullable key omitted (one at a time) → `ModelError` "did not match the required schema";
   - all three explicit `null` with `action_required=false` → accepted;
   - `action_required=true` with `suggested_action` `null`, `""` or whitespace → still rejected;
   - `deadline_iso` `"September 5"` → still cleared;
   - `"2026-09-05T00:00:00-05:00Z"` from a non-enforcing runtime → still cleared;
   - past date → still cleared;
   - valid future date → kept.

Then:

- `uv run pytest -q` and `uv run ruff check .` pass.
- Both corpora still hash to `b64c74f44478a776f328b3e471cf66e57e27dfacf23ec6018b0ecb52dc44ccb6`
  and `f6abe7c55e405f03ffc9afbe54b9b191e74d6e1af4d4a0a833b503811d738e56`, and the benchmark's
  validation boundary passes every case unchanged.
- Rerun `reproduction.py` against the implementation head. The schema the code sends equals
  `proposed.json` and is admitted by the gateway.

Settling evidence (GPU; needs operator go):

- **Qwen3.5-9B (production model).** Run both frozen corpora with 3 repetitions, through the
  same pinned direct Ollama profile as the 2026-10-07 baseline and again through the installed
  gateway path. No aggregate metric may fall below that baseline. Baseline artifacts and
  values:

  | Metric | analysis (`bf6b4660…`) | obligation (`8d09e84f…`) |
  |---|---:|---:|
  | Schema-valid rate | 1.0 | 1.0 |
  | Category accuracy | 0.833333 | 1.0 |
  | Priority accuracy | 0.944444 | 1.0 |
  | Action-required precision | 1.0 | 1.0 |
  | Action-required recall | 1.0 | 1.0 |
  | Suggested-action valid rate | 1.0 | 1.0 |
  | Deadline exact rate | 0.833333 | 0.5 |
  | High/urgent false negatives | 0 | 0 |
  | Deadline hallucinations | 0 | 0 |
  | Prompt-injection failures | 0 | 0 |
  | Grounding failures | 0 | 0 |

  A drop in any metric blocks the merge.
- **Qwen3.6-35B-A3B.** Optional ranking only, never a gate. It runs only after #310 item 1 is
  fixed; Ollama 0.24 cannot load the Unsloth MTP file, so it uses the Q4_K_M build.

## Evidence aliases (SHA-256)

Local folder `codex-evidence/eom-email-watcher/310-analysis-schema-contract/`:

- `reproduction.py`: `c50b5a1c02cb9dcfc616a8ba5fa37552d7cc6d08e3b048c9a8a4057d21d155b2`
- `reproduction.json`: `13ac59382a5703bc7e3d277f1170340954035a152161209c2dd539171cb9c2b6`
- `gateway-contracts-f105bfd.py`: `f1ff3f518043e4c9bae6591e1c325cc71549b702de240d3ca7925ef37e6f7841`
- `current.json`: `41075d682cb14d46344f98aca4bb30eb3782fbc3b4ad7940656cd28e4b9b35af`
- `proposed.json`: `0430b53df43f4a0bcd270894e1a003e9b5d18614c36a4193bdbad3e1428fb277`
- `current.gbnf`: `cb2641d5cdf500f18de12b322e9bead10eb43e9655d9c0e134978122859dad57`
- `proposed.gbnf`: `0746c42bedbbeefae720dcf21e6f0caf9941df32eff1d874fc24f6e8bafa7d25`
