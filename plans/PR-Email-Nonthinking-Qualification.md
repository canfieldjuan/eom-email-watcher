# Email non-thinking policy and 9B qualification

## Contract

Operator accepts non-thinking as Email's target for both direct and gateway paths
("lets do it", 2026-10-07). The runtime/profile owns the mode. The actual pinned
Ollama 9B template closes an empty thinking block; no Invoice flag is copied.
Existing content-first reasoning fallback is preserved as a separate compatibility
policy. Necessity or removal is not decided by this qualification.

### Proven inventory and differences

Use docs/OLLAMA_EMAIL_ANALYSIS_QUALIFICATION.md as the recorded 30B comparator.
The existing benchmark and frozen analysis/obligation corpora own the oracle.
The candidate differs in model, template, model sampler defaults and loaded
context (32768 versus 8192). Request temperature0.1, analysis500 tokens,
scheduling1500, prompts, schemas, validators and labels were unchanged in the
first run. Production direct and gateway request projection matched on all22
frozen cases. Current wall-clock gateway expiry is a transport-only adapter
change; historical analysis timestamps stay inside the prompt.

Durable evidence: nonthinking-9b-qualification-20261007, outside worktrees.
One maximum144-call reservation stopped after67 submissions: direct54 analysis,
12 obligation, one scheduling failure. No gateway inference was reached, no retry
or replacement run is authorized by this record. Both deterministic corpus gates
passed; human semantic review and scheduling/gateway qualification remain open.
Never present an old unmatched private 30B packet as the remediated baseline.

### Root cause revision before implementation

The first scheduling response exposes two independent failure classes. It names
the correct attendee/time, but quotes prose containing the attendee address.
scheduling.py:1032-1038 treats that prose as an RFC email header using
getaddresses, losing mailbox identity. Introducing commit:
af8e00ecf7b3423593fcddd063f2676ddc4fd561 (strict scheduling extraction ledger).
This is a pre-existing validator defect, not a model-capacity hypothesis or
an implementation from PR217. The response also carries an ambiguity reason
with new_meeting; rejecting that contradiction is correct and remains required.

Correct invariant: an attendee's normalized complete mailbox must occur within
its quoted evidence AND within the source occurrence of that quote. Surrounding
prose is legal. Cropping the local part or domain out of a larger address must
not manufacture support. Source association, organizer/duplicate rules and
ambiguity admission remain mandatory. One scheduling evidence owner implements
this invariant, reusing the existing evidence-candidate and whitespace owners;
remove header parsing and its now-redundant address-validity helper.

### Required change surface

- scheduling.py: source-bound complete-mailbox evidence matcher at the point
  attendee support is decided.
- test_scheduling.py: public minimal prose reproduction, both boundary
  directions, cropped quotes, sibling mailbox forms and preserved ambiguity.
- Qualification record: distinguish the passing direct corpus gates from the
  stopped scheduling run and missing gateway/human qualification.

### Explicit non-scope

No prompt/model/config/runtime/default changes, fallback removal, corpus or label
edits, schema/cap changes, scheduling ambiguity relaxation, mailbox-header parser
changes, live retries, PR217 fixes, DocSum implementation or migration. This
revision permits only the isolated evidence-parser repair; it does not turn the
failed candidate into a qualified profile.

### Verification

Declare the prose-evidence regression fail-first. Then test complete-address
boundaries including cropped source quotes, good source/sender/display forms,
unknown source and evidence, organizer/duplicate behavior and ambiguity. Replay
the retained raw scheduling response without inference: attendee support must
be repaired, while new_meeting_ambiguous must still reject it. Run affected
scheduling/model/gateway/benchmark unit files and Ruff/format/diff checks.

### Acceptance

The operator authorized non-thinking qualification. This root parser revision
follows the standing reproduce/isolate/fix-at-origin rule. It is a correctness
repair to the existing source-evidence contract; model policy promotion remains
blocked by the failed scheduling candidate, absent gateway run and pending
human semantic review.

## Implementation and verification receipt

Fail-first prose regression:1 failed in0.37s with attendee_unsupported. After the origin repair:128 scheduling tests passed, then248 affected scheduling/model/gateway/benchmark tests passed in1.07s. An interim documentation artifact placement hit the frozen baseline inventory count; candidate metrics now remain in durable evidence rather than altering baseline fixtures. Final scheduling plus inventory regression:129 passed in0.34s; Ruff All checks passed; diff check clean. Cold AST audit changes only validate_scheduling_output, adds _attendee_evidence_supported and removes _is_valid_address; old test content is byte-preserved.

Offline retained-output replay repairs only attendee support and preserves new_meeting_ambiguous rejection. The isolated mailbox variant passes; larger local/domain cropped quotes admitted before are rejected after. No new generation, source prompt/schema/corpus/default change or model promotion. See docs/EMAIL_MODEL_POLICY_QUALIFICATION.md for the single Email policy/result record.

Gap audit: NOT DONE for full 9B qualification/promotion. Direct corpus safety is proven; actual gateway run, scheduling compatibility and independent semantic review remain outstanding. The bounded run stayed stopped at67 submissions.
